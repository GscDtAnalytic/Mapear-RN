"""Coverage guard for TRACKED_TABLES.

Ensures that every critical table is present and well-formed.
Fails fast with a diff if a table was accidentally dropped or never added.
"""

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from main import _LAST_MODIFIED, TRACKED_TABLES, compute_staleness_minutes  # noqa: E402

VALID_DATASETS = {"mapear_raw", "mapear_silver", "mapear_gold"}
VALID_SINCE_COLUMNS = {"extracted_at", _LAST_MODIFIED}

# Ground truth: every table that must be present. Adding a table here forces
# a corresponding TRACKED_TABLES entry; removing one should come with a
# deliberate update to both this list and the emitter.
REQUIRED_TABLES = {
    # raw — RSS
    "mapear_raw.raw_articles",
    # raw — social
    "mapear_raw.raw_social_posts_facebook",
    "mapear_raw.raw_social_posts_instagram",
    "mapear_raw.raw_social_posts_x",
    "mapear_raw.raw_social_posts_tiktok",
    # silver
    "mapear_silver.silver_articles",
    "mapear_silver.silver_social_posts",
    # gold — RSS pipeline direct write
    "mapear_gold.gold_articles",
    # gold — dbt marts
    "mapear_gold.mapear_events",
    "mapear_gold.fct_content",
    "mapear_gold.fct_content_gold",
    "mapear_gold.fct_entity_sentiment",
    "mapear_gold.fct_trends",
    "mapear_gold.dim_topics",
}


def test_minimum_coverage():
    assert (
        len(TRACKED_TABLES) >= 14
    ), f"Expected ≥14 entries in TRACKED_TABLES, got {len(TRACKED_TABLES)}"


def test_entry_structure():
    for entry in TRACKED_TABLES:
        assert (
            isinstance(entry, tuple) and len(entry) == 2
        ), f"Each entry must be a 2-tuple, got: {entry!r}"
        table_fqn, since_col = entry
        assert isinstance(table_fqn, str), f"table_fqn must be str, got: {table_fqn!r}"
        assert isinstance(since_col, str), f"since_col must be str, got: {since_col!r}"


def test_dataset_prefixes():
    for table_fqn, _ in TRACKED_TABLES:
        parts = table_fqn.split(".")
        assert len(parts) == 2, f"Expected 'dataset.table' format, got: {table_fqn!r}"
        dataset = parts[0]
        assert dataset in VALID_DATASETS, (
            f"Unknown dataset '{dataset}' in '{table_fqn}'. " f"Valid: {VALID_DATASETS}"
        )


def test_since_column_values():
    for table_fqn, since_col in TRACKED_TABLES:
        assert since_col in VALID_SINCE_COLUMNS, (
            f"Invalid since_column '{since_col}' for '{table_fqn}'. "
            f"Valid: {VALID_SINCE_COLUMNS}"
        )


def test_required_tables_present():
    tracked = {table_fqn for table_fqn, _ in TRACKED_TABLES}
    missing = REQUIRED_TABLES - tracked
    assert not missing, "Required tables missing from TRACKED_TABLES:\n" + "\n".join(
        f"  - {t}" for t in sorted(missing)
    )


# ---------------------------------------------------------------------------
# Staleness simulation — pure arithmetic, no BQ connection needed
# ---------------------------------------------------------------------------


def test_fresh_table_emits_low_staleness():
    """Table written 30 min ago reports ~30 min staleness."""
    now = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)
    last_written = now - timedelta(minutes=30)
    result = compute_staleness_minutes(now, last_written)
    assert result == 30.0


def test_stale_rss_table_emits_above_threshold():
    """17h-stale RSS table (1020 min) exceeds the rss threshold of 960 min."""
    now = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)
    last_written = now - timedelta(hours=17)
    result = compute_staleness_minutes(now, last_written)
    assert result == 1020.0
    assert result > 960, "Should exceed rss threshold (960 min)"


def test_stale_x_table_below_threshold():
    """60h-stale X table (3600 min) is below the x threshold of 4320 min — no alert."""
    now = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)
    last_written = now - timedelta(hours=60)
    result = compute_staleness_minutes(now, last_written)
    assert result == 3600.0
    assert result < 4320, "Should not exceed x threshold (4320 min)"


def test_stale_x_table_above_threshold():
    """80h-stale X table (4800 min) exceeds the x threshold of 4320 min — alert fires."""
    now = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)
    last_written = now - timedelta(hours=80)
    result = compute_staleness_minutes(now, last_written)
    assert result == 4800.0
    assert result > 4320, "Should exceed x threshold (4320 min)"
