"""Instagram adapter — apify/instagram-profile-scraper (dSCLg0C3YEZ83HzYX).

Input contract:
    {
      "usernames": [<handle>, ...],
      "maxPosts": 10,
      "onlyPostsNewerThan": "<ISO 8601>",  # only when `since` provided
    }

Output shape: one dataset item per profile with posts nested under
latestPosts. expand_items() flattens these into individual post dicts
before parse_item() is called.

Post fields used:
    id | shortCode | caption | timestamp | likesCount | commentsCount |
    type | (url constructed from shortCode)

Profile fields injected per post:
    username → ownerUsername | isVerified → ownerVerified

Scope: posts and reels only. Stories are transient and the Apify actor
does not return them reliably — explicitly excluded in the plan (4.3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mapear_domain.entity_resolution import Target
from mapear_social.adapters.base import PlatformAdapter
from mapear_social.models import Engagement, SocialAccount, SocialPost

_SCHEMA_VERSION = 3
_REQUIRED_KEYS: tuple[str, ...] = ("id", "url", "timestamp", "ownerUsername")

# Keys that only appear in real post items — used to detect meta/status items
# that Apify sometimes inserts in the dataset (e.g. "no posts found" markers).
_POST_SIGNAL_KEYS: frozenset[str] = frozenset(
    {
        "shortCode",
        "shortcode",
        "postId",
        "pk",
        "igId",
        "mediaId",
        "graphId",
        "caption",
        "description",
        "captionText",
        "likesCount",
        "likeCount",
        "commentsCount",
        "commentCount",
        "type",
        "mediaType",
        "isVideo",
        "id",
        "timestamp",
        "takenAt",
        "takenAtTimestamp",
        "isoDate",
        "ownerUsername",
        "username",
        "ownerName",
    }
)

# types we keep — Image post, Video (reel). Everything else (Story, Highlight,
# IGTV legacy) is dropped at parse time via filter_item.
_ACCEPTED_TYPES = frozenset({"Image", "Sidecar", "Video", "Reel"})

# shu8hvrXbJbY3Eb9W ships schema updates without version bumps; this maps the
# UPPER_SNAKE Instagram Graph API mediaType values to our canonical PascalCase.
_MEDIA_TYPE_MAP: dict[str, str] = {
    "IMAGE": "Image",
    "VIDEO": "Video",
    "CAROUSEL_ALBUM": "Sidecar",
    "CLIPS": "Reel",
    "REEL": "Reel",
    "GRAPHIMAGE": "Image",
    "GRAPHVIDEO": "Video",
    "GRAPHSIDECAR": "Sidecar",
}


class InstagramAdapter(PlatformAdapter):
    actor_id = "dSCLg0C3YEZ83HzYX"
    platform = "instagram"

    def expected_schema_version(self) -> int:
        return _SCHEMA_VERSION

    def targets_with_handle(self, targets: list[Target]) -> list[Target]:
        return [t for t in targets if t.instagram_username]

    def build_input(
        self,
        targets: list[Target],
        *,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        with_handle = self.targets_with_handle(targets)
        payload: dict[str, Any] = {
            "usernames": [
                t.instagram_username.strip().lstrip("@") for t in with_handle
            ],
            "maxPosts": 10,
            "proxy": {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
            },
        }
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            payload["onlyPostsNewerThan"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        return payload

    def expand_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Flatten profile-level items from instagram-profile-scraper into post items.

        instagram-profile-scraper returns one dataset item per profile with
        posts nested under latestPosts. Injects username and verified from the
        profile into each post so parse_item() requires no changes.

        Items without latestPosts are passed through unchanged (backward compat
        with old-format post-level items).
        """
        expanded: list[dict[str, Any]] = []
        for item in items:
            posts = item.get("latestPosts")
            if posts is None:
                expanded.append(item)
                continue
            username = item.get("username") or ""
            verified = bool(item.get("isVerified") or item.get("verified") or False)
            for post in posts:
                enriched = dict(post)
                if "ownerUsername" not in enriched and "username" not in enriched:
                    enriched["username"] = username
                if (
                    "ownerVerified" not in enriched
                    and "isVerified" not in enriched
                    and "verified" not in enriched
                ):
                    enriched["ownerVerified"] = verified
                expanded.append(enriched)
        return expanded

    def parse_item(
        self,
        raw: dict[str, Any],
        actor_run_id: str,
        ingestion_run_id: str,
    ) -> SocialPost:
        if not _POST_SIGNAL_KEYS & raw.keys():
            from mapear_social.adapters.base import SchemaDriftError

            raise SchemaDriftError(
                f"non_post_item: no post-signal fields found — "
                f"keys: {sorted(raw.keys())!r}"
            )
        raw = _normalize_raw(raw)
        # If normalization could not derive an id from any known alias, the item
        # has no usable post identity — it is a profile summary, error response, or
        # unknown actor item. Classify as non_post_item so the pipeline does not
        # exit(4) when an entire batch consists of such items.
        if "id" not in raw:
            from mapear_social.adapters.base import SchemaDriftError

            raise SchemaDriftError(
                f"non_post_item: no post id after normalization (aliases exhausted) — "
                f"keys: {sorted(raw.keys())!r}"
            )
        self.require_keys(raw, _REQUIRED_KEYS)

        raw_type = str(raw.get("type") or "")
        if raw_type and raw_type not in _ACCEPTED_TYPES:
            # Use the same SchemaDriftError channel as missing-keys so
            # the alert filter catches it — but the pipeline will route
            # it to dlq_social with error_type='skipped_type'.
            from mapear_social.adapters.base import SchemaDriftError

            raise SchemaDriftError(
                f"non_post_item: skipped IG item"
                f" type={raw_type!r} (not in {_ACCEPTED_TYPES})"
            )

        post_id_raw = str(raw["id"])
        text = (raw.get("caption") or "").strip()
        owner = str(raw["ownerUsername"])

        account = SocialAccount(
            platform=self.platform,
            handle=owner,
            display_name=raw.get("ownerFullName") or owner,
            verified=bool(raw.get("ownerVerified") or False),
        )

        engagement = Engagement(
            likes=_safe_int(raw.get("likesCount")),
            comments=_safe_int(raw.get("commentsCount")),
            shares=None,  # IG does not expose shares via this actor
            views=_safe_int(raw.get("videoViewCount")),
        )

        return SocialPost(
            post_id=self.prefix_post_id(self.platform, post_id_raw),
            platform=self.platform,
            url=raw["url"],
            account=account,
            author_display_name=raw.get("ownerFullName") or owner,
            text=text,
            language=None,
            published_at=_parse_ts(raw["timestamp"]),
            engagement=engagement,
            is_repost=False,
            is_reply=False,
            parent_post_id=None,
            content_hash=self.compute_content_hash(self.platform, post_id_raw, text),
            actor_run_id=actor_run_id,
            ingestion_run_id=ingestion_run_id,
            schema_version=_SCHEMA_VERSION,
        )


def _normalize_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Map known alternate key names from newer actor versions to canonical keys.

    shu8hvrXbJbY3Eb9W ships schema updates without version bumps.  Rather than
    incrementing _SCHEMA_VERSION for each field rename we resolve aliases here
    so the parse logic above stays unchanged.

    Alias coverage is intentionally broad: the actor has shipped at least three
    distinct schemas (v1 flat, v2 nested-owner, v3 Graph-API-style) without
    version bumps.  Every alias added here came from an observed production or
    staging payload — do not prune without checking DLQ samples first.
    """
    out = dict(raw)

    # --- id ---
    # shortCode is the canonical IG URL segment; pk/igId are internal numeric IDs.
    if "id" not in out:
        for alias in (
            "shortCode",
            "shortcode",  # case variants
            "postId",
            "post_id",
            "pk",
            "igId",
            "mediaId",
            "code",
            "graphId",
        ):
            if alias in out:
                out["id"] = out[alias]
                break

    # --- url ---
    # Reconstruct from shortCode or id when the field is absent.
    # displayUrl is deliberately excluded — it is the media thumbnail URL,
    # not the post permalink.
    if "url" not in out:
        for alias in ("postUrl", "post_url", "permalink", "link"):
            if alias in out:
                out["url"] = out[alias]
                break
    if "url" not in out:
        # shortCode produces a human-readable permalink; prefer it over the
        # numeric id that instagram-profile-scraper emits as "id".
        sc = out.get("shortCode") or out.get("shortcode")
        if sc:
            out["url"] = f"https://www.instagram.com/p/{sc}/"
        elif "id" in out:
            out["url"] = f"https://www.instagram.com/p/{out['id']}/"

    # --- timestamp ---
    # Epoch seconds/millis, ISO-8601, or actor-specific field names.
    if "timestamp" not in out:
        for alias in (
            "takenAt",
            "takenAtTimestamp",
            "taken_at",
            "createdAt",
            "created_at",
            "postedAt",
            "posted_at",
            "publishedAt",
            "published_at",
            "isoDate",
            "date",
            "datetime",
        ):
            if alias in out:
                out["timestamp"] = out[alias]
                break

    # --- ownerUsername ---
    # Flat field, nested struct, or last-resort URL extraction.
    if "ownerUsername" not in out:
        # 1. flat alternatives
        for alias in (
            "username",
            "authorUsername",
            "author_username",
            "ownerName",
            "queryUsername",
            "handle",
            "userName",  # camelCase variant
        ):
            if alias in out:
                out["ownerUsername"] = out[alias]
                break

    if "ownerUsername" not in out:
        # 2. nested under common parent keys
        for parent_key in ("ownerProfile", "author", "owner", "user"):
            parent = out.get(parent_key)
            if isinstance(parent, dict):
                uname = (
                    parent.get("username")
                    or parent.get("userName")
                    or parent.get("handle")
                )
                if uname:
                    out["ownerUsername"] = uname
                    break

    if "ownerUsername" not in out:
        # 3. extract from the post URL or any profile/input URL (last resort)
        for url_key in ("url", "inputUrl", "postUrl", "profileUrl", "pageUrl"):
            extracted = _extract_ig_username(str(out.get(url_key) or ""))
            if extracted:
                out["ownerUsername"] = extracted
                break

    # --- type ---
    # mediaType uses Instagram Graph API UPPER_SNAKE convention.
    if not out.get("type"):
        raw_mt = str(out.get("mediaType") or "").upper().replace(" ", "_")
        mapped = _MEDIA_TYPE_MAP.get(raw_mt)
        if mapped:
            out["type"] = mapped
        elif not out.get("type") and out.get("isVideo"):
            out["type"] = "Video"

    # --- engagement ---
    if "likesCount" not in out:
        for alias in ("likeCount", "likes", "likes_count", "likesNumber"):
            if alias in out:
                out["likesCount"] = out[alias]
                break
    if "commentsCount" not in out:
        for alias in ("commentCount", "comments", "comments_count", "commentsNumber"):
            if alias in out:
                out["commentsCount"] = out[alias]
                break
    if "videoViewCount" not in out:
        for alias in (
            "videoPlayCount",
            "playCount",
            "video_view_count",
            "viewCount",
            "views",
            "reelsVideoViewCount",
        ):
            if alias in out:
                out["videoViewCount"] = out[alias]
                break

    # --- display name / verified ---
    if "ownerFullName" not in out:
        for alias in ("ownerName", "fullName", "full_name", "name"):
            if alias in out:
                out["ownerFullName"] = out[alias]
                break
        if "ownerFullName" not in out:
            for parent_key in ("ownerProfile", "author", "owner", "user"):
                parent = out.get(parent_key)
                if isinstance(parent, dict):
                    name = (
                        parent.get("full_name")
                        or parent.get("fullName")
                        or parent.get("name")
                    )
                    if name:
                        out["ownerFullName"] = name
                        break
    if "ownerVerified" not in out:
        for alias in ("isVerified", "verified", "is_verified"):
            if alias in out:
                out["ownerVerified"] = out[alias]
                break
        if "ownerVerified" not in out:
            for parent_key in ("ownerProfile", "author", "owner", "user"):
                parent = out.get(parent_key)
                if isinstance(parent, dict):
                    v = parent.get("is_verified") or parent.get("verified")
                    if v is not None:
                        out["ownerVerified"] = v
                        break

    # --- caption ---
    if "caption" not in out:
        for alias in ("description", "text", "body", "captionText"):
            if alias in out:
                out["caption"] = out[alias]
                break
        # caption may be nested as a dict with a 'text' key (older actor versions)
        if "caption" not in out:
            cap_obj = out.get("captionObj") or out.get("edge_media_to_caption")
            if isinstance(cap_obj, dict):
                out["caption"] = cap_obj.get("text") or ""

    return out


def _extract_ig_username(url: str) -> str | None:
    """Extract Instagram username from a profile or post URL.

    Handles patterns like:
      https://www.instagram.com/joao.silva/
      https://www.instagram.com/joao.silva/p/ABC123/
    """
    import re

    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)(?:/|$)", url)
    if m and m.group(1) not in ("p", "reel", "tv", "stories", "explore"):
        return m.group(1)
    return None


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
