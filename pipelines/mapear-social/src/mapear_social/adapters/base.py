"""Platform adapter contract.

Each social platform (Facebook, Instagram, X) is handled by a subclass
of ``PlatformAdapter`` that knows:

1. Which Apify actor to invoke (``actor_id``).
2. How to build the actor input from the rn_targets seed
   (``build_input``) — per-target handle mapping, limits, filters.
3. How to map one Apify dataset item to our internal ``SocialPost``
   model (``parse_item``).
4. What the expected Apify schema version is (``expected_schema_version``);
   mismatch triggers ``SchemaDriftError`` — same pattern as alert A-01
   (YT schema drift incident 2026-04-18, BL-11).
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from mapear_domain.entity_resolution import Target
from mapear_social.models import SocialPost


class SchemaDriftError(Exception):
    """Raised when an Apify item does not match the expected schema version.

    The actor silently changing output shape is the #1 root cause of
    parse failures in production (cf. BL-11). Adapters assert on a set
    of required keys per major schema_version and surface this error
    verbatim so the alerting filter matches ``"schema drift"``.
    """


class PlatformAdapter(ABC):
    """Contract every platform adapter implements."""

    #: Apify actor slug — e.g. ``apify/facebook-posts-scraper``.
    actor_id: str

    #: Platform tag persisted to ``post_id`` prefix and ``platform`` column.
    platform: str

    @abstractmethod
    def build_input(
        self,
        targets: list[Target],
        *,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        """Return the JSON body to POST to ``/v2/acts/{actor}/runs``.

        Each adapter extracts the platform-specific handle from the
        target (e.g. ``target.facebook_page``) and composes a minimal
        input that respects the actor's own schema.

        When ``since`` is provided, adapters must translate it into the
        actor's native temporal filter (``onlyPostsNewerThan``,
        ``oldestPostDateUnified``, …) so Apify returns only posts newer
        than the cutoff — the filter that actually reduces billing,
        since Apify charges per item scraped, not per item persisted.
        """

    @abstractmethod
    def parse_item(
        self, raw: dict[str, Any], actor_run_id: str, ingestion_run_id: str
    ) -> SocialPost:
        """Convert one Apify dataset item into a ``SocialPost``.

        Raises ``SchemaDriftError`` if required fields are missing or
        incompatible with ``expected_schema_version``.
        """

    @abstractmethod
    def expected_schema_version(self) -> int:
        """Major schema version this adapter was written against.

        Bump alongside ``parse_item`` whenever the actor's output
        shape changes in a breaking way.
        """

    @abstractmethod
    def targets_with_handle(self, targets: list[Target]) -> list[Target]:
        """Filter the seed list to targets that have a usable handle.

        Silent drop of a target is worse than skipping the run entirely
        for that platform — this method exists so the pipeline can log
        exactly who got dropped and surface it in metrics.
        """

    def expand_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pre-process raw actor items before parsing.

        Default: identity (no expansion). Override in adapters where the
        actor returns container-level items that must be flattened into
        individual post items (e.g. instagram-profile-scraper returns one
        profile item per account with posts nested under latestPosts).
        """
        return items

    # --- Helpers shared by every adapter ---

    @staticmethod
    def compute_content_hash(platform: str, post_id_raw: str, text: str) -> str:
        """Stable sha256 for dedup / cache keying.

        Includes platform + id + text so a re-post with the same id but
        different text (edit) is treated as a new version downstream.
        """
        digest = hashlib.sha256()
        digest.update(platform.encode("utf-8"))
        digest.update(b":")
        digest.update(post_id_raw.encode("utf-8"))
        digest.update(b":")
        digest.update(text.encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def prefix_post_id(platform: str, raw_id: str) -> str:
        """Namespace a platform-native id for the unified table."""
        short = {"facebook": "fb", "instagram": "ig", "x": "x", "tiktok": "tt"}[
            platform
        ]
        return f"{short}:{raw_id}"

    @staticmethod
    def require_keys(raw: dict[str, Any], keys: tuple[str, ...]) -> None:
        """Schema-drift guard — raises ``SchemaDriftError`` if a key is missing.

        Used by every adapter's ``parse_item`` to fail loud when the
        actor changes its output shape.
        """
        missing = [k for k in keys if k not in raw]
        if missing:
            available = sorted(raw.keys())
            raise SchemaDriftError(
                f"schema drift: missing keys {missing!r} — "
                f"item has {len(available)} keys: {available!r}"
            )
