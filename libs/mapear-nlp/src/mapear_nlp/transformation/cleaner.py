"""Text cleaning for extracted articles.

Normalizes whitespace, removes boilerplate fragments, fixes encoding
issues, and prepares text for NER and sentiment analysis.
"""

import re
import unicodedata

# Padrões de boilerplate comuns em portais de notícia brasileiros
BOILERPLATE_PATTERNS = [
    r"(?i)leia\s+(também|mais)\s*:.*$",
    r"(?i)siga\s+o?\s*\w+\s+no\s+(twitter|instagram|facebook).*$",
    r"(?i)inscreva-se\s+no\s+canal.*$",
    r"(?i)compartilhe\s+esta?\s+(notícia|matéria).*$",
    r"(?i)assine\s+(já|agora|o).*$",
    r"(?i)publicidade\s*$",
    r"(?i)veja\s+também\s*:.*$",
    r"(?i)(foto|imagem|crédito|reprodução)\s*:.*$",
]


def clean_text(text: str) -> str:
    """Clean extracted article text."""
    text = unicodedata.normalize("NFC", text)
    text = _fix_encoding_artifacts(text)
    text = _remove_boilerplate(text)
    text = _normalize_whitespace(text)
    text = _remove_short_lines(text, min_length=30)

    return text.strip()


def _fix_encoding_artifacts(text: str) -> str:
    """Fix common encoding problems from web scraping."""
    replacements = {
        "\u00a0": " ",
        "\u200b": "",
        "\u200c": "",
        "\u200d": "",
        "\ufeff": "",
        "\r\n": "\n",
        "\r": "\n",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _remove_boilerplate(text: str) -> str:
    """Remove common boilerplate lines from Brazilian news portals."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        is_boilerplate = any(
            re.search(pattern, line) for pattern in BOILERPLATE_PATTERNS
        )
        if not is_boilerplate:
            cleaned.append(line)
    return "\n".join(cleaned)


def _normalize_whitespace(text: str) -> str:
    """Collapse multiple spaces and blank lines."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _remove_short_lines(text: str, min_length: int = 30) -> str:
    """Remove lines shorter than min_length (likely captions or noise)."""
    lines = text.split("\n")
    filtered = []
    for line in lines:
        stripped = line.strip()
        if len(stripped) >= min_length or not stripped:
            filtered.append(line)
    return "\n".join(filtered)
