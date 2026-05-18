"""Batch-level data quality reporter for MAPEAR pipelines.

Checks six categories per batch and returns a structured PASS/FAIL report:
  1. completeness   — IDs, URLs, timestamps
  2. semantic       — enum values, numeric score ranges
  3. enrichment     — stoplist entity leak, city/mayor coverage
  4. temporal       — published_at before incremental cutoff
  5. distribution   — near-constant resolution_confidence / sentiment_confidence
  6. consistency    — cross-field invariants (inscope→person_id, sentiment pairing)

Usage::

    from mapear_infra.quality_report import BatchQualityChecker, QualityThresholds

    checker = BatchQualityChecker(QualityThresholds())
    report  = checker.run(df, source_type="social", batch_id="20260423_apify")
    report.log_summary()
    print(report.to_json())
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import pandas as pd
from loguru import logger

# ---------------------------------------------------------------------------
# Constants — shared with dbt layer; keep in sync with quality_thresholds macro
# ---------------------------------------------------------------------------

_SENTIMENT_LABELS = {"FAVORABLE", "WARNING", "ALERT"}
_SCOPE_STATUSES = {"IN_SCOPE", "OUT_OF_SCOPE", "AMBIGUOUS"}
_PLATFORMS = {"facebook", "instagram", "x", "tiktok", "rss"}
_EVENT_TYPES = {"article", "post"}
_SOURCE_TYPES = {"rss", "social"}
_DATA_TYPES = {"backfill", "incremental"}

_PK_BY_SOURCE = {"rss": "content_hash", "social": "post_id"}


# ---------------------------------------------------------------------------
# Public API types
# ---------------------------------------------------------------------------


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


@dataclass
class QualityThresholds:
    """All configurable thresholds; override per source or environment."""

    temporal_cutoff: datetime = field(
        default_factory=lambda: datetime(2025, 1, 1, tzinfo=UTC)
    )
    # Enrichment: min % of rows that must mention ≥1 city
    min_city_coverage_pct_rss: float = 0.10
    min_city_coverage_pct_social: float = 0.20
    # Enrichment: min % of in-scope rows that must mention ≥1 person
    min_mayor_coverage_pct: float = 0.30
    # Enrichment: max total entity mentions per document (stoplist leak proxy)
    max_entity_mentions_per_doc: int = 50
    # Distribution: min std-dev before flagging near-constant distribution
    min_resolution_confidence_stddev: float = 0.01
    min_sentiment_confidence_stddev: float = 0.05
    # Sample size gates
    min_rows_coverage_check: int = 20
    min_rows_distribution_check: int = 100


@dataclass
class CheckResult:
    name: str
    category: str
    status: CheckStatus
    description: str
    failing_rows: int = 0
    total_rows: int = 0
    threshold: Any = None
    actual_value: Any = None

    @property
    def pass_rate(self) -> float | None:
        if self.total_rows == 0:
            return None
        return round((self.total_rows - self.failing_rows) / self.total_rows, 4)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "status": self.status.value,
            "description": self.description,
            "failing_rows": self.failing_rows,
            "total_rows": self.total_rows,
            "pass_rate": self.pass_rate,
            "threshold": self.threshold,
            "actual_value": self.actual_value,
        }


@dataclass
class QualityReport:
    batch_id: str
    source_type: str
    generated_at: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def status(self) -> CheckStatus:
        statuses = {c.status for c in self.checks}
        if CheckStatus.FAIL in statuses:
            return CheckStatus.FAIL
        if CheckStatus.WARN in statuses:
            return CheckStatus.WARN
        return CheckStatus.PASS

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == CheckStatus.FAIL]

    @property
    def warned_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == CheckStatus.WARN]

    def log_summary(self) -> None:
        n = {s: 0 for s in CheckStatus}
        for c in self.checks:
            n[c.status] += 1
        log_fn = (
            logger.error
            if self.status == CheckStatus.FAIL
            else logger.warning if self.status == CheckStatus.WARN else logger.info
        )
        log_fn(
            "Quality report [{batch}] {src} — {status}: " "{p}✓ {w}⚠ {f}✗ {s}↷",
            batch=self.batch_id,
            src=self.source_type,
            status=self.status.value,
            p=n[CheckStatus.PASS],
            w=n[CheckStatus.WARN],
            f=n[CheckStatus.FAIL],
            s=n[CheckStatus.SKIP],
        )
        for c in self.failed_checks:
            logger.error(
                "  FAIL [{cat}] {name}: {desc} ({fail}/{total})",
                cat=c.category,
                name=c.name,
                desc=c.description,
                fail=c.failing_rows,
                total=c.total_rows,
            )
        for c in self.warned_checks:
            logger.warning(
                "  WARN [{cat}] {name}: actual={actual} threshold={thr}",
                cat=c.category,
                name=c.name,
                actual=c.actual_value,
                thr=c.threshold,
            )

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "source_type": self.source_type,
            "generated_at": self.generated_at,
            "overall_status": self.status.value,
            "summary": {
                "total_checks": len(self.checks),
                "pass": sum(1 for c in self.checks if c.status == CheckStatus.PASS),
                "warn": len(self.warned_checks),
                "fail": len(self.failed_checks),
                "skip": sum(1 for c in self.checks if c.status == CheckStatus.SKIP),
            },
            "checks": [c.to_dict() for c in self.checks],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


class BatchQualityChecker:
    def __init__(self, thresholds: QualityThresholds | None = None) -> None:
        self._t = thresholds or QualityThresholds()

    # --- helpers ---

    def _array_len(self, series: pd.Series) -> pd.Series:
        return series.apply(lambda x: len(x) if isinstance(x, list) else 0)

    def _total_entity_mentions(self, df: pd.DataFrame) -> pd.Series:
        total = pd.Series(0, index=df.index, dtype=int)
        for col in (
            "mentioned_cities",
            "mentioned_mayors",
            "mentioned_persons",
            "mentioned_parties",
        ):
            if col in df.columns:
                total = total + self._array_len(df[col])
        return total

    def _pass(self, name: str, cat: str, desc: str, n: int) -> CheckResult:
        return CheckResult(
            name=name,
            category=cat,
            status=CheckStatus.PASS,
            description=desc,
            total_rows=n,
        )

    def _fail(
        self, name: str, cat: str, desc: str, failing: int, total: int, **kw: Any
    ) -> CheckResult:
        return CheckResult(
            name=name,
            category=cat,
            status=CheckStatus.FAIL,
            description=desc,
            failing_rows=failing,
            total_rows=total,
            **kw,
        )

    def _warn(
        self, name: str, cat: str, desc: str, failing: int, total: int, **kw: Any
    ) -> CheckResult:
        return CheckResult(
            name=name,
            category=cat,
            status=CheckStatus.WARN,
            description=desc,
            failing_rows=failing,
            total_rows=total,
            **kw,
        )

    def _skip(
        self, name: str, cat: str, desc: str, total: int, **kw: Any
    ) -> CheckResult:
        return CheckResult(
            name=name,
            category=cat,
            status=CheckStatus.SKIP,
            description=desc,
            total_rows=total,
            **kw,
        )

    # --- 1. Completeness ---------------------------------------------------

    def check_completeness(self, df: pd.DataFrame) -> list[CheckResult]:
        results: list[CheckResult] = []
        n = len(df)

        for col in ("published_at", "extracted_at"):
            if col not in df.columns:
                results.append(
                    self._fail(
                        f"not_null_{col}",
                        "completeness",
                        f"Coluna obrigatória '{col}' ausente",
                        n,
                        n,
                    )
                )
                continue
            null_count = int(df[col].isna().sum())
            result_fn = self._fail if null_count else self._pass
            results.append(
                result_fn(
                    f"not_null_{col}",
                    "completeness",
                    f"'{col}' não deve ser NULL",
                    **({} if not null_count else {"failing": null_count, "total": n}),
                    n=n,
                )
                if null_count == 0
                else self._fail(
                    f"not_null_{col}",
                    "completeness",
                    f"'{col}' não deve ser NULL",
                    null_count,
                    n,
                )
            )

        src = (
            df["source_type"].iloc[0] if "source_type" in df.columns and n > 0 else None
        )
        pk = _PK_BY_SOURCE.get(str(src), "content_hash")
        if pk in df.columns:
            null_count = int(df[pk].isna().sum())
            dup_count = int(df[pk].duplicated().sum())
            results.append(
                self._fail(
                    f"not_null_{pk}",
                    "completeness",
                    f"PK '{pk}' não deve ser NULL",
                    null_count,
                    n,
                )
                if null_count
                else self._pass(
                    f"not_null_{pk}", "completeness", f"PK '{pk}' não nulo", n
                )
            )
            results.append(
                self._fail(
                    f"unique_{pk}",
                    "completeness",
                    f"PK '{pk}' duplicado no batch",
                    dup_count,
                    n,
                )
                if dup_count
                else self._pass(f"unique_{pk}", "completeness", f"PK '{pk}' único", n)
            )

        if "url" in df.columns and "source_type" in df.columns:
            url_required = df["source_type"].isin(["rss", "social"])
            null_count = int((df["url"].isna() & url_required).sum())
            results.append(
                self._fail(
                    "not_null_url_rss_social",
                    "completeness",
                    "URL obrigatória para RSS e Social",
                    null_count,
                    n,
                )
                if null_count
                else self._pass(
                    "not_null_url_rss_social",
                    "completeness",
                    "URL presente para RSS e Social",
                    n,
                )
            )

        return results

    # --- 2. Semantic validity ----------------------------------------------

    def check_semantic_validity(self, df: pd.DataFrame) -> list[CheckResult]:
        results: list[CheckResult] = []
        n = len(df)

        enum_checks = [
            ("sentiment_label", _SENTIMENT_LABELS, False),
            ("scope_status", _SCOPE_STATUSES, False),
            ("platform", _PLATFORMS, True),
            ("source_type", _SOURCE_TYPES, True),
            ("event_type", _EVENT_TYPES, True),
        ]
        if "data_type" in df.columns:
            enum_checks.append(("data_type", _DATA_TYPES, True))

        for col, valid, required in enum_checks:
            if col not in df.columns:
                continue
            non_null = df[col].notna()
            invalid_count = int((non_null & ~df[col].isin(valid)).sum())
            null_count = int(df[col].isna().sum())

            if required and null_count:
                results.append(
                    self._fail(
                        f"not_null_{col}",
                        "semantic",
                        f"'{col}' obrigatório está NULL",
                        null_count,
                        n,
                    )
                )

            results.append(
                self._fail(
                    f"valid_enum_{col}",
                    "semantic",
                    f"'{col}' fora dos valores válidos {sorted(valid)}",
                    invalid_count,
                    n,
                    threshold=sorted(valid),
                    actual_value=list(
                        df.loc[non_null & ~df[col].isin(valid), col].unique()
                    ),
                )
                if invalid_count
                else self._pass(
                    f"valid_enum_{col}", "semantic", f"'{col}' todos válidos", n
                )
            )

        score_ranges = [
            ("resolution_confidence", 0.0, 1.0),
            ("sentiment_confidence", 0.0, 1.0),
            ("confidence_score", 0.0, 1.0),
            ("risk_score", 0.0, 1.0),
        ]
        for col, lo, hi in score_ranges:
            if col not in df.columns:
                continue
            oor = int((df[col].notna() & ((df[col] < lo) | (df[col] > hi))).sum())
            results.append(
                self._fail(
                    f"range_{col}",
                    "semantic",
                    f"'{col}' deve estar em [{lo}, {hi}]",
                    oor,
                    n,
                    threshold=f"[{lo}, {hi}]",
                )
                if oor
                else self._pass(
                    f"range_{col}", "semantic", f"'{col}' dentro de [{lo}, {hi}]", n
                )
            )

        if "sentiment_overall" in df.columns:
            oor = int(
                (
                    df["sentiment_overall"].notna()
                    & ((df["sentiment_overall"] < -1) | (df["sentiment_overall"] > 1))
                ).sum()
            )
            results.append(
                self._fail(
                    "range_sentiment_overall",
                    "semantic",
                    "'sentiment_overall' deve estar em [-1, 1]",
                    oor,
                    n,
                    threshold="[-1, 1]",
                )
                if oor
                else self._pass(
                    "range_sentiment_overall",
                    "semantic",
                    "'sentiment_overall' dentro de [-1, 1]",
                    n,
                )
            )

        return results

    # --- 3. Enrichment quality ---------------------------------------------

    def check_enrichment_quality(
        self, df: pd.DataFrame, source_type: str
    ) -> list[CheckResult]:
        results: list[CheckResult] = []
        n = len(df)
        t = self._t

        # 3a — stoplist leak
        totals = self._total_entity_mentions(df)
        overloaded = int((totals > t.max_entity_mentions_per_doc).sum())
        avg = round(float(totals.mean()), 1) if n else 0.0
        results.append(
            self._warn(
                "stoplist_entity_leak",
                "enrichment",
                f"Documentos com >{t.max_entity_mentions_per_doc} menções de entidades "
                f"(média do batch: {avg}) — possível vazamento de stoplist",
                overloaded,
                n,
                threshold=t.max_entity_mentions_per_doc,
                actual_value=avg,
            )
            if overloaded
            else self._pass(
                "stoplist_entity_leak",
                "enrichment",
                "Contagem de entidades dentro do esperado",
                n,
            )
        )

        # 3b — city coverage
        if "mentioned_cities" in df.columns:
            min_pct = {
                "rss": t.min_city_coverage_pct_rss,
                "social": t.min_city_coverage_pct_social,
            }.get(source_type, 0.0)
            has_cities = self._array_len(df["mentioned_cities"]) > 0
            coverage = round(float(has_cities.mean()), 4) if n else 0.0
            zero_count = int((~has_cities).sum())
            if n < t.min_rows_coverage_check:
                results.append(
                    self._skip(
                        f"city_coverage_{source_type}",
                        "enrichment",
                        "Amostra insuficiente para verificar cobertura de cidades",
                        n,
                        threshold=t.min_rows_coverage_check,
                    )
                )
            elif coverage < min_pct:
                results.append(
                    self._warn(
                        f"city_coverage_{source_type}",
                        "enrichment",
                        f"Cobertura de cidades {coverage*100:.1f}%"
                        f" < mínimo {min_pct*100:.0f}%",
                        zero_count,
                        n,
                        threshold=min_pct,
                        actual_value=coverage,
                    )
                )
            else:
                results.append(
                    self._pass(
                        f"city_coverage_{source_type}",
                        "enrichment",
                        f"Cobertura de cidades {coverage*100:.1f}%"
                        f" ≥ {min_pct*100:.0f}%",
                        n,
                    )
                )

        # 3c — mayor / person coverage for in-scope rows
        person_col = next(
            (c for c in ("mentioned_mayors", "mentioned_persons") if c in df.columns),
            None,
        )
        if person_col and "author_in_scope" in df.columns:
            inscope = df[df["author_in_scope"].eq(True)]
            ni = len(inscope)
            if ni < t.min_rows_coverage_check:
                results.append(
                    self._skip(
                        "mayor_coverage_inscope",
                        "enrichment",
                        "Amostra in-scope insuficiente para checar"
                        " cobertura de prefeitos",
                        ni,
                        threshold=t.min_rows_coverage_check,
                    )
                )
            else:
                has_persons = self._array_len(inscope[person_col]) > 0
                coverage = round(float(has_persons.mean()), 4)
                zero_count = int((~has_persons).sum())
                if coverage < t.min_mayor_coverage_pct:
                    results.append(
                        self._warn(
                            "mayor_coverage_inscope",
                            "enrichment",
                            f"Cobertura de prefeitos in-scope {coverage*100:.1f}% "
                            f"< mínimo {t.min_mayor_coverage_pct*100:.0f}%",
                            zero_count,
                            ni,
                            threshold=t.min_mayor_coverage_pct,
                            actual_value=coverage,
                        )
                    )
                else:
                    results.append(
                        self._pass(
                            "mayor_coverage_inscope",
                            "enrichment",
                            f"Cobertura de prefeitos in-scope {coverage*100:.1f}%",
                            ni,
                        )
                    )

        return results

    # --- 4. Temporal -------------------------------------------------------

    def check_temporal(self, df: pd.DataFrame) -> list[CheckResult]:
        if "published_at" not in df.columns:
            return []
        n = len(df)
        cutoff = self._t.temporal_cutoff
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        ts = pd.to_datetime(df["published_at"], utc=True, errors="coerce")
        before = int((ts < cutoff).sum())
        min_date = str(ts.min()) if not ts.isna().all() else None
        return [
            (
                self._fail(
                    "temporal_cutoff_violation",
                    "temporal",
                    f"Conteúdo anterior ao cutoff {cutoff.date()}"
                    " viola filtro incremental",
                    before,
                    n,
                    threshold=cutoff.isoformat(),
                    actual_value=min_date,
                )
                if before
                else self._pass(
                    "temporal_cutoff_violation",
                    "temporal",
                    f"Todos os eventos ≥ {cutoff.date()}",
                    n,
                )
            )
        ]

    # --- 5. Distribution ---------------------------------------------------

    def check_distribution(self, df: pd.DataFrame) -> list[CheckResult]:
        results: list[CheckResult] = []
        checks = [
            ("resolution_confidence", self._t.min_resolution_confidence_stddev),
            ("sentiment_confidence", self._t.min_sentiment_confidence_stddev),
            ("confidence_score", self._t.min_sentiment_confidence_stddev),
        ]
        for col, min_std in checks:
            if col not in df.columns:
                continue
            s = df[col].dropna()
            ni = len(s)
            if ni < self._t.min_rows_distribution_check:
                results.append(
                    self._skip(
                        f"distribution_{col}",
                        "distribution",
                        f"Amostra insuficiente para checar variância de '{col}'",
                        ni,
                        threshold=self._t.min_rows_distribution_check,
                    )
                )
                continue
            stddev = round(float(s.std(ddof=1)), 6)
            if stddev < min_std:
                results.append(
                    self._warn(
                        f"distribution_{col}",
                        "distribution",
                        f"'{col}' quase constante (stddev={stddev})"
                        " — possível modelo degenerado",
                        0,
                        ni,
                        threshold=min_std,
                        actual_value=stddev,
                    )
                )
            else:
                results.append(
                    self._pass(
                        f"distribution_{col}",
                        "distribution",
                        f"'{col}' variância saudável (stddev={stddev})",
                        ni,
                    )
                )
        return results

    # --- 6. Consistency ----------------------------------------------------

    def check_consistency(self, df: pd.DataFrame) -> list[CheckResult]:
        results: list[CheckResult] = []
        n = len(df)

        if "author_in_scope" in df.columns and "scope_status" in df.columns:
            expected = df["scope_status"].eq("IN_SCOPE")
            actual = df["author_in_scope"].eq(True)
            mismatch = int((expected != actual).sum())
            results.append(
                self._fail(
                    "author_in_scope_matches_scope_status",
                    "consistency",
                    "author_in_scope deve ser TRUE ↔ scope_status='IN_SCOPE'",
                    mismatch,
                    n,
                )
                if mismatch
                else self._pass(
                    "author_in_scope_matches_scope_status",
                    "consistency",
                    "author_in_scope ↔ scope_status coerentes",
                    n,
                )
            )

        if "content_rn_relevant" in df.columns and "is_rn_relevant" in df.columns:
            mismatch = int((df["content_rn_relevant"] != df["is_rn_relevant"]).sum())
            results.append(
                self._fail(
                    "content_rn_relevant_is_rn_relevant_parity",
                    "consistency",
                    "content_rn_relevant deve ser igual a is_rn_relevant (alias V2)",
                    mismatch,
                    n,
                )
                if mismatch
                else self._pass(
                    "content_rn_relevant_is_rn_relevant_parity",
                    "consistency",
                    "content_rn_relevant = is_rn_relevant ✓",
                    n,
                )
            )

        if "author_in_scope" in df.columns and "person_id" in df.columns:
            bad = int((df["author_in_scope"].eq(True) & df["person_id"].isna()).sum())
            results.append(
                self._fail(
                    "inscope_requires_person_id",
                    "consistency",
                    "author_in_scope=TRUE exige person_id (IN_SCOPE implica resolução)",
                    bad,
                    n,
                )
                if bad
                else self._pass(
                    "inscope_requires_person_id",
                    "consistency",
                    "Todos os in-scope têm person_id ✓",
                    n,
                )
            )

        if "sentiment_label" in df.columns and "sentiment_confidence" in df.columns:
            mismatch = int(
                (
                    df["sentiment_label"].isna() != df["sentiment_confidence"].isna()
                ).sum()
            )
            results.append(
                self._fail(
                    "sentiment_label_confidence_pairing",
                    "consistency",
                    "sentiment_label e sentiment_confidence devem ser"
                    " ambos NULL ou ambos preenchidos",
                    mismatch,
                    n,
                )
                if mismatch
                else self._pass(
                    "sentiment_label_confidence_pairing",
                    "consistency",
                    "Paridade sentiment_label / confidence ✓",
                    n,
                )
            )

        return results

    # --- public entry point ------------------------------------------------

    def run(self, df: pd.DataFrame, source_type: str, batch_id: str) -> QualityReport:
        report = QualityReport(batch_id=batch_id, source_type=source_type)

        if df.empty:
            report.checks.append(
                self._fail(
                    "not_empty",
                    "completeness",
                    "DataFrame vazio — batch sem dados",
                    0,
                    0,
                )
            )
            report.log_summary()
            return report

        report.checks.extend(self.check_completeness(df))
        report.checks.extend(self.check_semantic_validity(df))
        report.checks.extend(self.check_enrichment_quality(df, source_type))
        report.checks.extend(self.check_temporal(df))
        report.checks.extend(self.check_distribution(df))
        report.checks.extend(self.check_consistency(df))

        report.log_summary()
        return report


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


def run_batch_quality_report(
    df: pd.DataFrame,
    source_type: str,
    batch_id: str,
    thresholds: QualityThresholds | None = None,
) -> QualityReport:
    """Run all quality checks on a batch DataFrame and return the report."""
    return BatchQualityChecker(thresholds).run(
        df, source_type=source_type, batch_id=batch_id
    )
