"""Data quality checks for pipeline layer boundaries.

Runs mandatory inline checks (pandas-only, zero extra deps) that enforce
schema invariants at Raw→Silver→Gold transitions.  When great_expectations
is installed, an additional GE validation pass is executed on top.

Inline checks catch the most critical issues (nulls in required columns,
empty DataFrames, type violations).  GE suites catch statistical / range
expectations that are harder to express in plain pandas.
"""

from pathlib import Path

import pandas as pd
from loguru import logger

# ---------------------------------------------------------------------------
# Inline (always-on) validators
# ---------------------------------------------------------------------------

# --- RSS column sets ---
_RAW_REQUIRED_COLS = {"url", "source_feed", "title", "content", "content_hash"}
_SILVER_REQUIRED_COLS = {"url", "source_feed", "title", "content_clean", "content_hash"}
_GOLD_REQUIRED_COLS = {
    "url",
    "source_feed",
    "title",
    "content_clean",
    "content_hash",
    "content_rn_relevant",
}


class InlineValidationError(Exception):
    """Raised when an inline quality check fails."""


def _check_not_empty(df: pd.DataFrame, layer: str) -> list[str]:
    """Reject empty DataFrames."""
    if df.empty:
        return [f"{layer}: DataFrame is empty"]
    return []


def _check_required_columns(
    df: pd.DataFrame, required: set[str], layer: str
) -> list[str]:
    """Ensure all required columns exist."""
    missing = required - set(df.columns)
    if missing:
        return [f"{layer}: missing columns {sorted(missing)}"]
    return []


def _check_no_nulls(df: pd.DataFrame, columns: set[str], layer: str) -> list[str]:
    """Ensure no nulls in required columns (only checks columns that exist)."""
    failures = []
    for col in columns & set(df.columns):
        null_count = df[col].isna().sum()
        if null_count > 0:
            failures.append(f"{layer}: {null_count} null(s) in column '{col}'")
    return failures


def _check_no_duplicates(df: pd.DataFrame, column: str, layer: str) -> list[str]:
    """Detect duplicate values in a dedup-key column within a batch."""
    if column not in df.columns:
        return []
    dup_count = df[column].duplicated().sum()
    if dup_count > 0:
        return [f"{layer}: {dup_count} duplicate(s) in '{column}'"]
    return []


def _validate_inline(
    df: pd.DataFrame,
    required_cols: set[str],
    layer: str,
    dedup_col: str = "content_hash",
) -> list[str]:
    """Run all inline checks and return a list of failure messages."""
    failures: list[str] = []
    failures.extend(_check_not_empty(df, layer))
    if failures:
        return failures  # nothing else to check on empty df
    failures.extend(_check_required_columns(df, required_cols, layer))
    failures.extend(_check_no_nulls(df, required_cols, layer))
    failures.extend(_check_no_duplicates(df, dedup_col, layer))
    return failures


# ---------------------------------------------------------------------------
# Great Expectations (optional, additive)
# ---------------------------------------------------------------------------


class QualityChecker:
    """Runs Great Expectations validation suites on DataFrames."""

    def __init__(self, ge_dir: Path | None = None) -> None:
        self._ge_dir = ge_dir or Path("great_expectations")

    @property
    def expectations_dir(self) -> Path:
        return self._ge_dir / "expectations"

    def validate(
        self,
        df: pd.DataFrame,
        suite_name: str,
    ) -> bool:
        """Validate a DataFrame against a named expectation suite."""
        try:
            import great_expectations as gx

            context = gx.get_context()

            datasource = context.sources.add_or_update_pandas(name="pipeline")
            data_asset = datasource.add_dataframe_asset(name=suite_name)
            batch_request = data_asset.build_batch_request(dataframe=df)

            suite_path = self.expectations_dir / f"{suite_name}.json"
            if not suite_path.exists():
                logger.warning(
                    "Suite {suite} not found at {path}, skipping GE validation",
                    suite=suite_name,
                    path=str(suite_path),
                )
                return True

            checkpoint = context.add_or_update_checkpoint(
                name=f"checkpoint_{suite_name}",
                validations=[
                    {
                        "batch_request": batch_request,
                        "expectation_suite_name": suite_name,
                    }
                ],
            )

            result = checkpoint.run()
            success = result.success

            if success:
                logger.info(
                    "GE quality check PASSED: {suite}",
                    suite=suite_name,
                )
            else:
                failed = []
                for r in result.run_results.values():
                    for res in r["validation_result"]["results"]:
                        if not res["success"]:
                            failed.append(res["expectation_config"]["expectation_type"])

                logger.error(
                    "GE quality check FAILED: {suite} — failed: {failed}",
                    suite=suite_name,
                    failed=failed,
                )

            return success

        except ImportError:
            logger.debug("great_expectations not installed, skipping GE validation")
            return True


# ---------------------------------------------------------------------------
# Public API — inline checks always run; GE runs when available
# ---------------------------------------------------------------------------


def _run_gate(
    df: pd.DataFrame,
    required_cols: set[str],
    layer: str,
    ge_suite: str,
    dedup_col: str = "content_hash",
) -> bool:
    """Shared gate: inline checks + optional GE."""
    failures = _validate_inline(df, required_cols, layer, dedup_col)
    if failures:
        for msg in failures:
            logger.error("Quality gate FAILED: {msg}", msg=msg)
        return False
    return QualityChecker().validate(df, ge_suite)


# --- RSS gates ---


def validate_raw(df: pd.DataFrame) -> bool:
    """Validate raw RSS articles before writing to lake."""
    return _run_gate(df, _RAW_REQUIRED_COLS, "raw", "raw_articles_suite")


def validate_silver(df: pd.DataFrame) -> bool:
    """Validate silver RSS articles before writing to lake."""
    return _run_gate(df, _SILVER_REQUIRED_COLS, "silver", "silver_articles_suite")


def validate_gold(df: pd.DataFrame) -> bool:
    """Validate gold RSS articles before writing to lake."""
    return _run_gate(df, _GOLD_REQUIRED_COLS, "gold", "gold_articles_suite")


# ---------------------------------------------------------------------------
# Quality report generation
# ---------------------------------------------------------------------------

# Fields where > 50% null rate is a critical failure
_CRITICAL_FIELDS_RSS: frozenset[str] = frozenset(
    {"published_at", "content_clean", "sentiment_overall"}
)

_CRITICAL_FIELDS_SOCIAL: frozenset[str] = frozenset(
    {"post_id", "platform", "published_at", "content_hash"}
)
"""Campos cuja ausência em silver_social_posts indica corrupção/registro inutilizável.
Justificativa em docs/sprint3_b6_critical_fields_social.md.
"""

_CRITICAL_FIELDS_BY_SOURCE: dict[str, frozenset[str]] = {
    "rss": _CRITICAL_FIELDS_RSS,
    "social": _CRITICAL_FIELDS_SOCIAL,
}


def generate_quality_report(
    df: pd.DataFrame,
    layer: str,
    source_type: str = "rss",
) -> dict:
    """Generate a quality report for a DataFrame at any pipeline layer.

    Returns a dict with record counts, null rates, coverage stats, and
    deduplication metrics. Logs ERROR if critical fields exceed 50% null.

    Args:
        df: The DataFrame to analyze.
        layer: Pipeline layer name (raw, silver, gold).
        source_type: One of "rss" or "social". Unknown values fallback to RSS behavior.
            Critical fields are source-aware: see _CRITICAL_FIELDS_BY_SOURCE.

    Returns:
        Quality report dict suitable for JSON serialization.
    """
    if df.empty:
        logger.error("Quality report: {layer} DataFrame is empty", layer=layer)
        return {"total_records": 0, "error": "empty_dataframe"}

    total = len(df)

    # Null rates for all columns
    fields_null_rate: dict[str, str] = {}
    for col in df.columns:
        null_count = df[col].isna().sum()
        # Also count empty strings and empty lists as "null"
        if df[col].dtype == object:
            empty_count = (
                df[col]
                .apply(
                    lambda x: (
                        x is None
                        or (isinstance(x, str) and not x.strip())
                        or (isinstance(x, list) and len(x) == 0)
                    )
                )
                .sum()
            )
            null_count = max(null_count, empty_count)
        rate = round(null_count / total * 100, 1)
        fields_null_rate[col] = f"{rate}%"

    # Dedup stats
    dedup_col = {"rss": "content_hash", "social": "post_id"}.get(
        source_type, "content_hash"
    )
    unique_records = df[dedup_col].nunique() if dedup_col in df.columns else total
    duplicates = total - unique_records

    # Content relevance: canonical field is content_rn_relevant (V2);
    # fall back to is_rn_relevant for V1 legacy rows.
    content_relevant_rate = "N/A"
    _relevance_col = next(
        (c for c in ("content_rn_relevant", "is_rn_relevant") if c in df.columns),
        None,
    )
    if _relevance_col:
        rn_count = df[_relevance_col].sum()
        content_relevant_rate = f"{round(rn_count / total * 100, 1)}%"

    # City coverage
    cities_coverage: dict[str, int] = {}
    if "mentioned_cities" in df.columns:
        for cities in df["mentioned_cities"].dropna():
            if isinstance(cities, list):
                for city in cities:
                    cities_coverage[city] = cities_coverage.get(city, 0) + 1

    # Persons coverage
    persons_coverage: dict[str, int] = {}
    persons_col = "mentioned_persons" if "mentioned_persons" in df.columns else None
    if persons_col is None and "mentioned_mayors" in df.columns:
        persons_col = "mentioned_mayors"
    if persons_col and persons_col in df.columns:
        for persons in df[persons_col].dropna():
            if isinstance(persons, list):
                for person in persons:
                    persons_coverage[person] = persons_coverage.get(person, 0) + 1

    report = {
        "total_records": total,
        "unique_records": unique_records,
        "duplicates_removed": duplicates,
        "fields_null_rate": fields_null_rate,
        "content_relevant_rate": content_relevant_rate,
        "cities_coverage": dict(
            sorted(cities_coverage.items(), key=lambda x: x[1], reverse=True)
        ),
        "persons_coverage": dict(
            sorted(persons_coverage.items(), key=lambda x: x[1], reverse=True)
        ),
    }

    # Critical field null rate check
    critical_fields = _CRITICAL_FIELDS_BY_SOURCE.get(source_type, _CRITICAL_FIELDS_RSS)
    has_critical_failure = False
    for field in critical_fields:
        if field in df.columns:
            null_count = df[field].isna().sum()
            if df[field].dtype == object:
                null_count = max(
                    null_count,
                    df[field]
                    .apply(
                        lambda x: x is None or (isinstance(x, str) and not x.strip())
                    )
                    .sum(),
                )
            rate = null_count / total
            if rate > 0.5:
                logger.error(
                    "Quality CRITICAL: {field} is {rate}% null in {layer} "
                    "(threshold: 50%)",
                    field=field,
                    rate=round(rate * 100, 1),
                    layer=layer,
                )
                has_critical_failure = True

    report["critical_failure"] = has_critical_failure

    logger.info(
        "Quality report ({layer}): {total} records, {unique} unique, "
        "rn_relevant={rn_rate}, cities={n_cities}, persons={n_persons}",
        layer=layer,
        total=total,
        unique=unique_records,
        rn_rate=content_relevant_rate,
        n_cities=len(cities_coverage),
        n_persons=len(persons_coverage),
    )

    return report
