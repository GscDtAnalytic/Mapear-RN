"""Stage 4.5 — _classify_political_sentiment overlays sentiment_label on RSS gold.

Cobertura BL-F2-05 / BL-28 (single-batch):
  - sentiment_label sempre em {FAVORABLE, WARNING, ALERT}
  - confidence_score em [0, 1]
  - rule_version e model_version preenchidos
  - articles sem person_id mantêm rota polarity-only (zero reach) → ALERT inalcançável
  - articles com person_id e reach suficiente alcançam ALERT (closes BL-28 single-batch)
  - lista vazia é no-op
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mapear_domain.models.base import GoldArticle
from mapear_nlp.shadow import build_shadow_scorer
from mapear_rss.pipeline import _classify_political_sentiment


def _make_gold(
    content_hash: str,
    sentiment_overall: float | None,
    *,
    person_id: str | None = None,
    published_at: datetime | None = None,
) -> GoldArticle:
    return GoldArticle(
        url=f"https://example.com/{content_hash}",
        source_feed="example",
        title="t",
        content_clean="c",
        published_at=published_at or datetime(2026, 5, 1, tzinfo=UTC),
        content_hash=content_hash,
        is_rn_relevant=True,
        sentiment_overall=sentiment_overall,
        person_id=person_id,
    )


def test_classifier_populates_six_fields_on_each_article():
    articles = [
        _make_gold("h1", 0.5),  # strong positive → FAVORABLE candidate
        _make_gold("h2", 0.0),  # neutral → WARNING (default low-signal)
        _make_gold("h3", -0.5),  # negative without velocity → WARNING
    ]

    _classify_political_sentiment(articles)

    for g in articles:
        assert g.sentiment_label in {"FAVORABLE", "WARNING", "ALERT"}
        assert g.confidence_score is not None and 0.0 <= g.confidence_score <= 1.0
        assert g.risk_score is not None and 0.0 <= g.risk_score <= 1.0
        assert g.rule_version  # short hex hash, non-empty
        assert g.model_version and g.model_version.startswith("political-sentiment")
        assert isinstance(g.decision_factors, list) and len(g.decision_factors) >= 1


def test_classifier_articles_without_person_id_never_yield_alert():
    """person_id=None → reach returns zeros → ALERT branch unreachable."""
    articles = [
        _make_gold(f"h{i}", p) for i, p in enumerate([-0.9, -0.5, -0.2, 0.0, 0.5, 0.9])
    ]

    _classify_political_sentiment(articles)

    labels = {g.sentiment_label for g in articles}
    assert "ALERT" not in labels


def test_classifier_with_person_id_and_volume_spike_reaches_alert():
    """Closes BL-28 single-batch: 15+ negative articles about the same
    person within the same hour trigger ALERT — what "many newsrooms ran
    a critical piece" looks like in RSS."""
    base = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    # 15 articles, 4 minutes apart → 1h span at the edge → rate≈14/h → velocity=1.0
    articles = [
        _make_gold(
            f"h{i}",
            sentiment_overall=-0.6,
            person_id="person-x",
            published_at=base + timedelta(minutes=4 * i),
        )
        for i in range(15)
    ]

    _classify_political_sentiment(articles)

    labels = [g.sentiment_label for g in articles]
    assert all(label == "ALERT" for label in labels), labels
    # Lineage stamps must still fire end-to-end.
    assert articles[0].rule_version
    assert articles[0].model_version


def test_classifier_with_person_id_but_low_volume_stays_warning():
    """A single negative article about a person — high polarity,
    but volume=1 < ALERT volume threshold (15) → WARNING."""
    article = _make_gold(
        "h1",
        sentiment_overall=-0.7,
        person_id="person-y",
        published_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )

    _classify_political_sentiment([article])

    assert article.sentiment_label == "WARNING"


def test_classifier_handles_none_polarity():
    article = _make_gold("h-none", None)

    _classify_political_sentiment([article])

    assert article.sentiment_label in {"FAVORABLE", "WARNING", "ALERT"}
    assert article.confidence_score is not None


def test_classifier_noop_on_empty_list():
    _classify_political_sentiment([])  # must not raise


# --- Stage 1E v2 — shadow A/B path -----------------------------------------


def _shadow_scorer(tmp_path, yaml_body: str):
    p = tmp_path / "candidate.yaml"
    p.write_text(yaml_body)
    return build_shadow_scorer(
        yaml_path=str(p),
        enabled=True,
        region="rn",
        tenant_id="default",
        pipeline_version="9.9.9",
        source_type="rss",
    )


def test_classify_returns_empty_when_no_shadow_scorer():
    articles = [_make_gold("h1", 0.5), _make_gold("h2", -0.3)]
    shadow_rows = _classify_political_sentiment(articles)
    assert shadow_rows == []
    # Primary path untouched.
    assert all(g.sentiment_label for g in articles)


def test_classify_emits_one_shadow_row_per_article(tmp_path):
    scorer = _shadow_scorer(tmp_path, "polarity_negative: -0.50\n")
    articles = [_make_gold("h1", 0.5), _make_gold("h2", -0.3), _make_gold("h3", 0.0)]

    shadow_rows = _classify_political_sentiment(articles, scorer)

    assert len(shadow_rows) == 3
    assert {r.content_hash for r in shadow_rows} == {"h1", "h2", "h3"}
    for r in shadow_rows:
        assert r.source_type == "rss"
        assert r.region == "rn"
        assert r.tenant_id == "default"
        assert r.pipeline_version == "9.9.9"
        assert r.primary_label in {"FAVORABLE", "WARNING", "ALERT"}
        assert r.shadow_label in {"FAVORABLE", "WARNING", "ALERT"}


def test_shadow_primary_snapshot_matches_stamped_gold(tmp_path):
    """The shadow row's primary_* fields mirror what landed on the gold row."""
    scorer = _shadow_scorer(tmp_path, "polarity_negative: -0.50\n")
    article = _make_gold("h1", -0.6)

    shadow_rows = _classify_political_sentiment([article], scorer)

    row = shadow_rows[0]
    assert row.primary_label == article.sentiment_label
    assert row.primary_rule_version == article.rule_version
    assert row.primary_confidence == article.confidence_score
    assert row.primary_risk_score == article.risk_score


def test_shadow_candidate_diverges_from_primary_on_tightened_alert(tmp_path):
    """A tightened polarity threshold demotes a borderline ALERT under shadow."""
    base = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    # 15 negative articles about one person → primary ALERT (volume spike).
    articles = [
        _make_gold(
            f"h{i}",
            sentiment_overall=-0.40,
            person_id="person-x",
            published_at=base + timedelta(minutes=4 * i),
        )
        for i in range(15)
    ]
    # Candidate tightens polarity_negative to -0.50 → -0.40 no longer ALERT.
    scorer = _shadow_scorer(tmp_path, "polarity_negative: -0.50\n")

    shadow_rows = _classify_political_sentiment(articles, scorer)

    assert all(r.primary_label == "ALERT" for r in shadow_rows)
    assert all(r.shadow_label != "ALERT" for r in shadow_rows)
    assert shadow_rows[0].shadow_rule_version != shadow_rows[0].primary_rule_version


def test_shadow_noop_on_empty_list(tmp_path):
    scorer = _shadow_scorer(tmp_path, "polarity_negative: -0.40\n")
    assert _classify_political_sentiment([], scorer) == []
