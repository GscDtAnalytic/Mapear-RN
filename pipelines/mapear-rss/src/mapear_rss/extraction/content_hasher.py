"""Content hashing for deduplication.

Uses SHA-256 on normalized text to detect duplicate articles
across different sources (syndicated content).
"""

import hashlib
import re
import unicodedata


def normalize_text(text: str) -> str:
    """Normalize text for consistent hashing.

    Strips whitespace, lowercases, removes accents and punctuation
    so that minor formatting differences don't produce different hashes.
    """
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def hash_content(title: str, content: str) -> str:
    """Generate a SHA-256 hash from normalized title + content.

    Args:
        title: Article title.
        content: Article body text.

    Returns:
        Hex-encoded SHA-256 hash string.
    """
    normalized = normalize_text(f"{title} {content}")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
