"""Unit tests for ``mapear_social.coactivation.build_activation_records``."""

from __future__ import annotations

from datetime import UTC, datetime

from mapear_social.coactivation import build_activation_records


def _silver_row(**overrides) -> dict:
    base = {
        "post_id": "x:1",
        "platform": "x",
        "url": "https://example.com/p",
        "author_handle": "alice",
        "author_display_name": "Alice",
        "author_verified": False,
        "text": "post text",
        "published_at": datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
        "extracted_at": datetime(2026, 5, 10, 12, 5, tzinfo=UTC),
        "is_repost": False,
        "is_reply": False,
        "parent_post_id": None,
        "entities": [],
        "mentioned_cities": [],
        "mentioned_mayors": [],
        "mentioned_governors": [],
        "mentioned_parties": [],
        "mentioned_candidates": [],
        "mentioned_politicians": [],
        "mentioned_persons": [],
        "is_rn_relevant": True,
        "sentiment_overall": 0.0,
        "sentiment_by_entity": [],
        "scope_status": "IN_SCOPE",
        "content_hash": "abc123",
        "actor_run_id": "actor-x",
        "ingestion_run_id": "ing-1",
        "batch_id": "batch-1",
    }
    base.update(overrides)
    return base


def test_row_with_no_mentions_produces_no_activations() -> None:
    rows = [_silver_row()]
    out = build_activation_records(rows, region="rn", pipeline_version="0.1.0")
    assert out == []


def test_single_mention_produces_one_activation() -> None:
    rows = [_silver_row(mentioned_mayors=["Fátima Bezerra"])]
    out = build_activation_records(rows, region="rn", pipeline_version="0.1.0")
    assert len(out) == 1
    activation = out[0]
    assert activation["author_id"] == "alice"
    assert activation["platform"] == "x"
    assert activation["person_target"] == "Fátima Bezerra"
    assert activation["target_kind"] == "mayor"
    assert activation["region"] == "rn"
    assert activation["pipeline_version"] == "0.1.0"
    assert activation["source_type"] == "social"
    assert activation["author_in_scope"] is True


def test_multiple_kinds_fan_out_with_first_kind_winning() -> None:
    """When the same name appears in multiple mentioned_* lists,
    the earliest kind wins (mayor > governor > candidate > politician
    > party > person)."""
    rows = [
        _silver_row(
            mentioned_mayors=["Allyson Bezerra"],
            mentioned_candidates=["Allyson Bezerra"],
            mentioned_politicians=["Allyson Bezerra"],
        )
    ]
    out = build_activation_records(rows, region="rn", pipeline_version="0.1.0")
    assert len(out) == 1
    assert out[0]["target_kind"] == "mayor"


def test_distinct_targets_produce_distinct_activations() -> None:
    rows = [
        _silver_row(
            mentioned_mayors=["Fátima Bezerra"],
            mentioned_governors=["Rogério Marinho"],
            mentioned_persons=["Random Citizen"],
        )
    ]
    out = build_activation_records(rows, region="rn", pipeline_version="0.1.0")
    assert len(out) == 3
    kinds = {a["target_kind"]: a["person_target"] for a in out}
    assert kinds == {
        "mayor": "Fátima Bezerra",
        "governor": "Rogério Marinho",
        "person": "Random Citizen",
    }


def test_falsy_author_handle_skips_row() -> None:
    rows = [_silver_row(author_handle="", mentioned_mayors=["X"])]
    out = build_activation_records(rows, region="rn", pipeline_version="0.1.0")
    assert out == []


def test_scope_status_drives_author_in_scope_flag() -> None:
    in_scope = _silver_row(
        mentioned_mayors=["X"], scope_status="IN_SCOPE", post_id="x:1"
    )
    out_of_scope = _silver_row(
        mentioned_mayors=["X"], scope_status="OUT_OF_SCOPE", post_id="x:2"
    )
    activations = build_activation_records(
        [in_scope, out_of_scope], region="rn", pipeline_version="0.1.0"
    )
    flags = {a["post_id"]: a["author_in_scope"] for a in activations}
    assert flags == {"x:1": True, "x:2": False}


def test_party_mention_produces_party_kind() -> None:
    rows = [_silver_row(mentioned_parties=["PT"])]
    out = build_activation_records(rows, region="rn", pipeline_version="0.1.0")
    assert len(out) == 1
    assert out[0]["target_kind"] == "party"
    assert out[0]["person_target"] == "PT"


def test_region_propagates_from_settings() -> None:
    rows = [_silver_row(mentioned_mayors=["X"])]
    out = build_activation_records(rows, region="pe", pipeline_version="0.2.0")
    assert out[0]["region"] == "pe"
