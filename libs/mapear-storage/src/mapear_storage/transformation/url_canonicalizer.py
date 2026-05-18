"""URL canonicalization for cross-feed deduplication.

Strips tracking parameters and normalizes URLs so that the same article
linked from multiple feeds produces a single canonical form.
"""

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        # UTM
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "utm_id",
        "utm_cid",
        # Platform click IDs
        "fbclid",
        "gclid",
        "dclid",
        "yclid",
        "msclkid",
        # Email marketing
        "mc_eid",
        "mc_cid",
        # Generic referral signals
        "ref",
        "via",
        "_ga",
        "_gl",
        # Portuguese-language portals
        "origem",
        "canal",
        "campanha",
    }
)


def canonicalize_url(url: str) -> str:
    """Return a canonical URL suitable for cross-feed deduplication.

    Transforms:
    - Lowercases scheme and host
    - Strips default ports (80/http, 443/https)
    - Removes tracking and UTM query parameters
    - Removes trailing slash from non-root paths
    - Sorts remaining query parameters for consistency
    """
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return url.lower().rstrip("/")

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    # Strip default ports
    if ":" in netloc:
        host, port = netloc.rsplit(":", 1)
        if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
            netloc = host

    # Preserve root slash; strip trailing slash from other paths
    path = parsed.path.rstrip("/") or "/"

    # Remove tracking params; sort the rest for determinism
    qs = parse_qs(parsed.query, keep_blank_values=False)
    clean_qs = {k: v for k, v in qs.items() if k.lower() not in _TRACKING_PARAMS}
    query = urlencode(sorted(clean_qs.items()), doseq=True)

    return urlunparse((scheme, netloc, path, "", query, ""))
