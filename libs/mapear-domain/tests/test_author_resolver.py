"""Unit tests for the cross-platform author resolver — Eixo 3 v2b."""

from __future__ import annotations

import pytest

from mapear_domain.entity_resolution.author_resolver import (
    IDENTITY_RESOLUTION_AUTHOR_VERSION,
    AuthorKey,
    Thresholds,
    blocking_keys,
    jaro_winkler,
    normalize_display_name,
    normalize_handle,
    resolve_personas,
    score_pair,
)

# --- Normalisation --------------------------------------------------------


def test_normalize_handle_strips_decorators_and_case() -> None:
    assert normalize_handle("@Zoey.Silva") == "zoeysilva"
    assert normalize_handle("zoey_silva") == "zoeysilva"
    assert normalize_handle("ZOEY-SILVA") == "zoeysilva"
    assert normalize_handle("  zoey silva  ") == "zoeysilva"


def test_normalize_handle_folds_diacritics() -> None:
    # NFKD + combining-mark strip — café/cafe are the same identity for ER.
    assert normalize_handle("café.do.rn") == "cafedorn"
    assert normalize_handle("João_Silva") == "joaosilva"


def test_normalize_handle_handles_empty() -> None:
    assert normalize_handle("") == ""
    assert normalize_handle("   ") == ""


def test_normalize_display_name_returns_none_for_empty() -> None:
    assert normalize_display_name(None) is None
    assert normalize_display_name("") is None
    assert normalize_display_name("   ") is None


def test_normalize_display_name_collapses_whitespace() -> None:
    assert normalize_display_name("  João   Silva  ") == "joão silva"


# --- Jaro-Winkler ----------------------------------------------------------


def test_jaro_winkler_identical() -> None:
    assert jaro_winkler("foo", "foo") == 1.0


def test_jaro_winkler_empty() -> None:
    assert jaro_winkler("", "foo") == 0.0
    assert jaro_winkler("foo", "") == 0.0


def test_jaro_winkler_prefix_boost() -> None:
    # Both strings start with "mart" — JW should be noticeably higher
    # than plain Jaro thanks to the 4-char prefix boost.
    score = jaro_winkler("martha", "marhta")
    assert 0.9 < score < 1.0


def test_jaro_winkler_no_overlap() -> None:
    score = jaro_winkler("abcd", "wxyz")
    assert score == 0.0


# --- Blocking --------------------------------------------------------------


def test_blocking_handle_prefix() -> None:
    keys = blocking_keys({"platform": "facebook", "author_id": "zoey.silva"})
    assert "h:zoey" in keys


def test_blocking_display_name_token() -> None:
    keys = blocking_keys(
        {
            "platform": "facebook",
            "author_id": "user_x",
            "display_name": "Maria Santos",
        }
    )
    assert "d:maria" in keys


def test_blocking_content_hash() -> None:
    keys = blocking_keys(
        {
            "platform": "x",
            "author_id": "anyone",
            "content_hashes": ["h1", "h2"],
        }
    )
    assert "c:h1" in keys
    assert "c:h2" in keys


# --- Pairwise scoring ------------------------------------------------------


def test_score_pair_identical_handles_match() -> None:
    a = {"platform": "facebook", "author_id": "zoey", "display_name": "Zoey"}
    b = {"platform": "instagram", "author_id": "zoey", "display_name": "Zoey"}
    score = score_pair(a, b)
    assert score.decision == "match"
    assert score.handle_similarity == 1.0
    assert score.confidence > 0.9


def test_score_pair_ambiguous_when_display_missing() -> None:
    a = {"platform": "facebook", "author_id": "zoey", "display_name": None}
    b = {"platform": "instagram", "author_id": "zoey", "display_name": None}
    score = score_pair(a, b)
    assert score.decision == "ambiguous"


def test_score_pair_no_match_for_unrelated_handles() -> None:
    a = {"platform": "facebook", "author_id": "alpha", "display_name": "Alpha"}
    b = {"platform": "instagram", "author_id": "omega", "display_name": "Omega"}
    score = score_pair(a, b)
    assert score.decision == "no_match"


def test_score_pair_content_hash_bridge() -> None:
    a = {
        "platform": "facebook",
        "author_id": "acct_a1",
        "display_name": "Profile A",
        "content_hashes": ["h1", "h2"],
    }
    b = {
        "platform": "instagram",
        "author_id": "acct_a2",
        "display_name": "Profile B",
        "content_hashes": ["h1", "h2"],
    }
    score = score_pair(a, b)
    assert score.decision == "match"
    assert score.content_hash_overlap == 2


def test_score_pair_enumerated_handles_downgraded() -> None:
    """politico_a vs politico_b are not the same person — must be ambiguous."""
    a = {
        "platform": "facebook",
        "author_id": "politico_a",
        "display_name": "Político A",
    }
    b = {
        "platform": "instagram",
        "author_id": "politico_b",
        "display_name": "Político B",
    }
    score = score_pair(a, b)
    assert score.decision == "ambiguous"


def test_score_pair_enumerated_overridden_by_content_overlap() -> None:
    a = {
        "platform": "facebook",
        "author_id": "politico_a",
        "display_name": "Político A",
        "content_hashes": ["h1"],
    }
    b = {
        "platform": "instagram",
        "author_id": "politico_b",
        "display_name": "Político B",
        "content_hashes": ["h1"],
    }
    score = score_pair(a, b)
    assert score.decision == "match"


def test_score_pair_threshold_override() -> None:
    a = {"platform": "facebook", "author_id": "zoey", "display_name": "Zoey"}
    b = {"platform": "instagram", "author_id": "zoey", "display_name": "Zoey"}
    strict = Thresholds(handle_similarity=0.99, display_name_similarity=0.99)
    score = score_pair(a, b, thresholds=strict)
    assert score.decision == "match"  # identical strings hit 1.0


# --- Resolve personas (clustering + survivorship) -------------------------


def test_resolve_emits_no_personas_for_singletons() -> None:
    personas = resolve_personas([{"platform": "facebook", "author_id": "solo"}])
    assert personas == []


def test_resolve_same_platform_pairs_never_merged() -> None:
    records = [
        {"platform": "x", "author_id": "bot1", "display_name": "Bot 1"},
        {"platform": "x", "author_id": "bot2", "display_name": "Bot 2"},
    ]
    personas = resolve_personas(records)
    assert personas == []


def test_resolve_cross_platform_match() -> None:
    records = [
        {"platform": "facebook", "author_id": "zoey", "display_name": "Zoey"},
        {"platform": "instagram", "author_id": "zoey", "display_name": "Zoey"},
    ]
    personas = resolve_personas(records)
    assert len(personas) == 1
    p = personas[0]
    assert {(m.platform, m.author_id) for m in p.members} == {
        ("facebook", "zoey"),
        ("instagram", "zoey"),
    }
    assert p.confidence == 1.0


def test_resolve_three_platform_persona_via_transitive_closure() -> None:
    records = [
        {
            "platform": "facebook",
            "author_id": "carlos.oficial",
            "display_name": "Carlos",
        },
        {
            "platform": "instagram",
            "author_id": "carlos.oficial",
            "display_name": "Carlos",
        },
        {"platform": "x", "author_id": "carlosoficial", "display_name": "Carlos"},
    ]
    personas = resolve_personas(records)
    assert len(personas) == 1
    assert len(personas[0].members) == 3


def test_resolve_emits_two_distinct_personas() -> None:
    records = [
        {"platform": "facebook", "author_id": "alpha", "display_name": "Alpha"},
        {"platform": "instagram", "author_id": "alpha", "display_name": "Alpha"},
        {"platform": "facebook", "author_id": "beta", "display_name": "Beta"},
        {"platform": "instagram", "author_id": "beta", "display_name": "Beta"},
    ]
    personas = resolve_personas(records)
    assert len(personas) == 2
    member_sets = {
        frozenset((m.platform, m.author_id) for m in p.members) for p in personas
    }
    assert member_sets == {
        frozenset({("facebook", "alpha"), ("instagram", "alpha")}),
        frozenset({("facebook", "beta"), ("instagram", "beta")}),
    }


def test_resolve_persona_id_is_deterministic_across_runs() -> None:
    records = [
        {"platform": "facebook", "author_id": "zoey", "display_name": "Zoey"},
        {"platform": "instagram", "author_id": "zoey", "display_name": "Zoey"},
    ]
    p1 = resolve_personas(records)[0]
    p2 = resolve_personas(list(reversed(records)))[0]
    assert p1.persona_id == p2.persona_id
    assert p1.members == p2.members


def test_resolve_canonical_handle_is_lex_smallest() -> None:
    records = [
        {"platform": "instagram", "author_id": "zoey", "display_name": "Zoey"},
        {"platform": "facebook", "author_id": "zoey", "display_name": "Zoey"},
    ]
    personas = resolve_personas(records)
    assert len(personas) == 1
    # AuthorKey order sorts by platform then author_id — facebook < instagram.
    assert personas[0].members[0].platform == "facebook"
    assert personas[0].canonical_handle == "zoey"


def test_resolve_resolution_version_is_stamped() -> None:
    records = [
        {"platform": "facebook", "author_id": "zoey", "display_name": "Zoey"},
        {"platform": "instagram", "author_id": "zoey", "display_name": "Zoey"},
    ]
    personas = resolve_personas(records)
    assert personas[0].resolution_version == IDENTITY_RESOLUTION_AUTHOR_VERSION


def test_resolve_evidence_carries_pair_scores() -> None:
    records = [
        {"platform": "facebook", "author_id": "zoey", "display_name": "Zoey"},
        {"platform": "instagram", "author_id": "zoey", "display_name": "Zoey"},
    ]
    personas = resolve_personas(records)
    assert len(personas[0].evidence) == 1
    assert personas[0].evidence[0].decision == "match"


def test_resolve_distinct_homonyms_left_apart() -> None:
    records = [
        {
            "platform": "facebook",
            "author_id": "joao.silva",
            "display_name": "João Silva",
        },
        {
            "platform": "instagram",
            "author_id": "joao.silva",
            "display_name": "Outra Pessoa",
        },
    ]
    personas = resolve_personas(records)
    assert personas == []


def test_resolve_dedups_same_author_key_records() -> None:
    """Two activations for the same (platform, author_id) are one author."""
    records = [
        {"platform": "facebook", "author_id": "zoey", "display_name": None},
        {"platform": "facebook", "author_id": "zoey", "display_name": "Zoey"},
        {"platform": "instagram", "author_id": "zoey", "display_name": "Zoey"},
    ]
    personas = resolve_personas(records)
    assert len(personas) == 1
    # The collapsed FB record picked up the display_name from its
    # second occurrence — otherwise the JW comparison against IG would
    # fail name_match and the pair would land in AMBIGUOUS.
    assert len(personas[0].members) == 2


def test_resolve_threshold_override_can_block_match() -> None:
    records = [
        {"platform": "facebook", "author_id": "zoey", "display_name": "Zoey"},
        {"platform": "instagram", "author_id": "zoey.bsb", "display_name": "Zoey"},
    ]
    relaxed = Thresholds(handle_similarity=0.7, display_name_similarity=0.7)
    strict = Thresholds(handle_similarity=0.99, display_name_similarity=0.99)
    # Relaxed should merge; strict should not.
    assert len(resolve_personas(records, thresholds=relaxed)) == 1
    assert resolve_personas(records, thresholds=strict) == []


def test_resolve_persona_member_tuple_is_sorted() -> None:
    records = [
        {"platform": "instagram", "author_id": "zoey", "display_name": "Zoey"},
        {"platform": "facebook", "author_id": "zoey", "display_name": "Zoey"},
    ]
    personas = resolve_personas(records)
    members = personas[0].members
    assert members == tuple(sorted(members))


def test_score_pair_verified_agreement_propagates() -> None:
    a = {
        "platform": "facebook",
        "author_id": "zoey",
        "display_name": "Zoey",
        "verified": True,
    }
    b = {
        "platform": "instagram",
        "author_id": "zoey",
        "display_name": "Zoey",
        "verified": True,
    }
    score = score_pair(a, b)
    assert score.verified_agreement is True


def test_author_key_ordering() -> None:
    """AuthorKey sorts by platform then author_id so canonical pairs are stable."""
    ka = AuthorKey(platform="facebook", author_id="b")
    kb = AuthorKey(platform="instagram", author_id="a")
    assert ka < kb


def test_resolve_raises_on_missing_required_field() -> None:
    with pytest.raises(KeyError):
        resolve_personas([{"author_id": "no_platform"}])
