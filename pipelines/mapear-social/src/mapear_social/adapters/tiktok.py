"""TikTok adapter — clockworks/tiktok-scraper (GdWCkxBtKWOsKjdch).

Input contract:
    {
      "profiles": [<handle>, ...],      # bare handles, no @
      "profileScrapeSections": ["videos"],
      "resultsPerPage": 10,
      "oldestPostDateUnified": "YYYY-MM-DD",  # only when `since` provided
      "shouldDownloadVideos": false,
      "shouldDownloadAvatars": false,
      "shouldDownloadCovers": false,
      "shouldDownloadSubtitles": false,
      "proxyCountryCode": "None"
    }

Output items used:
    id | text | createTimeISO | playCount | diggCount | commentCount |
    shareCount | webVideoUrl | authorMeta.name | authorMeta.nickName |
    authorMeta.verified | isSponsored

authorMeta field mapping (new actor):
    name     → handle (unique username / login)
    nickName → display name

Scope: organic videos only. ``isSponsored`` (and legacy ``isAd``) items are
dropped at parse time via SchemaDriftError (same routing as IG story rejection).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mapear_domain.entity_resolution import Target
from mapear_social.adapters.base import PlatformAdapter, SchemaDriftError
from mapear_social.models import Engagement, SocialAccount, SocialPost

_SCHEMA_VERSION = 2
_REQUIRED_KEYS: tuple[str, ...] = ("id", "webVideoUrl", "authorMeta")


class TikTokAdapter(PlatformAdapter):
    actor_id = "GdWCkxBtKWOsKjdch"
    platform = "tiktok"

    def expected_schema_version(self) -> int:
        return _SCHEMA_VERSION

    def targets_with_handle(self, targets: list[Target]) -> list[Target]:
        return [t for t in targets if getattr(t, "tiktok_handle", None)]

    def build_input(
        self,
        targets: list[Target],
        *,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        with_handle = self.targets_with_handle(targets)
        payload: dict[str, Any] = {
            "profiles": [t.tiktok_handle.strip().lstrip("@") for t in with_handle],
            "profileScrapeSections": ["videos"],
            "resultsPerPage": 10,
            "shouldDownloadVideos": False,
            "shouldDownloadAvatars": False,
            "shouldDownloadCovers": False,
            "shouldDownloadSubtitles": False,
            "proxyCountryCode": "None",
        }
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            payload["oldestPostDateUnified"] = since.strftime("%Y-%m-%d")
        return payload

    def parse_item(
        self,
        raw: dict[str, Any],
        actor_run_id: str,
        ingestion_run_id: str,
    ) -> SocialPost:
        if "noResults" in raw:
            raise SchemaDriftError(
                f"non_post_item: TikTok actor returned noResults sentinel — "
                f"keys: {sorted(raw.keys())!r}"
            )
        # Actor returns {"authorMeta", "input", "note"} when a profile is
        # unavailable (private, geo-blocked, removed) and {"error", "input",
        # "url"} for individual video fetch errors.  Both are actor-level
        # housekeeping rows, not actual videos — classify as non_post_item so
        # the pipeline exits cleanly instead of raising SystemExit(4).
        if "note" in raw and "id" not in raw:
            raise SchemaDriftError(
                f"non_post_item: TikTok actor returned note-only item "
                f"(account unavailable or no posts) — "
                f"note={str(raw.get('note'))[:120]!r}, keys: {sorted(raw.keys())!r}"
            )
        if "error" in raw and "id" not in raw:
            raise SchemaDriftError(
                f"non_post_item: TikTok actor returned error item — "
                f"error={str(raw.get('error'))[:120]!r}, keys: {sorted(raw.keys())!r}"
            )
        self.require_keys(raw, _REQUIRED_KEYS)

        # Drop sponsored/ad content (isSponsored = new actor, isAd = old actor)
        if raw.get("isSponsored") or raw.get("isAd"):
            raise SchemaDriftError(
                "non_post_item: TikTok sponsored/ad item — out of organic scope"
            )

        author = raw["authorMeta"] if isinstance(raw["authorMeta"], dict) else {}
        # New actor: name = handle slug, nickName = display name
        # Old actor: uniqueId = handle slug, name/nickName = display name
        handle = str(author.get("uniqueId") or author.get("name") or "")
        display = str(author.get("nickName") or author.get("name") or handle)

        post_id_raw = str(raw["id"])
        text = (raw.get("text") or "").strip()

        # New actor: createTimeISO (ISO 8601); old actor: createTime (epoch or ISO)
        ts_raw = raw.get("createTimeISO") or raw.get("createTime")
        if ts_raw is None:
            raise SchemaDriftError(
                f"schema drift: missing timestamp (createTimeISO / createTime) — "
                f"keys: {sorted(raw.keys())!r}"
            )

        engagement = Engagement(
            likes=_safe_int(raw.get("diggCount")),
            comments=_safe_int(raw.get("commentCount")),
            shares=_safe_int(raw.get("shareCount")),
            views=_safe_int(raw.get("playCount")),
        )

        return SocialPost(
            post_id=self.prefix_post_id(self.platform, post_id_raw),
            platform=self.platform,
            url=raw["webVideoUrl"],
            account=SocialAccount(
                platform=self.platform,
                handle=handle,
                display_name=display,
                verified=bool(author.get("verified") or False),
            ),
            author_display_name=display,
            text=text,
            language=raw.get("textLanguage") or raw.get("locationLanguage"),
            published_at=_parse_ts(ts_raw),
            engagement=engagement,
            is_repost=False,
            is_reply=False,
            parent_post_id=None,
            content_hash=self.compute_content_hash(self.platform, post_id_raw, text),
            actor_run_id=actor_run_id,
            ingestion_run_id=ingestion_run_id,
            schema_version=_SCHEMA_VERSION,
        )


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, int | float):
        ts = float(value)
        if ts > 10_000_000_000:
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"unsupported timestamp type {type(value).__name__}")
