"""
Decomposes resolution confidence into four additive components:

  handle_sim  (weight 0.30) — 1.0 when the input handle exactly matched the
                               target's official platform handle; 0.0 otherwise.
  name_sim    (weight 0.45) — recall of target-name tokens in candidate name
                               (max across canonical + all aliases).
  dict_match  (weight 0.15) — 1.0 if resolution driven by official dictionary
                               (handle or exact name); 0.5 for partial match.
  bio_match   (weight 0.10) — signals from account bio (canonical name, alias,
                               or city).

Replaces the flat 0.98 / 0.90 / 0.97 constants from person_resolver v1.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

IDENTITY_RESOLUTION_VERSION: str = "v2"

WEIGHT_HANDLE: float = 0.30
WEIGHT_NAME: float = 0.45
WEIGHT_DICT: float = 0.15
WEIGHT_BIO: float = 0.10


def _normalize(text: str) -> str:
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).lower().strip()


def _tokens(text: str) -> set[str]:
    return {t for t in _normalize(text).split() if len(t) >= 3}


def _recall(candidate_tokens: set[str], target_tokens: set[str]) -> float:
    """Fraction of target tokens found in candidate (recall orientation).

    More robust than Jaccard for page names with extra words, e.g.
    "Paulinho Freire – Prefeito de Natal" still gets recall=1.0 against
    canonical "Paulinho Freire".
    """
    if not target_tokens:
        return 0.0
    return len(candidate_tokens & target_tokens) / len(target_tokens)


def _name_similarity(
    candidate: str,
    target_name: str,
    aliases: tuple[str, ...],
) -> float:
    """Max recall of target tokens in candidate, across canonical name + aliases."""
    c_tokens = _tokens(candidate)
    if not c_tokens:
        return 0.0
    best = _recall(c_tokens, _tokens(target_name))
    for alias in aliases:
        s = _recall(c_tokens, _tokens(alias))
        if s > best:
            best = s
    return round(best, 4)


def _bio_score(
    bio: str | None,
    target_name: str,
    target_aliases: tuple[str, ...],
    target_city: str,
) -> float:
    if not bio:
        return 0.0
    norm_bio = _normalize(bio)
    if _normalize(target_name) in norm_bio:
        return 1.0
    for alias in target_aliases:
        if _normalize(alias) in norm_bio:
            return 0.8
    if target_city and _normalize(target_city) in norm_bio:
        return 0.5
    return 0.0


@dataclass(frozen=True)
class ConfidenceBreakdown:
    handle_sim: float
    name_sim: float
    dict_match: float
    bio_match: float
    total: float

    def as_dict(self) -> dict[str, float]:
        return {
            "handle_sim": self.handle_sim,
            "name_sim": self.name_sim,
            "dict_match": self.dict_match,
            "bio_match": self.bio_match,
            "total": self.total,
        }


class ResolutionConfidenceScorer:
    """Compute a calibrated, per-observation confidence score."""

    def score(
        self,
        *,
        input_name: str,
        input_handle: str | None = None,
        page_name: str | None = None,
        bio: str | None = None,
        target_name: str,
        target_aliases: tuple[str, ...] = (),
        target_city: str = "",
        handle_matched: bool = False,
    ) -> ConfidenceBreakdown:
        """
        Args:
            input_name:     NER mention text or handle text.
            input_handle:   Normalized platform handle (already stripped of @ / URLs).
            page_name:      Account display name (e.g. "Paulinho Freire – Prefeito").
                            Best available name signal; preferred over input_name.
            bio:            Account bio / description (optional).
            target_name:    Canonical name from rn_targets.csv.
            target_aliases: Aliases from rn_targets.csv.
            target_city:    Canonical city (used in bio check).
            handle_matched: True when input_handle hit this target's official handle.
        """
        handle_sim = 1.0 if handle_matched else 0.0

        name_for_sim = page_name or input_name
        name_sim = _name_similarity(name_for_sim, target_name, target_aliases)

        if handle_matched:
            dict_match = 1.0
        else:
            norm_input = _normalize(name_for_sim)
            norm_target = _normalize(target_name)
            norm_aliases = [_normalize(a) for a in target_aliases]
            if norm_input == norm_target or norm_input in norm_aliases:
                dict_match = 1.0
            elif _recall(_tokens(name_for_sim), _tokens(target_name)) >= 0.5:
                dict_match = 0.5
            else:
                dict_match = 0.0

        bio_m = _bio_score(bio, target_name, target_aliases, target_city)

        total = (
            WEIGHT_HANDLE * handle_sim
            + WEIGHT_NAME * name_sim
            + WEIGHT_DICT * dict_match
            + WEIGHT_BIO * bio_m
        )
        total = round(min(max(total, 0.0), 1.0), 4)

        return ConfidenceBreakdown(
            handle_sim=handle_sim,
            name_sim=name_sim,
            dict_match=dict_match,
            bio_match=round(bio_m, 4),
            total=total,
        )
