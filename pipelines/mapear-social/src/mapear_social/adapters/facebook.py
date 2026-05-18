"""Facebook adapter — apify/facebook-posts-scraper (KoJrdxJCTtpon81KY).

Input contract:
    {
      "startUrls": [{"url": "https://facebook.com/<page_handle>"}, ...],
      "resultsLimit": 10,
      "onlyPostsNewerThan": "<ISO 8601>",  # only when `since` provided
      "includeVideoTranscripts": false,
      "proxyConfiguration": {"useApifyProxy": true, "apifyProxyGroups": ["RESIDENTIAL"]}
    }

Output items used:
    postId | text | time | likes | comments | shares | viewsCount |
    url | user.name | user.profileUrl | isVideo

The Apify actor historically nests counters under different keys across
versions (``likes`` vs ``likesCount``, ``time`` vs ``timestamp``), so
``parse_item`` looks in multiple candidate fields and fails loud via
``SchemaDriftError`` only when none are present.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mapear_domain.entity_resolution import Target
from mapear_social.adapters.base import PlatformAdapter, SchemaDriftError
from mapear_social.models import Engagement, SocialAccount, SocialPost

_SCHEMA_VERSION = 4

_REQUIRED_KEYS: tuple[str, ...] = ("postId", "url")
_DATETIME_KEYS: tuple[str, ...] = ("time", "timestamp", "publishTime")


class FacebookAdapter(PlatformAdapter):
    actor_id = "KoJrdxJCTtpon81KY"
    platform = "facebook"

    def expected_schema_version(self) -> int:
        return _SCHEMA_VERSION

    def targets_with_handle(self, targets: list[Target]) -> list[Target]:
        return [t for t in targets if t.facebook_page]

    def build_input(
        self,
        targets: list[Target],
        *,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        with_handle = self.targets_with_handle(targets)
        start_urls = [
            {"url": self._normalize_page_url(t.facebook_page)} for t in with_handle
        ]
        payload: dict[str, Any] = {
            "startUrls": start_urls,
            "resultsLimit": 10,
            "includeVideoTranscripts": False,
            "proxyConfiguration": {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
            },
        }
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            payload["onlyPostsNewerThan"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        return payload

    def parse_item(
        self,
        raw: dict[str, Any],
        actor_run_id: str,
        ingestion_run_id: str,
    ) -> SocialPost:
        if "error" in raw and "postId" not in raw:
            # Distinguish deterministic "page unavailable" responses (private /
            # deleted pages) from generic actor sentinels so they can be
            # filtered out of alerting and aggregated into a seed-health report
            # instead of polluting the drift DLQ every run.
            error_code = str(raw.get("error") or "")
            if error_code == "not_available":
                raise SchemaDriftError(
                    f"page_unavailable: Facebook page returned 'not_available' "
                    f"— {raw.get('url')!r}: {raw.get('errorDescription')!r}"
                )
            raise SchemaDriftError(
                f"non_post_item: Facebook actor error/sentinel — "
                f"keys: {sorted(raw.keys())!r}"
            )
        self.require_keys(raw, _REQUIRED_KEYS)
        if not any(raw.get(k) for k in _DATETIME_KEYS):
            raise SchemaDriftError(
                "schema drift: Provided Schema does not match Actor — "
                f"no datetime key present (expected one of {_DATETIME_KEYS!r})"
            )

        post_id_raw = str(raw["postId"])
        text = (raw.get("text") or "").strip()
        url = raw["url"]

        user_obj = raw.get("user") if isinstance(raw.get("user"), dict) else {}

        page_handle = self._page_handle_from(raw)
        display_name = (
            raw.get("pageName")
            or raw.get("userName")
            or user_obj.get("name")
            or page_handle
        )
        verified = bool(
            raw.get("pageVerified")
            or raw.get("isVerified")
            or user_obj.get("verified")
            or False
        )

        account = SocialAccount(
            platform=self.platform,
            handle=page_handle,
            display_name=display_name,
            verified=verified,
        )

        engagement = Engagement(
            likes=_first_int(raw, ("likes", "likesCount")),
            comments=_first_int(raw, ("comments", "commentsCount")),
            shares=_first_int(raw, ("shares", "sharesCount")),
            views=_first_int(raw, ("viewsCount", "videoViewCount")),
        )

        published_at = _parse_datetime(raw, ("time", "timestamp", "publishTime"))

        is_repost = bool(raw.get("isShared") or raw.get("sharedPostId"))
        parent = raw.get("sharedPostId") or raw.get("parentPostId")

        return SocialPost(
            post_id=self.prefix_post_id(self.platform, post_id_raw),
            platform=self.platform,
            url=url,
            account=account,
            author_display_name=display_name,
            text=text,
            language=raw.get("language"),
            published_at=published_at,
            engagement=engagement,
            is_repost=is_repost,
            is_reply=False,
            parent_post_id=(
                self.prefix_post_id(self.platform, str(parent)) if parent else None
            ),
            content_hash=self.compute_content_hash(self.platform, post_id_raw, text),
            actor_run_id=actor_run_id,
            ingestion_run_id=ingestion_run_id,
            schema_version=_SCHEMA_VERSION,
        )

    @staticmethod
    def _normalize_page_url(page_handle: str) -> str:
        h = page_handle.strip().removeprefix("@")
        if h.startswith("http://") or h.startswith("https://"):
            return h
        return f"https://facebook.com/{h}"

    @staticmethod
    def _page_handle_from(raw: dict[str, Any]) -> str:
        # Backward compat: old actor had explicit pageUrl field
        page_url = raw.get("pageUrl") or ""
        if page_url:
            return page_url.rstrip("/").split("/")[-1]
        # New actor: extract page handle from post URL
        # (facebook.com/<handle>/posts/<id>)
        post_url = raw.get("url") or ""
        if post_url:
            parts = [p for p in post_url.rstrip("/").split("/") if p]
            for i, part in enumerate(parts):
                if part == "posts" and i > 0:
                    return parts[i - 1]
        # Fall back to explicit name fields
        user_obj = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        return raw.get("pageName") or user_obj.get("name") or ""


def _first_int(raw: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key in raw and raw[key] is not None:
            try:
                return int(raw[key])
            except (TypeError, ValueError):
                continue
    return None


def _parse_datetime(raw: dict[str, Any], keys: tuple[str, ...]) -> datetime:
    for key in keys:
        value = raw.get(key)
        if not value:
            continue
        if isinstance(value, int | float):
            ts = float(value)
            if ts > 10_000_000_000:
                ts /= 1000
            return datetime.fromtimestamp(ts, tz=UTC)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    from loguru import logger

    logger.warning("Facebook item missing published_at — defaulting to now")
    return datetime.now(UTC)
