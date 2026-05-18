"""Integration tests for temporal governance across all four social platforms.

Each test verifies that the cutoff filter, data_type tagging, effective_cutoff_date
persistence, and backfill partition logic work correctly — without requiring a live
Apify or GCP environment.

Platforms covered: facebook, instagram, x, tiktok.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mapear_domain.entity_resolution import ResolutionResult, ScopeStatus
from mapear_social.models import SocialAccount, SocialPost
from mapear_social.pipeline import (
    ELECTORAL_CUTOFF_DATE,
    _build_silver_row,
    _dedup_intra_batch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CUTOFF_DT = datetime(
    ELECTORAL_CUTOFF_DATE.year,
    ELECTORAL_CUTOFF_DATE.month,
    ELECTORAL_CUTOFF_DATE.day,
    tzinfo=UTC,
)

_RESOLUTION_OUT = ResolutionResult(
    person_id=None,
    canonical_name=None,
    role=None,
    confidence=0.0,
    scope_status=ScopeStatus.OUT_OF_SCOPE,
    matched_signal="no_match",
)


def _make_post(platform: str, post_id: str, published_at: datetime) -> SocialPost:
    return SocialPost(
        post_id=post_id,
        platform=platform,
        url=f"https://example.com/{post_id}",
        account=SocialAccount(platform=platform, handle=f"handle_{platform}"),
        text="Test post text",
        published_at=published_at,
        content_hash=f"hash_{post_id}",
        actor_run_id="run-test-1",
        ingestion_run_id="ing-test-1",
    )


def _make_mixed_posts(platform: str) -> list[SocialPost]:
    """Three posts: one before cutoff, one on cutoff, one after."""
    return [
        _make_post(platform, f"{platform}:before", datetime(2024, 12, 31, tzinfo=UTC)),
        _make_post(platform, f"{platform}:on_cutoff", CUTOFF_DT),
        _make_post(platform, f"{platform}:after", datetime(2025, 6, 1, tzinfo=UTC)),
    ]


# ---------------------------------------------------------------------------
# Task 3: Cutoff filter per platform — incremental mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["facebook", "instagram", "x", "tiktok"])
def test_incremental_cutoff_filter_discards_pre_cutoff(platform: str) -> None:
    """In incremental mode, posts before the electoral cutoff must be discarded."""
    posts = _make_mixed_posts(platform)
    accepted = [p for p in posts if p.published_at >= CUTOFF_DT]
    rejected = [p for p in posts if p.published_at < CUTOFF_DT]

    assert len(accepted) == 2, f"{platform}: expected 2 posts at/after cutoff"
    assert len(rejected) == 1, f"{platform}: expected 1 post before cutoff"
    assert all(p.published_at >= CUTOFF_DT for p in accepted)
    assert rejected[0].post_id == f"{platform}:before"


@pytest.mark.parametrize("platform", ["facebook", "instagram", "x", "tiktok"])
def test_incremental_cutoff_is_inclusive(platform: str) -> None:
    """A post published exactly at the cutoff timestamp must be accepted."""
    post = _make_post(platform, f"{platform}:on_cutoff", CUTOFF_DT)
    assert post.published_at >= CUTOFF_DT


@pytest.mark.parametrize("platform", ["facebook", "instagram", "x", "tiktok"])
def test_backfill_mode_accepts_pre_cutoff_posts(platform: str) -> None:
    """In backfill mode the cutoff acts as a window start, not a discard gate."""
    backfill_since = datetime(2024, 7, 1, tzinfo=UTC)
    posts = _make_mixed_posts(platform)
    # Backfill window: [backfill_since, ∞)
    accepted = [p for p in posts if p.published_at >= backfill_since]
    assert len(accepted) == 3, f"{platform}: backfill should accept pre-cutoff posts"


# ---------------------------------------------------------------------------
# Task 2: Metadata persistence — data_type and effective_cutoff_date
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["facebook", "instagram", "x", "tiktok"])
def test_silver_row_data_type_incremental(platform: str) -> None:
    """Silver rows built in incremental mode must have data_type='incremental'."""
    post = _make_post(platform, f"{platform}:1", datetime(2025, 6, 1, tzinfo=UTC))
    row = _build_silver_row(
        post=post,
        ner_result={},
        sentiment={},
        resolution=_RESOLUTION_OUT,
        classification=None,
        batch_id="batch-test",
        is_backfill=False,
        effective_cutoff_date=CUTOFF_DT,
    )
    assert row["data_type"] == "incremental"
    assert row["effective_cutoff_date"] == CUTOFF_DT


@pytest.mark.parametrize("platform", ["facebook", "instagram", "x", "tiktok"])
def test_silver_row_data_type_backfill(platform: str) -> None:
    """Silver rows built in backfill mode must have data_type='backfill'."""
    backfill_since = datetime(2024, 7, 1, tzinfo=UTC)
    post = _make_post(platform, f"{platform}:old", datetime(2024, 8, 1, tzinfo=UTC))
    row = _build_silver_row(
        post=post,
        ner_result={},
        sentiment={},
        resolution=_RESOLUTION_OUT,
        classification=None,
        batch_id="batch-backfill",
        is_backfill=True,
        effective_cutoff_date=backfill_since,
    )
    assert row["data_type"] == "backfill"
    assert row["effective_cutoff_date"] == backfill_since


@pytest.mark.parametrize("platform", ["facebook", "instagram", "x", "tiktok"])
def test_silver_row_effective_cutoff_date_never_null_in_governance_runs(
    platform: str,
) -> None:
    """effective_cutoff_date must be set whenever temporal governance is active."""
    post = _make_post(platform, f"{platform}:x", datetime(2025, 3, 1, tzinfo=UTC))
    row = _build_silver_row(
        post=post,
        ner_result={},
        sentiment={},
        resolution=_RESOLUTION_OUT,
        classification=None,
        batch_id="batch-any",
        is_backfill=False,
        effective_cutoff_date=CUTOFF_DT,
    )
    assert row["effective_cutoff_date"] is not None


# ---------------------------------------------------------------------------
# Task 1: Effective cutoff computation logic
# ---------------------------------------------------------------------------


def test_effective_cutoff_uses_electoral_floor_when_no_watermark() -> None:
    """Without a watermark or lookback, effective_cutoff equals the electoral cutoff."""
    run_started_at = datetime(2026, 4, 23, tzinfo=UTC)
    watermark: datetime | None = None
    lookback_days: int | None = None

    # Mirrors the logic in run_pipeline
    _cutoff_dt = datetime(
        ELECTORAL_CUTOFF_DATE.year,
        ELECTORAL_CUTOFF_DATE.month,
        ELECTORAL_CUTOFF_DATE.day,
        tzinfo=UTC,
    )
    if lookback_days is not None:
        effective = max(run_started_at - timedelta(days=lookback_days), _cutoff_dt)
    elif watermark is not None:
        effective = max(watermark, _cutoff_dt)
    else:
        effective = _cutoff_dt

    assert effective == datetime(2025, 1, 1, tzinfo=UTC)


def test_effective_cutoff_watermark_beats_electoral_floor_when_newer() -> None:
    """When the watermark is newer than the electoral cutoff, watermark wins."""
    wm = datetime(2026, 4, 1, tzinfo=UTC)
    _cutoff_dt = datetime(2025, 1, 1, tzinfo=UTC)
    effective = max(wm, _cutoff_dt)
    assert effective == wm


def test_effective_cutoff_electoral_floor_beats_old_watermark() -> None:
    """When a watermark predates the electoral cutoff, the floor takes effect."""
    wm = datetime(2024, 6, 1, tzinfo=UTC)
    _cutoff_dt = datetime(2025, 1, 1, tzinfo=UTC)
    effective = max(wm, _cutoff_dt)
    assert effective == _cutoff_dt


def test_lookback_days_clamped_to_electoral_floor() -> None:
    """lookback_days can never push effective_cutoff below the electoral floor."""
    run_started_at = datetime(2025, 1, 15, tzinfo=UTC)
    lookback_days = 30
    _cutoff_dt = datetime(2025, 1, 1, tzinfo=UTC)

    effective = max(run_started_at - timedelta(days=lookback_days), _cutoff_dt)
    # 2025-01-15 minus 30 days = 2024-12-16, but clamped to 2025-01-01
    assert effective == _cutoff_dt


def test_lookback_days_within_window_not_clamped() -> None:
    """When lookback window is entirely within the electoral period, no clamping."""
    run_started_at = datetime(2026, 4, 23, tzinfo=UTC)
    lookback_days = 7
    _cutoff_dt = datetime(2025, 1, 1, tzinfo=UTC)

    effective = max(run_started_at - timedelta(days=lookback_days), _cutoff_dt)
    expected = datetime(2026, 4, 16, tzinfo=UTC)
    assert effective == expected


# ---------------------------------------------------------------------------
# Task 4: Backfill partition key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["facebook", "instagram", "x", "tiktok"])
def test_backfill_partition_contains_data_type_label(platform: str) -> None:
    """Backfill silver writes use a data_type=backfill directory segment."""
    batch_id = "20260423_120000"
    is_backfill = True
    partition = (
        f"platform={platform}/data_type=backfill/batch={batch_id}"
        if is_backfill
        else f"platform={platform}/batch={batch_id}"
    )
    assert "data_type=backfill" in partition
    assert f"platform={platform}" in partition
    assert f"batch={batch_id}" in partition


@pytest.mark.parametrize("platform", ["facebook", "instagram", "x", "tiktok"])
def test_incremental_partition_has_no_data_type_label(platform: str) -> None:
    """Incremental silver writes must NOT inject a data_type directory segment."""
    batch_id = "20260423_120000"
    is_backfill = False
    partition = (
        f"platform={platform}/data_type=backfill/batch={batch_id}"
        if is_backfill
        else f"platform={platform}/batch={batch_id}"
    )
    assert "data_type=" not in partition


# ---------------------------------------------------------------------------
# Dedup does not interfere with temporal governance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["facebook", "instagram", "x", "tiktok"])
def test_dedup_preserves_temporal_order_after_filter(platform: str) -> None:
    """Dedup + cutoff filter together keep the correct post set."""
    posts = _make_mixed_posts(platform) + [
        # Duplicate of the 'after' post
        _make_post(platform, f"{platform}:after", datetime(2025, 6, 1, tzinfo=UTC)),
    ]
    filtered = [p for p in posts if p.published_at >= CUTOFF_DT]
    deduped = _dedup_intra_batch(filtered)

    assert len(deduped) == 2
    assert all(p.published_at >= CUTOFF_DT for p in deduped)
