"""Cross-platform author identity resolution — Eixo 3 v2b.

Resolves "the same person on different platforms" — e.g. the FB account
``zoey.silva`` and the IG handle ``zoey_silva`` belong to the same
real-world author. v1+v2a treat them as distinct via the
``(platform, author_id)`` surrogate; v2b lifts that limitation by
mapping each ``(platform, author_id)`` to a stable ``persona_id``.

Counterpart to :mod:`mapear_domain.entity_resolution.person_resolver`
but for *authors* (who is talking), not *targets* (who is being talked
about). The Acxiom 2010 framework is the spine:

1. **Blocking / indexing** — group likely-match authors via normalized
   handle prefix + display-name tokens, avoiding the O(n²) all-pairs
   sweep.
2. **Pairwise comparison** — Jaro-Winkler on handle + display_name,
   verified-flag agreement, shared content_hash bridge.
3. **Classification** — rule-based v2b: MATCH / NO_MATCH /
   AMBIGUOUS. Probabilistic (Fellegi-Sunter) and ML (random forest,
   deep learning) classifiers are v3.
4. **Clustering** — transitive closure of MATCH edges → connected
   components → personas.
5. **Survivorship** — canonical handle = lex-smallest member;
   ``persona_id`` = ``sha1`` over the sorted member tuple (idempotent
   across runs over the same input).

Why only cross-platform pairs
-----------------------------
v2b deliberately compares only authors on *different* platforms.
Merging same-platform accounts is more dangerous: a politician's
personal vs official Instagram, or a fan account vs the real one, are
exactly the cases where false-positive merges cause the most damage
downstream (graph collapses real coordination into a single node). The
gain is small (cross-platform is where the v1 surrogate genuinely
under-counts) and the risk is high. Same-platform deduplication is a
v3 concern with stronger evidence requirements.

Anti-objectives (v2b)
---------------------
  * No probabilistic / Fellegi-Sunter classification.
  * No ML classifier (random forest, GBM, deep).
  * No active-learning loop.
  * No cross-day persona persistence beyond ``persona_id`` stability
    over identical inputs — non-overlapping daily batches will not
    stitch automatically; that is v3.
  * No same-platform deduplication.

See ADR ``docs/decisions/adr-eixo-3-v2b-cross-platform-identity-resolution.md``.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

IDENTITY_RESOLUTION_AUTHOR_VERSION = "v2b.1"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class AuthorKey:
    """Stable per-platform identity.

    Mirrors :class:`mapear_nlp.graph.coactivation.AuthorKey`. Kept local
    so :mod:`mapear_domain` does not import :mod:`mapear_nlp`. Equal-by-
    value with the nlp counterpart; downstream code can interconvert by
    tuple unpack.
    """

    platform: str
    author_id: str


Decision = Literal["match", "no_match", "ambiguous"]


@dataclass(frozen=True)
class PairScore:
    """Pairwise comparison breakdown — every contributing signal recorded.

    Persisted into ``SilverAuthorPersona.evidence`` so an analyst can
    audit *why* two handles ended up in the same persona without
    re-running the engine.
    """

    handle_similarity: float
    display_name_similarity: float | None
    verified_agreement: bool
    content_hash_overlap: int
    city_match: bool | None
    decision: Decision
    confidence: float


@dataclass(frozen=True)
class Persona:
    """One resolved cross-platform identity.

    ``persona_id`` is content-addressed: sha1 over the sorted member
    tuple → same input always yields the same id. Members are sorted
    by ``AuthorKey`` natural order so the tuple is canonical.
    """

    persona_id: str
    members: tuple[AuthorKey, ...]
    canonical_handle: str
    canonical_display_name: str | None
    confidence: float
    evidence: tuple[PairScore, ...]
    resolution_version: str = IDENTITY_RESOLUTION_AUTHOR_VERSION


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_HANDLE_STRIP = "@._- "


def normalize_handle(handle: str) -> str:
    """Canonicalise a social handle for comparison.

    Lowercases, NFKD-normalises, strips diacritics, and removes the
    common decorators (``@``, dots, underscores, dashes, whitespace).
    ``Zoey.Silva``, ``zoey_silva``, ``@zoey-silva``, ``ZoeySilva`` and
    ``café.do.rn`` all collapse predictably (the last to ``cafedorn``).

    The normalised form is used both for blocking and as the basis of
    Jaro-Winkler similarity. Stripping diacritics is standard ER hygiene
    on Latin-script handles — ``café``/``cafe`` and ``joão``/``joao``
    are the same identity for matching purposes.
    """
    if not handle:
        return ""
    decomposed = unicodedata.normalize("NFKD", handle)
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    h = folded.strip().lower()
    return h.translate({ord(c): None for c in _HANDLE_STRIP})


def normalize_display_name(name: str | None) -> str | None:
    if not name:
        return None
    n = unicodedata.normalize("NFKC", name).strip().lower()
    # Collapse runs of whitespace; preserve word boundaries (no strip
    # of spaces) — display names are multi-token.
    return " ".join(n.split()) or None


# ---------------------------------------------------------------------------
# Jaro-Winkler
# ---------------------------------------------------------------------------


def _jaro(a: str, b: str) -> float:
    """Plain Jaro similarity — bounded matching window + transpositions.

    Implemented inline to avoid a dependency on jellyfish/rapidfuzz.
    O(|a|·|b|) but |handles| << 100 in practice. Returns 0.0 for empty
    inputs.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    len_a, len_b = len(a), len(b)
    window = max(len_a, len_b) // 2 - 1
    if window < 0:
        window = 0
    a_match = [False] * len_a
    b_match = [False] * len_b
    matches = 0
    for i in range(len_a):
        start = max(0, i - window)
        end = min(i + window + 1, len_b)
        for j in range(start, end):
            if b_match[j]:
                continue
            if a[i] != b[j]:
                continue
            a_match[i] = True
            b_match[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    transpositions = 0
    k = 0
    for i in range(len_a):
        if not a_match[i]:
            continue
        while not b_match[k]:
            k += 1
        if a[i] != b[k]:
            transpositions += 1
        k += 1
    t = transpositions / 2
    return (matches / len_a + matches / len_b + (matches - t) / matches) / 3.0


def jaro_winkler(a: str, b: str, *, prefix_scale: float = 0.1) -> float:
    """Jaro-Winkler with the standard 4-char prefix boost.

    Returns the unmodified Jaro score for empty inputs (0.0) and equal
    strings (1.0). ``prefix_scale=0.1`` is the canonical Winkler value;
    keep it fixed unless the eval gold-set is recalibrated.
    """
    jaro = _jaro(a, b)
    if jaro == 0.0 or jaro == 1.0:
        return jaro
    prefix = 0
    for ca, cb in zip(a, b, strict=False):
        if ca != cb:
            break
        prefix += 1
        if prefix == 4:
            break
    return jaro + prefix * prefix_scale * (1.0 - jaro)


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------


def _author_key(record: Mapping[str, Any]) -> AuthorKey:
    return AuthorKey(
        platform=str(record["platform"]),
        author_id=str(record["author_id"]),
    )


def _record_content_hashes(record: Mapping[str, Any]) -> frozenset[str]:
    raw = record.get("content_hashes") or ()
    return frozenset(str(h) for h in raw if h)


def blocking_keys(record: Mapping[str, Any]) -> set[str]:
    """Return the set of blocking buckets a record belongs to.

    Two records are candidate matches iff they share at least one
    blocking key. v2b uses three buckets:

      * ``h:<first-4-chars-of-normalized-handle>`` — prefix block, the
        Acxiom-canonical handle bucket.
      * ``d:<first-token-of-display-name>`` — token-level display-name
        prefix; catches handle-renames where the display name stayed.
      * ``c:<content_hash>`` — exact-content bridge; two accounts that
        post the same text within a day are forced into the same
        candidate bucket regardless of handle similarity.

    The blocking is intentionally generous — false positives at this
    stage are cheap (filtered later by pairwise classification); false
    negatives are expensive (an account that fails to block can never
    be merged).
    """
    keys: set[str] = set()
    norm_handle = normalize_handle(str(record["author_id"]))
    if norm_handle:
        keys.add("h:" + norm_handle[:4])
    display = normalize_display_name(record.get("display_name"))
    if display:
        first_token = display.split(" ", 1)[0]
        if first_token:
            keys.add("d:" + first_token)
    for content_hash in _record_content_hashes(record):
        keys.add("c:" + content_hash)
    return keys


# ---------------------------------------------------------------------------
# Pairwise scoring + classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    # Default calibration is tuned on the bundled gold set in
    # ``mapear-nlp/eval/author_resolution_gold_set.csv`` — JW prefix
    # boost makes plausible "_oficial" suffixes (e.g. prefeito.x vs
    # prefeito_x_oficial) score ~0.91; we land just below that to
    # keep them as MATCH when the display name corroborates. The pair
    # of distinct homonyms with different display names still lands
    # in AMBIGUOUS because the display_name floor (0.90) is not met.
    handle_similarity: float = 0.90
    display_name_similarity: float = 0.90
    min_shared_content: int = 1
    use_content_hash_bridge: bool = True
    # Floor on handle similarity even when a content-hash bridge is
    # present — guards against unrelated accounts that happen to share
    # one piece of content (e.g. a viral meme reposted by both).
    content_bridge_handle_floor: float = 0.60


def _looks_enumerated(norm_a: str, norm_b: str) -> bool:
    """Detect ``politico_a`` vs ``politico_b`` series.

    Same-length, same-prefix handles that differ by exactly one
    character are a classic Acxiom false-positive trap: JW reports
    >0.95 similarity, display names share the politically generic
    base ("Político A" vs "Político B") and the engine merges two
    distinct accounts. Heuristic: if the normalised handles differ
    by exactly one character position and have the same length, flag
    as enumerated and require stronger corroboration (content
    overlap) before promoting to MATCH.

    Equal handles return False — they are the easy case, not an
    enumeration.
    """
    if norm_a == norm_b:
        return False
    if len(norm_a) != len(norm_b):
        return False
    diffs = 0
    for ca, cb in zip(norm_a, norm_b, strict=False):
        if ca != cb:
            diffs += 1
            if diffs > 1:
                return False
    return diffs == 1


def score_pair(
    a: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    thresholds: Thresholds | None = None,
) -> PairScore:
    """Compute pairwise comparison breakdown + classification.

    Assumes ``a`` and ``b`` are on *different* platforms — same-platform
    pairs are filtered upstream in :func:`resolve_personas`. Caller
    that bypasses this and passes same-platform pairs still gets a
    valid score, but interpret with caution.
    """
    if thresholds is None:
        thresholds = Thresholds()
    norm_a = normalize_handle(str(a["author_id"]))
    norm_b = normalize_handle(str(b["author_id"]))
    handle_sim = jaro_winkler(norm_a, norm_b)

    disp_a = normalize_display_name(a.get("display_name"))
    disp_b = normalize_display_name(b.get("display_name"))
    if disp_a and disp_b:
        display_sim: float | None = jaro_winkler(disp_a, disp_b)
    else:
        display_sim = None

    ver_a = a.get("verified")
    ver_b = b.get("verified")
    # Verified agreement is a *soft* signal: two unverified accounts
    # carry no information; one verified + one unverified is a weak
    # disqualifier (a real person with the verified flag is unlikely
    # to operate an unverified parallel account *with the same handle*).
    verified_agreement = bool(ver_a) == bool(ver_b) and (
        ver_a is not None and ver_b is not None
    )

    overlap = len(_record_content_hashes(a) & _record_content_hashes(b))

    city_a = a.get("base_city")
    city_b = b.get("base_city")
    if city_a and city_b:
        city_match: bool | None = (
            str(city_a).strip().lower() == str(city_b).strip().lower()
        )
    else:
        city_match = None

    decision: Decision
    confidence: float

    name_match = (
        display_sim is not None and display_sim >= thresholds.display_name_similarity
    )
    handle_match = handle_sim >= thresholds.handle_similarity
    enumerated = _looks_enumerated(norm_a, norm_b)

    if handle_match and name_match and not enumerated:
        decision = "match"
        confidence = min(1.0, (handle_sim + display_sim) / 2)  # type: ignore[arg-type]
    elif (
        thresholds.use_content_hash_bridge
        and overlap >= thresholds.min_shared_content
        and handle_sim >= thresholds.content_bridge_handle_floor
    ):
        # Content overlap also overrides the enumerated guard — two
        # accounts that post the same content within a day are likely
        # the same person regardless of suffix.
        decision = "match"
        # Bridge confidence weights content overlap. A single shared
        # post lifts the floor; many shared posts saturate quickly.
        bridge_boost = min(0.3, 0.1 * overlap)
        confidence = min(1.0, handle_sim + bridge_boost)
    elif handle_match and (enumerated or not name_match):
        # Strong handle similarity but either enumerated (politico_a /
        # politico_b) or name-divergent — defer to operator review.
        decision = "ambiguous"
        confidence = handle_sim
    else:
        decision = "no_match"
        confidence = handle_sim

    return PairScore(
        handle_similarity=handle_sim,
        display_name_similarity=display_sim,
        verified_agreement=verified_agreement,
        content_hash_overlap=overlap,
        city_match=city_match,
        decision=decision,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Resolution: blocking → pairwise → clustering → survivorship
# ---------------------------------------------------------------------------


def _persona_id(members: tuple[AuthorKey, ...]) -> str:
    payload = json.dumps(
        [[m.platform, m.author_id] for m in members],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _connected_components(
    nodes: Iterable[AuthorKey],
    edges: Iterable[tuple[AuthorKey, AuthorKey]],
) -> list[list[AuthorKey]]:
    """Union-find over MATCH edges → list of components.

    Determinism note: component order and intra-component order are
    *not* canonicalised here; the caller sorts before hashing.
    """
    parent: dict[AuthorKey, AuthorKey] = {n: n for n in nodes}

    def find(x: AuthorKey) -> AuthorKey:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: AuthorKey, y: AuthorKey) -> None:
        rx, ry = find(x), find(y)
        if rx == ry:
            return
        # Tie-break deterministically by AuthorKey order so the root
        # is always the lexicographically smallest member of the
        # component — keeps test snapshots stable.
        if rx < ry:
            parent[ry] = rx
        else:
            parent[rx] = ry

    for a, b in edges:
        if a in parent and b in parent:
            union(a, b)

    groups: dict[AuthorKey, list[AuthorKey]] = defaultdict(list)
    for node in parent:
        groups[find(node)].append(node)
    return list(groups.values())


def resolve_personas(
    records: Iterable[Mapping[str, Any]],
    *,
    thresholds: Thresholds | None = None,
) -> list[Persona]:
    """Run the full Acxiom pipeline over a batch of author records.

    Parameters
    ----------
    records
        Iterable of dict-like rows. Required keys: ``platform``,
        ``author_id``. Optional: ``display_name``, ``verified``,
        ``base_city``, ``content_hashes`` (iterable of strings).
    thresholds
        Override the default classification thresholds — wired from
        ``MAPEAR_ER_*`` settings by the job CLI.

    Returns
    -------
    list[Persona]
        Personas where ``len(members) >= 2``. Authors that did not
        match any cross-platform counterpart are *not* emitted — the
        v1 ``(platform, author_id)`` surrogate already represents them
        unambiguously. Sorted by ``persona_id`` for stable output.
    """
    if thresholds is None:
        thresholds = Thresholds()
    # Dedup records by AuthorKey while merging content_hashes / display
    # names from collapsed duplicates. Two activations from the same
    # (platform, author_id) are one author, not two.
    by_key: dict[AuthorKey, dict[str, Any]] = {}
    for raw in records:
        key = _author_key(raw)
        slot = by_key.get(key)
        if slot is None:
            slot = {
                "platform": key.platform,
                "author_id": key.author_id,
                "display_name": raw.get("display_name"),
                "verified": raw.get("verified"),
                "base_city": raw.get("base_city"),
                "content_hashes": set(_record_content_hashes(raw)),
            }
            by_key[key] = slot
        else:
            if slot.get("display_name") is None and raw.get("display_name"):
                slot["display_name"] = raw["display_name"]
            if slot.get("verified") is None and raw.get("verified") is not None:
                slot["verified"] = raw["verified"]
            if slot.get("base_city") is None and raw.get("base_city"):
                slot["base_city"] = raw["base_city"]
            slot["content_hashes"].update(_record_content_hashes(raw))

    # Materialise records with frozenset content hashes — score_pair
    # expects an immutable set view.
    for slot in by_key.values():
        slot["content_hashes"] = frozenset(slot["content_hashes"])

    # Build the inverted blocking index.
    buckets: dict[str, list[AuthorKey]] = defaultdict(list)
    for key, slot in by_key.items():
        for bkey in blocking_keys(slot):
            buckets[bkey].append(key)

    # Pairwise score within buckets — cross-platform pairs only.
    seen_pairs: set[tuple[AuthorKey, AuthorKey]] = set()
    match_edges: list[tuple[AuthorKey, AuthorKey]] = []
    edge_scores: dict[tuple[AuthorKey, AuthorKey], PairScore] = {}

    for members in buckets.values():
        if len(members) < 2:
            continue
        members_sorted = sorted(members)
        for i in range(len(members_sorted)):
            for j in range(i + 1, len(members_sorted)):
                a, b = members_sorted[i], members_sorted[j]
                if a.platform == b.platform:
                    continue
                pair_key = (a, b)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                score = score_pair(by_key[a], by_key[b], thresholds=thresholds)
                edge_scores[pair_key] = score
                if score.decision == "match":
                    match_edges.append(pair_key)

    components = _connected_components(by_key.keys(), match_edges)

    out: list[Persona] = []
    for component in components:
        if len(component) < 2:
            continue
        members = tuple(sorted(component))
        # Confidence of the persona = weakest match edge inside the
        # component (chain is as strong as the weakest link).
        scores: list[PairScore] = []
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pair = (members[i], members[j])
                if pair in edge_scores and edge_scores[pair].decision == "match":
                    scores.append(edge_scores[pair])
        if not scores:
            # Defensive — only reachable if a non-match edge sneaks
            # into the component. Skip rather than emit a zero-evidence
            # persona.
            continue
        confidence = min(s.confidence for s in scores)
        canonical_key = members[0]
        canonical_handle = canonical_key.author_id
        canonical_display_name = normalize_display_name(
            by_key[canonical_key].get("display_name")
        )
        out.append(
            Persona(
                persona_id=_persona_id(members),
                members=members,
                canonical_handle=canonical_handle,
                canonical_display_name=canonical_display_name,
                confidence=confidence,
                evidence=tuple(scores),
            )
        )

    out.sort(key=lambda p: p.persona_id)
    return out


__all__ = [
    "AuthorKey",
    "Decision",
    "IDENTITY_RESOLUTION_AUTHOR_VERSION",
    "PairScore",
    "Persona",
    "Thresholds",
    "blocking_keys",
    "jaro_winkler",
    "normalize_display_name",
    "normalize_handle",
    "resolve_personas",
    "score_pair",
]
