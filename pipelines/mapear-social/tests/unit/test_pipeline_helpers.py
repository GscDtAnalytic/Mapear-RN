"""Unit tests for pure helpers in mapear_social.pipeline.

Full pipeline orchestration lives in integration tests (W4-D5 smoke).
Here we cover the pieces that must behave correctly without a live
Apify / GCP environment: schema drift DLQ, intra-batch dedup, silver
row shape, and the per-person volume aggregation.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from loguru import logger

from mapear_domain.entity_resolution import ResolutionResult, ScopeStatus
from mapear_domain.region import load_region
from mapear_nlp.language_detector import LanguageDetection
from mapear_nlp.matchers.region_matcher import RegionMatcher
from mapear_nlp.narrative_explainer import NarrativeResult
from mapear_nlp.political_sentiment import PoliticalSentimentClassifier
from mapear_nlp.shadow import build_shadow_scorer
from mapear_social.adapters.base import PlatformAdapter, SchemaDriftError
from mapear_social.models import Engagement, SocialAccount, SocialPost
from mapear_social.pipeline import (
    _apply_shadow_to_silver_rows,
    _apply_social_narrative_explainer,
    _build_silver_row,
    _classifier_inputs,
    _dedup_intra_batch,
    _dlq_entries_to_records,
    _enrich_with_region_matcher,
    _guard_nonempty_payload,
    _parse_social_posts,
    _resolve_platform,
    _volume_by_person,
)

# Test region + matcher — shared across enrichment tests (module-level for speed)
_TEST_REGION = load_region("test")
_TEST_MATCHER = RegionMatcher(_TEST_REGION)

_LANG_PT = LanguageDetection(language="pt", confidence=0.99, reason="detected")


class _StubAdapter(PlatformAdapter):
    """Fake adapter that yields one good post + one schema-drift failure."""

    actor_id = "stub/stub"
    platform = "facebook"

    def expected_schema_version(self) -> int:
        return 1

    def targets_with_handle(self, targets):
        return targets

    def build_input(self, targets):
        return {}

    def parse_item(self, raw, actor_run_id, ingestion_run_id):
        if raw.get("bad"):
            raise SchemaDriftError("schema drift: missing postId")
        return SocialPost(
            post_id=f"fb:{raw['id']}",
            platform="facebook",
            url="https://facebook.com/p/1",
            account=SocialAccount(platform="facebook", handle="someone"),
            text=raw.get("text", ""),
            published_at=datetime(2026, 4, 20, tzinfo=UTC),
            content_hash="abc",
            actor_run_id=actor_run_id,
            ingestion_run_id=ingestion_run_id,
        )


def _post(post_id: str, text: str = "hello") -> SocialPost:
    return SocialPost(
        post_id=post_id,
        platform="facebook",
        url="https://facebook.com/p/1",
        account=SocialAccount(
            platform="facebook",
            handle="paulinho.freire",
            display_name="Paulinho",
            verified=True,
        ),
        author_display_name="Paulinho",
        text=text,
        language="pt",
        published_at=datetime(2026, 4, 20, tzinfo=UTC),
        engagement=Engagement(likes=10, comments=2, shares=1, views=100),
        content_hash="hash",
        actor_run_id="run-1",
        ingestion_run_id="ing-1",
    )


def test_parse_social_posts_zero_items_returns_empty_not_raising():
    """Actor returning 0 items (e.g. X with language filter) must not crash the job.

    The pipeline's ``if not items: return`` gate converts this into a clean exit,
    but _parse_social_posts itself must also be safe on empty input.
    """
    posts, dlq = _parse_social_posts(
        _StubAdapter(), [], actor_run_id="run-x-0", ingestion_run_id="ing-x-0"
    )
    assert posts == []
    assert dlq == []


def test_parse_social_posts_routes_drift_to_dlq():
    # Items must have >2 keys to pass the sparse-sentinel guard before parse_item.
    items = [
        {"id": "1", "text": "a", "platform": "facebook"},
        {"bad": True, "x": 1, "y": 2},
        {"id": "2", "text": "b", "platform": "facebook"},
    ]
    posts, dlq = _parse_social_posts(
        _StubAdapter(),
        items,
        actor_run_id="run-42",
        ingestion_run_id="ing-42",
    )
    assert len(posts) == 2
    assert {p.post_id for p in posts} == {"fb:1", "fb:2"}
    assert len(dlq) == 1
    assert dlq[0]["reason"] == "schema_drift"
    assert "missing postId" in dlq[0]["error"]
    assert dlq[0]["platform"] == "facebook"


def test_dedup_intra_batch_keeps_first_by_post_id():
    posts = [_post("fb:1", "first"), _post("fb:2"), _post("fb:1", "second")]
    unique = _dedup_intra_batch(posts)
    assert [p.post_id for p in unique] == ["fb:1", "fb:2"]
    assert unique[0].text == "first"  # first wins


def test_volume_by_person_aggregates():
    rows = [
        {"person_id": "a"},
        {"person_id": "a"},
        {"person_id": "b"},
        {"person_id": None},  # out-of-scope → not counted
    ]
    assert _volume_by_person(rows) == {"a": 2, "b": 1}


# --- Stage 1E v2 — shadow A/B path -----------------------------------------


def _classified_row(content_hash: str, *, person_id: str | None, polarity: float):
    """A silver row dict after Stage 5 stamped the classifier output."""
    from mapear_nlp.political_sentiment import PoliticalSentimentClassifier

    clf = PoliticalSentimentClassifier()
    res = clf.classify(polarity=polarity, volume_24h=0, velocity=0.0, engagement=0)
    return {
        "content_hash": content_hash,
        "person_id": person_id,
        "likes": 10,
        "comments": 2,
        "shares": 1,
        "sentiment_overall": polarity,
        "ingestion_run_id": "run-1",
        "sentiment_label": res.label,
        "confidence_score": res.confidence,
        "risk_score": res.risk_score,
        "rule_version": res.rule_version,
        "model_version": res.model_version,
    }


def _shadow_scorer(tmp_path, yaml_body: str):
    p = tmp_path / "candidate.yaml"
    p.write_text(yaml_body)
    return build_shadow_scorer(
        yaml_path=str(p),
        enabled=True,
        region="rn",
        tenant_id="default",
        pipeline_version="9.9.9",
        source_type="social",
    )


def test_classifier_inputs_derives_four_values():
    row = {
        "person_id": "p1",
        "likes": 100,
        "comments": 20,
        "shares": 5,
        "sentiment_overall": -0.4,
    }
    polarity, volume_24h, velocity, engagement = _classifier_inputs(row, {"p1": 12})
    assert polarity == pytest.approx(-0.4)
    assert volume_24h == 12
    assert velocity == pytest.approx(1.0)  # min(12/10, 1.0)
    assert engagement == 125


def test_classifier_inputs_handles_missing_person_and_engagement():
    polarity, volume_24h, velocity, engagement = _classifier_inputs(
        {"person_id": None}, {}
    )
    assert (polarity, volume_24h, velocity, engagement) == (0.0, 0, 0.0, 0)


def test_apply_shadow_returns_empty_when_scorer_none():
    rows = [_classified_row("h1", person_id="p1", polarity=-0.3)]
    assert _apply_shadow_to_silver_rows(rows, {"p1": 1}, None) == []


def test_apply_shadow_returns_empty_on_empty_rows(tmp_path):
    scorer = _shadow_scorer(tmp_path, "polarity_negative: -0.40\n")
    assert _apply_shadow_to_silver_rows([], {}, scorer) == []


def test_apply_shadow_emits_one_row_per_post(tmp_path):
    scorer = _shadow_scorer(tmp_path, "polarity_warning: -0.05\n")
    rows = [
        _classified_row("h1", person_id="p1", polarity=-0.2),
        _classified_row("h2", person_id=None, polarity=0.3),
    ]
    shadow_rows = _apply_shadow_to_silver_rows(rows, {"p1": 3}, scorer)

    assert {r.content_hash for r in shadow_rows} == {"h1", "h2"}
    for r in shadow_rows:
        assert r.source_type == "social"
        assert r.region == "rn"
        assert r.tenant_id == "default"
        assert r.pipeline_version == "9.9.9"


def test_apply_shadow_primary_snapshot_mirrors_stamped_row(tmp_path):
    scorer = _shadow_scorer(tmp_path, "polarity_warning: -0.05\n")
    row = _classified_row("h1", person_id="p1", polarity=-0.2)

    shadow_rows = _apply_shadow_to_silver_rows([row], {"p1": 1}, scorer)

    sr = shadow_rows[0]
    assert sr.primary_label == row["sentiment_label"]
    assert sr.primary_rule_version == row["rule_version"]
    assert sr.primary_confidence == row["confidence_score"]
    assert sr.primary_risk_score == row["risk_score"]
    assert sr.shadow_rule_version != sr.primary_rule_version


def test_build_silver_row_shape_and_flattening():
    post = _post("fb:1", "a mayor said something")
    resolution = ResolutionResult(
        person_id="mayor_paulinho_freire",
        canonical_name="Paulinho Freire",
        role="mayor",
        confidence=0.92,
        scope_status=ScopeStatus.IN_SCOPE,
        matched_signal="handle:facebook",
    )
    classifier = PoliticalSentimentClassifier()
    classification = classifier.classify(
        polarity=-0.4, volume_24h=20, velocity=0.8, engagement=6000
    )
    ner_result = {
        "entities": [{"text": "Paulinho", "label": "PER"}],
        "mentioned_cities": ["Natal"],
        "mentioned_mayors": ["Paulinho Freire"],
        "mentioned_governors": [],
        "mentioned_parties": ["União Brasil"],
        "mentioned_persons": ["Paulinho Freire"],
        "is_rn_relevant": True,
    }
    sentiment = {
        "sentiment_overall": -0.4,
        "sentiment_by_entity": [
            {"entity": "Paulinho", "entity_type": "mayor", "sentiment": -0.3}
        ],
    }

    row = _build_silver_row(
        post=post,
        ner_result=ner_result,
        sentiment=sentiment,
        resolution=resolution,
        classification=classification,
        batch_id="20260420_120000",
        lang_detection=_LANG_PT,
    )

    assert row["post_id"] == "fb:1"
    assert row["platform"] == "facebook"
    assert row["author_handle"] == "paulinho.freire"
    assert row["author_verified"] is True
    assert row["likes"] == 10
    assert row["comments"] == 2
    assert row["shares"] == 1
    assert row["views"] == 100
    assert row["mentioned_cities"] == ["Natal"]
    assert row["person_id"] == "mayor_paulinho_freire"
    assert row["scope_status"] == "IN_SCOPE"
    assert row["resolution_confidence"] == pytest.approx(0.92)
    assert row["sentiment_label"] == classification.label
    assert row["confidence_score"] == classification.confidence
    assert row["risk_score"] == classification.risk_score
    assert row["rule_version"] == classification.rule_version
    assert row["model_version"] == classification.model_version
    assert row["pipeline_version"]  # populated
    assert row["source_type"] == "social"
    assert row["batch_id"] == "20260420_120000"


def test_build_silver_row_handles_missing_classification():
    post = _post("fb:1")
    resolution = ResolutionResult(
        person_id=None,
        canonical_name=None,
        role=None,
        confidence=0.0,
        scope_status=ScopeStatus.OUT_OF_SCOPE,
        matched_signal="no_match",
    )
    row = _build_silver_row(
        post=post,
        ner_result={},
        sentiment={},
        resolution=resolution,
        classification=None,
        batch_id="batch-x",
        lang_detection=_LANG_PT,
    )
    assert row["sentiment_label"] is None
    assert row["decision_factors"] == []
    assert row["rule_version"] is None
    assert row["author_in_scope"] is False


def test_build_silver_row_emits_canonical_computed_fields():
    """author_in_scope and content_rn_relevant must be present in the row dict.

    Regression guard for TD-AUTHOR-IN-SCOPE-NULL: _build_silver_row returns a
    plain dict, so @computed_field properties on SilverSocialPost are never
    evaluated. Both fields must be derived explicitly; otherwise dataframe_to_table
    fills them with NULL and every row in BQ lands as None.
    """
    post = _post("fb:42", "Prefeito de Natal discutiu obras.")
    resolution_in = ResolutionResult(
        person_id="mayor_joao",
        canonical_name="João",
        role="mayor",
        confidence=0.85,
        scope_status=ScopeStatus.IN_SCOPE,
        matched_signal="handle:facebook",
    )
    resolution_out = ResolutionResult(
        person_id=None,
        canonical_name=None,
        role=None,
        confidence=0.0,
        scope_status=ScopeStatus.OUT_OF_SCOPE,
        matched_signal="no_match",
    )
    ner_rn = {"is_rn_relevant": True}
    ner_not_rn = {"is_rn_relevant": False}

    row_in_rn = _build_silver_row(
        post=post,
        ner_result=ner_rn,
        sentiment={},
        resolution=resolution_in,
        classification=None,
        batch_id="b1",
        lang_detection=_LANG_PT,
    )
    assert row_in_rn["author_in_scope"] is True
    assert row_in_rn["content_rn_relevant"] is True

    row_out_not_rn = _build_silver_row(
        post=post,
        ner_result=ner_not_rn,
        sentiment={},
        resolution=resolution_out,
        classification=None,
        batch_id="b2",
        lang_detection=_LANG_PT,
    )
    assert row_out_not_rn["author_in_scope"] is False
    assert row_out_not_rn["content_rn_relevant"] is False


# --- Pre-call payload guard (BL-F2 Facebook 400 fix) ------------------------


def test_guard_nonempty_payload_exits_on_empty_targets():
    """Empty startUrls with no other target lists must raise SystemExit(5)."""
    adapter = _StubAdapter()
    empty_payload = {
        "startUrls": [],
        "query": "",
        "resultsLimit": 100,
        "commentsMode": "NONE",
    }
    with pytest.raises(SystemExit) as exc_info:
        _guard_nonempty_payload(adapter, empty_payload, "facebook", logger)
    assert exc_info.value.code == 5


def test_guard_nonempty_payload_passes_with_valid_start_urls():
    """Non-empty startUrls must not raise."""
    adapter = _StubAdapter()
    valid_payload = {
        "startUrls": [{"url": "https://facebook.com/paulinho.freire"}],
        "query": "",
        "resultsLimit": 100,
        "commentsMode": "NONE",
    }
    _guard_nonempty_payload(
        adapter, valid_payload, "facebook", logger
    )  # must not raise


def test_resolve_platform_cli_wins_over_env(monkeypatch):
    from mapear_social.config import SocialSettings

    monkeypatch.setenv("SOCIAL_PLATFORM", "instagram")
    settings = SocialSettings()
    assert _resolve_platform(settings, "x") == "x"


def test_resolve_platform_falls_back_to_env(monkeypatch):
    from mapear_social.config import SocialSettings

    monkeypatch.setenv("SOCIAL_PLATFORM", "instagram")
    settings = SocialSettings()
    assert _resolve_platform(settings, None) == "instagram"


def test_resolve_platform_rejects_unknown(monkeypatch):
    monkeypatch.delenv("SOCIAL_PLATFORM", raising=False)
    from mapear_social.config import SocialSettings

    settings = SocialSettings(platform="snapchat")
    with pytest.raises(SystemExit):
        _resolve_platform(settings, None)


# --- Entrypoint smoke tests (guard against Cloud Run args regression) ---


def test_parse_args_no_args_defaults_platform_to_none():
    from mapear_social.pipeline import _parse_args

    args = _parse_args([])
    assert args.platform is None


@pytest.mark.parametrize("platform", ["facebook", "instagram", "x", "tiktok"])
def test_parse_args_accepts_all_valid_platforms(platform):
    from mapear_social.pipeline import _parse_args

    args = _parse_args(["--platform", platform])
    assert args.platform == platform


def test_parse_args_rejects_unknown_platform():
    from mapear_social.pipeline import _parse_args

    with pytest.raises(SystemExit) as exc_info:
        _parse_args(["--platform", "snapchat"])
    assert exc_info.value.code == 2


# --- _fetch_items: ActorRun.run_id attribute (not .id) -----------------------


def test_fetch_items_returns_run_run_id(monkeypatch):
    """_fetch_items must extract run.run_id — ActorRun has no .id attribute."""
    import asyncio

    from mapear_social.apify_client import ActorRun
    from mapear_social.pipeline import _fetch_items

    fake_run = ActorRun(
        run_id="real-run-abc123",
        status="SUCCEEDED",
        dataset_id="ds-1",
        started_at=None,
        finished_at=None,
    )
    fake_items = [{"id": "post-1"}, {"id": "post-2"}]

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def run_actor(self, actor_id, payload):
            return fake_run, fake_items

    monkeypatch.setattr(
        "mapear_social.pipeline.ApifyClient",
        lambda **kwargs: _FakeClient(),
    )

    run_id, items = asyncio.run(
        _fetch_items(
            token="tok",
            actor_id="apify/actor",
            payload={},
            poll_timeout=300,
            page_size=100,
        )
    )

    assert run_id == "real-run-abc123"
    assert items == fake_items


# --- _dlq_entries_to_records --------------------------------------------------


def _make_dlq_entry(reason: str = "schema_drift", error: str = "missing x") -> dict:
    return {
        "platform": "instagram",
        "actor_run_id": "run-ig-42",
        "ingestion_run_id": "ing-ig-42",
        "reason": reason,
        "error": error,
        "raw": {"shortCode": "ABC", "mediaType": "IMAGE"},
        "raw_keys": ["mediaType", "shortCode"],
        "captured_at": "2026-04-22T10:00:00+00:00",
    }


def test_dlq_entries_to_records_shape():
    """Each DLQ entry must map to the SOCIAL_DLQ_SCHEMA field set."""
    import json

    entries = [_make_dlq_entry("schema_drift", "missing ownerUsername")]
    records = _dlq_entries_to_records(entries, actor_id="shu8hvrXbJbY3Eb9W")

    assert len(records) == 1
    r = records[0]
    assert r["platform"] == "instagram"
    assert r["actor_id"] == "shu8hvrXbJbY3Eb9W"
    assert r["actor_run_id"] == "run-ig-42"
    assert r["ingestion_run_id"] == "ing-ig-42"
    assert r["error_type"] == "schema_drift"
    assert r["error_message"] == "missing ownerUsername"
    assert json.loads(r["raw_payload_json"]) == {
        "shortCode": "ABC",
        "mediaType": "IMAGE",
    }
    assert json.loads(r["raw_keys_json"]) == ["mediaType", "shortCode"]
    assert r["created_at"].year == 2026


def test_dlq_entries_to_records_derives_raw_keys_when_absent():
    """raw_keys_json falls back to sorted(raw.keys()) when raw_keys is missing."""
    import json

    entry = _make_dlq_entry()
    del entry["raw_keys"]
    records = _dlq_entries_to_records([entry], actor_id="actor-x")
    keys = json.loads(records[0]["raw_keys_json"])
    assert keys == ["mediaType", "shortCode"]


def test_dlq_entries_to_records_multiple_entries():
    entries = [
        _make_dlq_entry("schema_drift"),
        _make_dlq_entry("non_post_item", "sparse sentinel"),
        _make_dlq_entry("parse_error", "ValueError: bad date"),
    ]
    records = _dlq_entries_to_records(entries, actor_id="actor-id")
    assert len(records) == 3
    assert {r["error_type"] for r in records} == {
        "schema_drift",
        "non_post_item",
        "parse_error",
    }


def test_dlq_entries_to_records_serialises_non_json_raw():
    """raw payload with non-serialisable values (e.g. datetime) must not crash."""
    from datetime import UTC, datetime

    entry = _make_dlq_entry()
    entry["raw"] = {"ts": datetime(2026, 4, 22, tzinfo=UTC), "n": 1}
    records = _dlq_entries_to_records([entry], actor_id="x")
    # default=str renders datetime as a string — must not raise
    import json

    parsed = json.loads(records[0]["raw_payload_json"])
    assert "ts" in parsed


def test_fetch_items_raises_on_empty_run_id(monkeypatch):
    """An ActorRun with an empty run_id must raise ApifyError before returning."""
    import asyncio

    from mapear_social.apify_client import ActorRun, ApifyError
    from mapear_social.pipeline import _fetch_items

    fake_run = ActorRun(
        run_id="",
        status="SUCCEEDED",
        dataset_id="ds-1",
        started_at=None,
        finished_at=None,
    )

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def run_actor(self, actor_id, payload):
            return fake_run, []

    monkeypatch.setattr(
        "mapear_social.pipeline.ApifyClient",
        lambda **kwargs: _FakeClient(),
    )

    with pytest.raises(ApifyError, match="empty run_id"):
        asyncio.run(
            _fetch_items(
                token="tok",
                actor_id="apify/actor",
                payload={},
                poll_timeout=300,
                page_size=100,
            )
        )


# ---------------------------------------------------------------------------
# _enrich_with_region_matcher — Region("test") to avoid RN seed dep in CI
# ---------------------------------------------------------------------------

_EMPTY_NER: dict = {
    "entities": [],
    "mentioned_cities": [],
    "mentioned_mayors": [],
    "mentioned_governors": [],
    "mentioned_parties": [],
    "mentioned_candidates": [],
    "mentioned_politicians": [],
    "mentioned_persons": [],
    "is_rn_relevant": False,
}


def test_enrich_matcher_finds_city_by_alias():
    """RegionMatcher detects 'Testópolis' from city_aliases (path 2)."""
    ner = dict(_EMPTY_NER)
    enriched, counts = _enrich_with_region_matcher(
        ner_result=ner,
        text="A cidade de Testópolis vai crescer muito este ano.",
        platform="instagram",
        author_handle=None,
        region=_TEST_REGION,
        matcher=_TEST_MATCHER,
    )
    assert "Testópolis" in enriched["mentioned_cities"]
    assert counts["matcher"] >= 1
    assert counts["handle"] == 0


def test_enrich_handle_adds_mayor_and_city():
    """Handle resolution: instagram 'bobprefeito' → Bob Prefeito + Testópolis added."""
    ner = dict(_EMPTY_NER)
    enriched, counts = _enrich_with_region_matcher(
        ner_result=ner,
        text="Bom dia a todos!",  # name not in text
        platform="instagram",
        author_handle="bobprefeito",
        region=_TEST_REGION,
        matcher=_TEST_MATCHER,
    )
    assert "Bob Prefeito" in enriched["mentioned_mayors"]
    assert "Testópolis" in enriched["mentioned_cities"]
    assert counts["handle"] == 2  # mayor + city
    assert counts["matcher"] == 0


def test_enrich_handle_adds_governor():
    """Handle resolution: facebook 'alicegovfb' → Alice Governadora
    added to mentioned_governors."""
    ner = dict(_EMPTY_NER)
    enriched, counts = _enrich_with_region_matcher(
        ner_result=ner,
        text="Inauguração de obra importante.",
        platform="facebook",
        author_handle="alicegovfb",
        region=_TEST_REGION,
        matcher=_TEST_MATCHER,
    )
    assert "Alice Governadora" in enriched["mentioned_governors"]
    assert counts["handle"] >= 1


def test_enrich_no_duplicate_when_ner_already_has_entity():
    """Merger never duplicates: if NER already found Testópolis, matcher count is 0."""
    ner = {**_EMPTY_NER, "mentioned_cities": ["Testópolis"]}
    enriched, counts = _enrich_with_region_matcher(
        ner_result=ner,
        text="Testópolis recebeu obras.",
        platform="instagram",
        author_handle=None,
        region=_TEST_REGION,
        matcher=_TEST_MATCHER,
    )
    assert enriched["mentioned_cities"].count("Testópolis") == 1
    assert counts["matcher"] == 0  # already present — no new entity added


def test_enrich_dedup_case_insensitive_ner_lowercase_matcher_canonical():
    """NER retorna 'testópolis' lowercase, matcher retorna 'Testópolis'
    → resultado é apenas 'Testópolis' (forma canônica).

    Documenta o invariante: a deduplicação é case-insensitive, e a forma canônica
    do RegionMatcher (proper case) prevalece quando o NER produz forma em minúsculas.
    Sem esse comportamento, set union ingênuo geraria ['testópolis', 'Testópolis'].
    """
    ner = {
        **_EMPTY_NER,
        "mentioned_cities": ["testópolis"],
    }  # lowercase simulando artifact de NER
    enriched, counts = _enrich_with_region_matcher(
        ner_result=ner,
        text="testópolis recebeu obras.",
        platform="instagram",
        author_handle=None,
        region=_TEST_REGION,
        matcher=_TEST_MATCHER,
    )
    cities_lower = [c.lower() for c in enriched["mentioned_cities"]]
    assert cities_lower.count("testópolis") == 1, "duplicata case-insensitive detectada"
    assert counts["matcher"] == 0  # já presente — zero novas entidades adicionadas


def test_enrich_no_duplicate_when_handle_already_in_mayors():
    """Handle resolution doesn't duplicate if matcher already added the mayor."""
    ner = {
        **_EMPTY_NER,
        "mentioned_mayors": ["Bob Prefeito"],
        "mentioned_cities": ["Testópolis"],
    }
    enriched, counts = _enrich_with_region_matcher(
        ner_result=ner,
        text="Bob Prefeito inaugurou a escola em Testópolis.",
        platform="instagram",
        author_handle="bobprefeito",
        region=_TEST_REGION,
        matcher=_TEST_MATCHER,
    )
    assert enriched["mentioned_mayors"].count("Bob Prefeito") == 1
    assert enriched["mentioned_cities"].count("Testópolis") == 1
    assert counts["handle"] == 0  # both already present


def test_enrich_unknown_handle_no_effect():
    """Unknown handle produces no enrichment (not a registered politician)."""
    ner = dict(_EMPTY_NER)
    enriched, counts = _enrich_with_region_matcher(
        ner_result=ner,
        text="Olá mundo!",
        platform="instagram",
        author_handle="random_citizen_rn",
        region=_TEST_REGION,
        matcher=_TEST_MATCHER,
    )
    assert enriched["mentioned_mayors"] == []
    assert enriched["mentioned_cities"] == []
    assert counts["handle"] == 0


def test_enrich_none_handle_no_crash():
    """author_handle=None is a valid input (RSS posts have no handle)."""
    ner = dict(_EMPTY_NER)
    enriched, counts = _enrich_with_region_matcher(
        ner_result=ner,
        text="Vilafake cresceu este ano.",
        platform="facebook",
        author_handle=None,
        region=_TEST_REGION,
        matcher=_TEST_MATCHER,
    )
    assert "Vilafake" in enriched["mentioned_cities"]
    assert counts["handle"] == 0


def test_enrich_matcher_propagates_candidates():
    """RegionMatcher detecta governor_candidate e propaga em mentioned_candidates
    (TDT-RM-01: campos não podem ser silenciosamente descartados)."""
    ner = dict(_EMPTY_NER)
    enriched, counts = _enrich_with_region_matcher(
        ner_result=ner,
        text="Dan Candidato lidera as pesquisas eleitorais.",
        platform="instagram",
        author_handle=None,
        region=_TEST_REGION,
        matcher=_TEST_MATCHER,
    )
    assert "Dan Candidato" in enriched["mentioned_candidates"]
    assert "Dan Candidato" not in enriched["mentioned_governors"]
    assert counts["matcher"] >= 1


def test_enrich_matcher_propagates_politicians():
    """RegionMatcher detecta senator/deputy_federal/vice_governor e propaga
    em mentioned_politicians (TDT-RM-01)."""
    ner = dict(_EMPTY_NER)
    enriched, counts = _enrich_with_region_matcher(
        ner_result=ner,
        text="Eva Senadora votou contra a proposta.",
        platform="x",
        author_handle=None,
        region=_TEST_REGION,
        matcher=_TEST_MATCHER,
    )
    assert "Eva Senadora" in enriched["mentioned_politicians"]
    assert "Eva Senadora" not in enriched["mentioned_governors"]
    assert counts["matcher"] >= 1


def test_enrich_returns_count_keys():
    """Returned count dict always has ner/matcher/handle keys."""
    _, counts = _enrich_with_region_matcher(
        ner_result=dict(_EMPTY_NER),
        text="",
        platform="x",
        author_handle=None,
        region=_TEST_REGION,
        matcher=_TEST_MATCHER,
    )
    assert set(counts.keys()) == {"ner", "matcher", "handle"}
    assert all(isinstance(v, int) for v in counts.values())


def test_enrich_preserves_existing_ner_fields():
    """Enrichment never removes fields already set by NER."""
    ner = {
        **_EMPTY_NER,
        "entities": [{"text": "João", "label": "PER"}],
        "mentioned_persons": ["João Teste"],
        "is_rn_relevant": True,
    }
    enriched, _ = _enrich_with_region_matcher(
        ner_result=ner,
        text="João Teste inaugurou obras.",
        platform="instagram",
        author_handle=None,
        region=_TEST_REGION,
        matcher=_TEST_MATCHER,
    )
    assert enriched["entities"] == [{"text": "João", "label": "PER"}]
    assert "João Teste" in enriched["mentioned_persons"]
    assert enriched["is_rn_relevant"] is True


# ---------------------------------------------------------------------------
# X auth-failure regression — issue #41 / #42
# Pipeline-level guarantees: ≥50% HTTP 401/403 → SystemExit(6) AND watermark
# is not advanced. Adapter-level coverage lives in test_adapters.py.
# ---------------------------------------------------------------------------


def test_x_pipeline_aborts_on_full_auth_failure_and_does_not_advance_watermark(
    monkeypatch,
):
    from mapear_domain.entity_resolution import Target
    from mapear_social import pipeline as pipeline_module
    from mapear_social.pipeline import run_pipeline

    fake_targets = [
        Target(
            person_id="governor_fatima_bezerra",
            name="Fátima Bezerra",
            role="governor",
            party="PT",
            city="",
            x_handle="fatimabezerra",
        ),
        Target(
            person_id="mayor_paulinho_freire",
            name="Paulinho Freire",
            role="mayor",
            party="União Brasil",
            city="Natal",
            x_handle="paulinhofreire",
        ),
    ]

    class _FakeResolver:
        def __init__(self, *_a, **_kw):
            pass

        def list_targets(self):
            return list(fake_targets)

    monkeypatch.setattr(pipeline_module, "PersonResolver", _FakeResolver)
    monkeypatch.setattr(pipeline_module, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_module, "start_metrics_server", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_module, "setup_tracing", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_module, "_point_seeds_at_dbt", lambda: None)
    monkeypatch.setenv("X_BEARER_TOKEN", "expired-token")
    monkeypatch.setenv("ENVIRONMENT", "local")

    save_calls: list = []

    class _FakeWatermarkManager:
        def __init__(self, key):
            self.key = key

        def get_watermark(self):
            return None

        def save_watermark(self, ts):
            save_calls.append(ts)

    monkeypatch.setattr(pipeline_module, "WatermarkManager", _FakeWatermarkManager)

    def _all_auth_fail(self, targets, *, ingestion_run_id, actor_run_id, **kwargs):
        attempted = len(targets)
        dlq = [
            {
                "platform": "x",
                "actor_run_id": actor_run_id,
                "ingestion_run_id": ingestion_run_id,
                "reason": "auth_error",
                "error": "X API /users/by/username/x returned HTTP 401: Unauthorized",
                "raw_keys": ["handle"],
                "raw": {"handle": t.x_handle},
                "captured_at": "2026-05-07T00:00:00+00:00",
            }
            for t in targets
        ]
        stats = {
            "handles_attempted": attempted,
            "auth_failures": attempted,
            "api_errors": 0,
            "users_not_found": 0,
            "successful_calls": 0,
        }
        return [], dlq, stats

    monkeypatch.setattr(
        pipeline_module.XAdapter,
        "fetch_posts_via_api",
        _all_auth_fail,
    )

    with pytest.raises(SystemExit) as exc_info:
        run_pipeline(cli_platform="x")

    assert exc_info.value.code == 6, "100% auth-fail must raise SystemExit(6)"
    assert (
        save_calls == []
    ), "watermark must NOT advance when auth-fail threshold is breached"


def test_x_pipeline_legitimate_empty_advances_watermark(monkeypatch):
    """When every handle answered HTTP 200 with zero new tweets, the run is
    a *legitimate empty* — watermark must advance so the next run starts
    from now and does not re-process the same window."""
    from mapear_domain.entity_resolution import Target
    from mapear_social import pipeline as pipeline_module
    from mapear_social.pipeline import run_pipeline

    fake_targets = [
        Target(
            person_id="mayor_x",
            name="X",
            role="mayor",
            party="X",
            city="Natal",
            x_handle="someone",
        ),
    ]

    class _FakeResolver:
        def __init__(self, *_a, **_kw):
            pass

        def list_targets(self):
            return list(fake_targets)

    monkeypatch.setattr(pipeline_module, "PersonResolver", _FakeResolver)
    monkeypatch.setattr(pipeline_module, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_module, "start_metrics_server", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_module, "setup_tracing", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_module, "_point_seeds_at_dbt", lambda: None)
    monkeypatch.setenv("X_BEARER_TOKEN", "valid-token")
    monkeypatch.setenv("ENVIRONMENT", "local")

    save_calls: list = []

    class _FakeWatermarkManager:
        def __init__(self, key):
            self.key = key

        def get_watermark(self):
            return None

        def save_watermark(self, ts):
            save_calls.append(ts)

    monkeypatch.setattr(pipeline_module, "WatermarkManager", _FakeWatermarkManager)

    def _legitimate_empty(self, targets, *, ingestion_run_id, actor_run_id, **kwargs):
        return (
            [],
            [],
            {
                "handles_attempted": len(targets),
                "auth_failures": 0,
                "api_errors": 0,
                "users_not_found": 0,
                "successful_calls": len(targets),
            },
        )

    monkeypatch.setattr(
        pipeline_module.XAdapter,
        "fetch_posts_via_api",
        _legitimate_empty,
    )

    run_pipeline(cli_platform="x")

    assert (
        len(save_calls) == 1
    ), "watermark must advance when API returned 200 + zero new posts"


def test_x_pipeline_partial_auth_below_threshold_does_not_abort_but_blocks_watermark(
    monkeypatch,
):
    """Below threshold (e.g. 1 of 3 handles auth-failed) — pipeline keeps
    running for the other handles but if it ends up with 0 posts the
    watermark must still NOT advance, because we have evidence of a real
    error and cannot prove the empty result is legitimate."""
    from mapear_domain.entity_resolution import Target
    from mapear_social import pipeline as pipeline_module
    from mapear_social.pipeline import run_pipeline

    fake_targets = [
        Target(
            person_id=f"mayor_{n}",
            name=n,
            role="mayor",
            party="X",
            city="Natal",
            x_handle=n,
        )
        for n in ("a", "b", "c")
    ]

    class _FakeResolver:
        def __init__(self, *_a, **_kw):
            pass

        def list_targets(self):
            return list(fake_targets)

    monkeypatch.setattr(pipeline_module, "PersonResolver", _FakeResolver)
    monkeypatch.setattr(pipeline_module, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_module, "start_metrics_server", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_module, "setup_tracing", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_module, "_point_seeds_at_dbt", lambda: None)
    monkeypatch.setenv("X_BEARER_TOKEN", "valid-token")
    monkeypatch.setenv("ENVIRONMENT", "local")

    save_calls: list = []

    class _FakeWatermarkManager:
        def __init__(self, key):
            self.key = key

        def get_watermark(self):
            return None

        def save_watermark(self, ts):
            save_calls.append(ts)

    monkeypatch.setattr(pipeline_module, "WatermarkManager", _FakeWatermarkManager)

    def _one_auth_two_ok(self, targets, *, ingestion_run_id, actor_run_id, **kwargs):
        return (
            [],
            [
                {
                    "platform": "x",
                    "actor_run_id": actor_run_id,
                    "ingestion_run_id": ingestion_run_id,
                    "reason": "auth_error",
                    "error": "HTTP 401",
                    "raw_keys": ["handle"],
                    "raw": {"handle": "a"},
                    "captured_at": "2026-05-07T00:00:00+00:00",
                }
            ],
            {
                "handles_attempted": 3,
                "auth_failures": 1,  # 33% < 50% threshold
                "api_errors": 0,
                "users_not_found": 0,
                "successful_calls": 2,
            },
        )

    monkeypatch.setattr(
        pipeline_module.XAdapter,
        "fetch_posts_via_api",
        _one_auth_two_ok,
    )

    # No SystemExit raised — partial auth-fail below threshold continues.
    run_pipeline(cli_platform="x")

    assert save_calls == [], "any auth/api error with 0 posts must block the watermark"


def test_enrich_performance_under_1ms_per_post():
    """_enrich_with_region_matcher must stay <1ms per post (matcher overhead budget).

    In production the dominant cost is NER via GCP NL API (~200-500ms/call).
    This test guards against regex/handle-lookup regressions in the new path.
    The 1ms budget is 10-50× the observed matcher cost (~0.09ms); ample headroom.
    """
    texts = [
        "Testópolis vai crescer muito este ano com investimentos em infraestrutura.",
        "Bom dia a todos os cidadãos de Vilafake e Cidadezinha!",
        "O prefeito João Teste inaugurou a escola municipal.",
        "Reunião importante com a governadora Alice Governadora.",
        "Sem menção a nenhuma entidade política conhecida aqui.",
    ] * 20  # 100 iterations

    start = time.perf_counter()
    for text in texts:
        _enrich_with_region_matcher(
            ner_result=dict(_EMPTY_NER),
            text=text,
            platform="instagram",
            author_handle="bobprefeito",
            region=_TEST_REGION,
            matcher=_TEST_MATCHER,
        )
    elapsed_ms = (time.perf_counter() - start) / len(texts) * 1000
    assert (
        elapsed_ms < 1.0
    ), f"_enrich_with_region_matcher levou {elapsed_ms:.2f}ms/post (limite: 1ms)"


# --- Eixo 2 v1: _apply_social_narrative_explainer ----------------------------


class _FakeExplainer:
    """Minimal stand-in for NarrativeExplainer — avoids LLM + cache deps."""

    def __init__(
        self, summary: str | None = "Resumo gerado.", error: str | None = None
    ) -> None:
        self._summary = summary
        self._error = error
        self.calls: list[dict] = []

    def explain(
        self,
        *,
        content_hash,
        title,
        content,
        person_name,
        person_role,
        polarity,
        velocity,
        volume,
        decision_factors,
        rule_version,
    ):
        self.calls.append({"content_hash": content_hash, "title": title})
        return NarrativeResult(
            summary=self._summary,
            prompt_version="narrative_v1",
            cache_hit=False,
            error=self._error,
            redaction_level="masked",
            redaction_counts={},
        )


def _alert_row(**kwargs) -> dict:
    base = {
        "content_hash": "abc123",
        "sentiment_label": "ALERT",
        "text": "Prefeito cortou verbas da saúde municipal.",
        "mentioned_persons": ["Paulinho Freire"],
        "sentiment_overall": -0.5,
        "decision_factors": [
            {"name": "velocity", "value": 0.8, "weight": 1.0},
            {"name": "volume", "value": 20, "weight": 1.0},
        ],
        "rule_version": "v3",
        "narrative_summary": None,
        "narrative_prompt_version": None,
    }
    base.update(kwargs)
    return base


def test_apply_social_narrative_explainer_stamps_alert_row():
    rows = [_alert_row()]
    explainer = _FakeExplainer("Resumo do post.")
    _apply_social_narrative_explainer(
        rows, explainer, tenant_id=None, region_id="rn", provider="fake", model="fake-m"
    )
    assert rows[0]["narrative_summary"] == "Resumo do post."
    assert rows[0]["narrative_prompt_version"] == "narrative_v1"


def test_apply_social_narrative_explainer_skips_non_alert():
    row = _alert_row(sentiment_label="WARNING")
    _apply_social_narrative_explainer(
        [row], _FakeExplainer(), tenant_id=None, region_id="rn", provider="p", model="m"
    )
    assert row["narrative_summary"] is None
    assert row["narrative_prompt_version"] is None


def test_apply_social_narrative_explainer_noop_when_explainer_none():
    rows = [_alert_row()]
    _apply_social_narrative_explainer(
        rows, None, tenant_id=None, region_id="rn", provider="p", model="m"
    )
    assert rows[0]["narrative_summary"] is None


def test_apply_social_narrative_explainer_uses_first_mentioned_person():
    row = _alert_row(mentioned_persons=["Fátima Bezerra", "Carlos Eduardo"])
    explainer = _FakeExplainer("ok")
    _apply_social_narrative_explainer(
        [row], explainer, tenant_id=None, region_id="rn", provider="p", model="m"
    )
    assert explainer.calls[0]["content_hash"] == "abc123"


def test_apply_social_narrative_explainer_passes_empty_title():
    row = _alert_row()
    explainer = _FakeExplainer("ok")
    _apply_social_narrative_explainer(
        [row], explainer, tenant_id=None, region_id="rn", provider="p", model="m"
    )
    assert explainer.calls[0]["title"] == ""


def test_apply_social_narrative_explainer_error_leaves_summary_none():
    row = _alert_row()
    explainer = _FakeExplainer(summary=None, error="LLM timeout")
    _apply_social_narrative_explainer(
        [row], explainer, tenant_id=None, region_id="rn", provider="p", model="m"
    )
    assert row["narrative_summary"] is None


def test_build_silver_row_contains_narrative_fields():
    post = _post("fb:999", "Post de teste")
    resolution = ResolutionResult(
        person_id=None,
        canonical_name=None,
        role=None,
        confidence=0.0,
        scope_status=ScopeStatus.OUT_OF_SCOPE,
        matched_signal="no_match",
    )
    row = _build_silver_row(
        post=post,
        ner_result={},
        sentiment={},
        resolution=resolution,
        classification=None,
        batch_id="b",
        lang_detection=_LANG_PT,
    )
    assert "narrative_summary" in row
    assert row["narrative_summary"] is None
    assert "narrative_prompt_version" in row
    assert row["narrative_prompt_version"] is None
