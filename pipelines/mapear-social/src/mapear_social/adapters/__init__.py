"""Platform-specific Apify adapters (FB / IG / X / TikTok).

Each adapter implements ``PlatformAdapter`` — the single contract the
pipeline iterates over. Add a new platform by dropping a module here
and registering it in ``get_adapter``.
"""

from mapear_social.adapters.base import (
    PlatformAdapter,
    SchemaDriftError,
)
from mapear_social.adapters.facebook import FacebookAdapter
from mapear_social.adapters.instagram import InstagramAdapter
from mapear_social.adapters.tiktok import TikTokAdapter
from mapear_social.adapters.x import XAdapter

_REGISTRY: dict[str, type[PlatformAdapter]] = {
    "facebook": FacebookAdapter,
    "instagram": InstagramAdapter,
    "x": XAdapter,
    "tiktok": TikTokAdapter,
}


def get_adapter(platform: str) -> PlatformAdapter:
    """Return the adapter instance for a given platform slug."""
    try:
        return _REGISTRY[platform]()
    except KeyError as exc:
        raise ValueError(
            f"Unknown platform {platform!r}. Valid: {sorted(_REGISTRY)}"
        ) from exc


__all__ = [
    "FacebookAdapter",
    "InstagramAdapter",
    "PlatformAdapter",
    "SchemaDriftError",
    "TikTokAdapter",
    "XAdapter",
    "get_adapter",
]
