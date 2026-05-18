"""Resolve raw political mentions to canonical person_id with scope_status.

The resolver is the single source of truth for the electoral scope filter:
who counts as a monitored target (mayor of an RN city, the incumbent
governor, or an officially declared governor candidate). Anything that
does not resolve to a target with sufficient confidence is marked
``OUT_OF_SCOPE`` and must NOT reach the Gold layer.

Confidence tiers (calibrated for RSS/social mentions):

* >= 0.85 — exact match on official social handle, canonical name, or alias.
* 0.70 .. 0.85 — substring match corroborated by contextual signal
  (e.g. mention of the target's city, party, or role).
* 0.40 .. 0.70 — ambiguous match (substring only, no corroboration, or
  multiple candidates with similar scores).
* < 0.40 — no plausible target.

The scope_status decision uses these tiers, not raw thresholds in the
caller, so that calibration changes happen in one place.
"""

import csv
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from mapear_domain.entity_resolution.confidence_scorer import (
    IDENTITY_RESOLUTION_VERSION,
    ConfidenceBreakdown,
    ResolutionConfidenceScorer,
)

if TYPE_CHECKING:
    from mapear_domain.region import Region

_targets_seed_path: Path | None = None


def set_targets_seed_path(path: Path) -> None:
    """Override the default rn_targets.csv path (called by each ETL on boot)."""
    global _targets_seed_path
    _targets_seed_path = path


def _resolve_seed_path() -> Path:
    if _targets_seed_path is not None:
        return _targets_seed_path
    candidates = [
        Path("dbt/seeds/rn_targets.csv"),
        Path("../dbt/seeds/rn_targets.csv"),
        Path(__file__).resolve().parents[4] / "dbt" / "seeds" / "rn_targets.csv",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path("dbt/seeds/rn_targets.csv")


class ScopeStatus(str, Enum):
    IN_SCOPE = "IN_SCOPE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class Target:
    """A canonical monitored political person."""

    person_id: str
    name: str
    role: str
    party: str
    city: str
    aliases: tuple[str, ...] = ()
    facebook_page: str = ""
    instagram_username: str = ""
    x_handle: str = ""
    tiktok_handle: str = ""
    is_incumbent: bool = False


@dataclass
class ResolutionResult:
    """Outcome of attempting to resolve a mention to a canonical target."""

    person_id: str | None
    canonical_name: str | None
    role: str | None
    confidence: float
    # DEPRECATED (V2): use author_in_scope for boolean checks
    scope_status: ScopeStatus
    matched_signal: str = ""
    candidates: list[str] = field(default_factory=list)
    confidence_breakdown: ConfidenceBreakdown | None = None
    identity_resolution_version: str = IDENTITY_RESOLUTION_VERSION

    @property
    def author_in_scope(self) -> bool:
        """True when IN_SCOPE. Canonical boolean form of scope_status."""
        return self.scope_status == ScopeStatus.IN_SCOPE


_STOPWORDS = frozenset(
    {
        "de",
        "do",
        "da",
        "dos",
        "das",
        "e",
        "dr",
        "dra",
        "sr",
        "sra",
        "prof",
        "prefeito",
        "prefeita",
        "prefeitura",
        "governador",
        "governadora",
        "candidato",
        "candidata",
        "ex",
    }
)


def _normalize(text: str) -> str:
    """Lowercase + strip accents — keeps comparison stable across encodings."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    no_accents = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return no_accents.lower().strip()


def _tokens(text: str) -> set[str]:
    """Significant tokens (≥3 chars, no stopwords) for fuzzy overlap."""
    return {t for t in _normalize(text).split() if len(t) >= 3 and t not in _STOPWORDS}


def _strip_handle(handle: str) -> str:
    """Normalize a social handle: drop @, drop URL prefixes, lowercase."""
    h = handle.strip().lower()
    h = h.removeprefix("@")
    for prefix in (
        "https://facebook.com/",
        "https://www.facebook.com/",
        "https://instagram.com/",
        "https://www.instagram.com/",
        "https://twitter.com/",
        "https://x.com/",
        "https://tiktok.com/@",
        "https://www.tiktok.com/@",
        "https://tiktok.com/",
        "https://www.tiktok.com/",
    ):
        if h.startswith(prefix):
            h = h.removeprefix(prefix)
    return h.rstrip("/")


class PersonResolver:
    """Map mentions to canonical person_id; gate the IN_SCOPE/OUT_OF_SCOPE decision."""

    IN_SCOPE_THRESHOLD = 0.70
    AMBIGUOUS_THRESHOLD = 0.40

    def __init__(
        self,
        targets: list[Target] | None = None,
        region: "Region | None" = None,
    ) -> None:
        # Resolution precedence: explicit targets → region.politicians → CSV load.
        if targets is not None:
            self._targets = targets
        elif region is not None:
            self._targets = self._targets_from_region(region)
        else:
            self._targets = self._load()
        self._scorer = ResolutionConfidenceScorer()
        self._index_handles()
        self._index_names()

    @staticmethod
    def _targets_from_region(region: "Region") -> list[Target]:
        """Build Targets from Region.politicians — Region DI path."""
        targets: list[Target] = []
        for p in region.politicians:
            handles = p.handles or {}
            targets.append(
                Target(
                    person_id=p.person_id,
                    name=p.name,
                    role=p.role,
                    party=p.party or "",
                    city=p.city or "",
                    aliases=tuple(p.aliases),
                    facebook_page=handles.get("facebook", ""),
                    instagram_username=handles.get("instagram", ""),
                    x_handle=handles.get("x", "") or handles.get("twitter", ""),
                    tiktok_handle=handles.get("tiktok", ""),
                    is_incumbent=p.is_incumbent,
                )
            )
        logger.info(
            "Loaded {n} targets from Region(id={rid})",
            n=len(targets),
            rid=region.id,
        )
        return targets

    @classmethod
    def _load(cls) -> list[Target]:
        seed_path = _resolve_seed_path()
        if not seed_path.exists():
            raise FileNotFoundError(
                f"rn_targets seed not found at {seed_path}. "
                "Expected at dbt/seeds/rn_targets.csv relative to the monorepo root. "
                "Call set_targets_seed_path(path) before instantiating PersonResolver "
                "when running outside the monorepo root."
            )
        targets: list[Target] = []
        with open(seed_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                aliases_raw = row.get("aliases", "") or ""
                aliases = tuple(a.strip() for a in aliases_raw.split(";") if a.strip())
                targets.append(
                    Target(
                        person_id=row["person_id"],
                        name=row["name"],
                        role=row["role"],
                        party=row.get("party", ""),
                        city=row.get("city", ""),
                        aliases=aliases,
                        facebook_page=row.get("facebook_page", ""),
                        instagram_username=row.get("instagram_username", ""),
                        x_handle=row.get("x_handle", ""),
                        tiktok_handle=row.get("tiktok_handle", ""),
                        is_incumbent=str(row.get("is_incumbent", "")).lower() == "true",
                    )
                )
        logger.info("Loaded {n} targets from {path}", n=len(targets), path=seed_path)
        return targets

    def _index_handles(self) -> None:
        self._fb_index: dict[str, Target] = {}
        self._ig_index: dict[str, Target] = {}
        self._x_index: dict[str, Target] = {}
        self._tt_index: dict[str, Target] = {}
        for t in self._targets:
            if t.facebook_page:
                self._fb_index[_strip_handle(t.facebook_page)] = t
            if t.instagram_username:
                self._ig_index[_strip_handle(t.instagram_username)] = t
            if t.x_handle:
                self._x_index[_strip_handle(t.x_handle)] = t
            if t.tiktok_handle:
                self._tt_index[_strip_handle(t.tiktok_handle)] = t

    def _index_names(self) -> None:
        self._name_index: dict[str, Target] = {}
        self._alias_index: dict[str, Target] = {}
        self._target_tokens: dict[str, set[str]] = {}
        for t in self._targets:
            self._name_index[_normalize(t.name)] = t
            for alias in t.aliases:
                self._alias_index[_normalize(alias)] = t
            tokens = _tokens(t.name)
            for alias in t.aliases:
                tokens |= _tokens(alias)
            self._target_tokens[t.person_id] = tokens

    def list_targets(self) -> list[Target]:
        return list(self._targets)

    def get(self, person_id: str) -> Target | None:
        for t in self._targets:
            if t.person_id == person_id:
                return t
        return None

    def get_all_target_names(self) -> set[str]:
        """Canonical names (used by NER + dbt enrichment)."""
        return {t.name for t in self._targets}

    def get_target_names_by_role(self, role: str) -> set[str]:
        return {t.name for t in self._targets if t.role == role}

    def resolve(
        self,
        name: str,
        context: str = "",
        platform: str | None = None,
        handle: str | None = None,
        page_name: str | None = None,
        bio: str | None = None,
    ) -> ResolutionResult:
        """Resolve a mention to a canonical person.

        Args:
            name: Raw mention text (e.g. NER output, post author, page name).
            context: Surrounding text used to corroborate ambiguous matches
                (post body, article content, surrounding sentence).
            platform: One of facebook|instagram|x|rss; enables
                handle-index lookup when paired with ``handle``.
            handle: Platform-native identifier (page handle, username);
                strongest signal when present.
            page_name: Account display name (e.g. "Paulinho Freire – Prefeito").
                Used to compute name-similarity component of confidence.
            bio: Account bio / description. Used for bio_match component.
        """
        # Tier 1 — handle match (strongest signal, exact)
        if handle and platform:
            target = self._lookup_handle(handle, platform)
            if target is not None:
                breakdown = self._scorer.score(
                    input_name=name,
                    input_handle=_strip_handle(handle),
                    page_name=page_name,
                    bio=bio,
                    target_name=target.name,
                    target_aliases=target.aliases,
                    target_city=target.city,
                    handle_matched=True,
                )
                return ResolutionResult(
                    person_id=target.person_id,
                    canonical_name=target.name,
                    role=target.role,
                    confidence=breakdown.total,
                    scope_status=ScopeStatus.IN_SCOPE,
                    matched_signal=f"handle:{platform}",
                    confidence_breakdown=breakdown,
                )

        norm_name = _normalize(name)
        if not norm_name:
            return ResolutionResult(
                person_id=None,
                canonical_name=None,
                role=None,
                confidence=0.0,
                scope_status=ScopeStatus.OUT_OF_SCOPE,
                matched_signal="empty_name",
            )

        norm_context = _normalize(context)

        # Tier 2 — exact canonical or alias match
        target = self._name_index.get(norm_name) or self._alias_index.get(norm_name)
        if target is not None:
            breakdown = self._scorer.score(
                input_name=name,
                input_handle=_strip_handle(handle) if handle else None,
                page_name=page_name,
                bio=bio,
                target_name=target.name,
                target_aliases=target.aliases,
                target_city=target.city,
                handle_matched=False,
            )
            if not norm_context:
                return ResolutionResult(
                    person_id=target.person_id,
                    canonical_name=target.name,
                    role=target.role,
                    confidence=0.90,
                    scope_status=ScopeStatus.IN_SCOPE,
                    matched_signal="canonical_or_alias_exact",
                    confidence_breakdown=breakdown,
                )
            corrob_score, corrob_signal = self._score_with_context(target, norm_context)
            if corrob_score >= 0.60:
                return ResolutionResult(
                    person_id=target.person_id,
                    canonical_name=target.name,
                    role=target.role,
                    confidence=min(corrob_score + 0.20, 0.97),
                    scope_status=ScopeStatus.IN_SCOPE,
                    matched_signal=f"canonical_exact+{corrob_signal}",
                    confidence_breakdown=breakdown,
                )
            # Exact canonical match BUT context fails to corroborate → homonym risk
            return ResolutionResult(
                person_id=None,
                canonical_name=None,
                role=None,
                confidence=0.55,
                scope_status=ScopeStatus.AMBIGUOUS,
                matched_signal="canonical_exact_homonym_risk",
                candidates=[target.person_id],
                confidence_breakdown=breakdown,
            )

        # Tier 3 — token overlap match with context corroboration
        query_tokens = _tokens(name)
        substring_hits = [
            t for t in self._targets if query_tokens & self._target_tokens[t.person_id]
        ]

        if not substring_hits:
            return ResolutionResult(
                person_id=None,
                canonical_name=None,
                role=None,
                confidence=0.0,
                scope_status=ScopeStatus.OUT_OF_SCOPE,
                matched_signal="no_match",
            )

        scored: list[tuple[Target, float, str]] = []
        for t in substring_hits:
            score, signal = self._score_with_context(t, norm_context)
            scored.append((t, score, signal))

        scored.sort(key=lambda x: x[1], reverse=True)
        best, best_score, best_signal = scored[0]

        best_breakdown = self._scorer.score(
            input_name=name,
            input_handle=_strip_handle(handle) if handle else None,
            page_name=page_name,
            bio=bio,
            target_name=best.name,
            target_aliases=best.aliases,
            target_city=best.city,
            handle_matched=False,
        )

        # Disambiguation: if top two are too close, mark AMBIGUOUS
        if len(scored) > 1 and (best_score - scored[1][1]) < 0.15:
            return ResolutionResult(
                person_id=None,
                canonical_name=None,
                role=None,
                confidence=best_score,
                scope_status=ScopeStatus.AMBIGUOUS,
                matched_signal=f"ambiguous:{best_signal}",
                candidates=[t.person_id for t, _, _ in scored[:3]],
                confidence_breakdown=best_breakdown,
            )

        scope_status = self._classify(best_score)
        return ResolutionResult(
            person_id=best.person_id if scope_status == ScopeStatus.IN_SCOPE else None,
            canonical_name=best.name if scope_status == ScopeStatus.IN_SCOPE else None,
            role=best.role if scope_status == ScopeStatus.IN_SCOPE else None,
            confidence=best_score,
            scope_status=scope_status,
            matched_signal=best_signal,
            candidates=[best.person_id] if scope_status != ScopeStatus.IN_SCOPE else [],
            confidence_breakdown=best_breakdown,
        )

    def resolve_best(
        self,
        mentions: list[str],
        context: str = "",
        platform: str | None = None,
        handle: str | None = None,
        page_name: str | None = None,
        bio: str | None = None,
    ) -> ResolutionResult:
        """Pick the strongest ResolutionResult across a list of mentions.

        ETLs typically extract many candidate person names per piece of
        content (NER + dictionary matching + co-reference). The scope gate
        needs ONE person_id per content row, so this helper collapses the
        list with the following priority:

        1. If any mention resolves IN_SCOPE → return the highest-confidence
           IN_SCOPE result.
        2. Else, if any mention resolves AMBIGUOUS → return the highest-
           confidence AMBIGUOUS result (surfaces the borderline signal for
           human review without leaking it into Gold).
        3. Else → OUT_OF_SCOPE sentinel with the highest OUT_OF_SCOPE
           confidence observed (useful for explainability).

        A handle (when the source exposes one, e.g. future social sources)
        short-circuits the loop with a single handle lookup — strongest
        signal available.

        Returns a single ``ResolutionResult`` with ``OUT_OF_SCOPE`` when
        ``mentions`` is empty.
        """
        if handle and platform:
            single = self.resolve(
                name=handle,
                context=context,
                platform=platform,
                handle=handle,
                page_name=page_name,
                bio=bio,
            )
            if single.scope_status == ScopeStatus.IN_SCOPE:
                return single

        if not mentions:
            return ResolutionResult(
                person_id=None,
                canonical_name=None,
                role=None,
                confidence=0.0,
                scope_status=ScopeStatus.OUT_OF_SCOPE,
                matched_signal="no_mentions",
            )

        results = [
            self.resolve(
                name=m,
                context=context,
                platform=platform,
                page_name=page_name,
                bio=bio,
            )
            for m in mentions
        ]

        in_scope = [r for r in results if r.scope_status == ScopeStatus.IN_SCOPE]
        if in_scope:
            return max(in_scope, key=lambda r: r.confidence)

        ambiguous = [r for r in results if r.scope_status == ScopeStatus.AMBIGUOUS]
        if ambiguous:
            return max(ambiguous, key=lambda r: r.confidence)

        return max(results, key=lambda r: r.confidence)

    def _lookup_handle(self, handle: str, platform: str) -> Target | None:
        h = _strip_handle(handle)
        if platform == "facebook":
            return self._fb_index.get(h)
        if platform == "instagram":
            return self._ig_index.get(h)
        if platform == "x":
            return self._x_index.get(h)
        if platform == "tiktok":
            return self._tt_index.get(h)
        return None

    def _score_with_context(
        self, target: Target, norm_context: str
    ) -> tuple[float, str]:
        """Score a substring candidate against contextual corroboration."""
        if not norm_context:
            return (0.50, "substring_no_context")

        signals: list[str] = []
        score = 0.50

        if target.city and _normalize(target.city) in norm_context:
            score += 0.20
            signals.append("city")

        if target.party and _normalize(target.party) in norm_context:
            score += 0.10
            signals.append("party")

        role_keywords = {
            "mayor": ("prefeito", "prefeita", "prefeitura"),
            "governor": ("governador", "governadora", "governo do estado"),
            "governor_candidate": ("candidato", "candidata", "campanha"),
        }
        for kw in role_keywords.get(target.role, ()):
            if kw in norm_context:
                score += 0.10
                signals.append("role_keyword")
                break

        if target.is_incumbent and "rn" in norm_context.split():
            score += 0.05
            signals.append("rn_token")

        return (min(score, 0.95), "+".join(signals) or "substring_no_corroboration")

    def _classify(self, score: float) -> ScopeStatus:
        if score >= self.IN_SCOPE_THRESHOLD:
            return ScopeStatus.IN_SCOPE
        if score >= self.AMBIGUOUS_THRESHOLD:
            return ScopeStatus.AMBIGUOUS
        return ScopeStatus.OUT_OF_SCOPE
