"""Source diversity scoring for RSS batches.

Computes the Herfindahl-Hirschman Index (HHI) over source feeds and
alerts when a single source dominates the batch beyond a threshold.
HHI ranges from 1/n (perfectly distributed across n sources) to 1.0
(all articles from a single source).
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from mapear_domain.models.base import RawArticle

DEFAULT_CONCENTRATION_THRESHOLD: float = 0.70


@dataclass
class DiversityReport:
    """Diversity metrics for a batch of articles."""

    total_articles: int
    unique_sources: int
    # Sorted descending by article count: {feed_url: count}
    source_distribution: dict[str, int]
    # HHI: 0.0 = perfectly diverse (theoretical), 1.0 = monopoly
    source_concentration_index: float
    dominant_source: str | None
    dominant_source_share: float  # raw fraction (0.0–1.0)
    concentration_alert: bool
    threshold: float


class DiversityScorer:
    """Computes source diversity metrics for a batch of raw articles."""

    def __init__(self, threshold: float = DEFAULT_CONCENTRATION_THRESHOLD) -> None:
        self._threshold = threshold

    def compute(self, articles: list[RawArticle]) -> DiversityReport:
        """Compute diversity metrics for the given article batch."""
        if not articles:
            return DiversityReport(
                total_articles=0,
                unique_sources=0,
                source_distribution={},
                source_concentration_index=0.0,
                dominant_source=None,
                dominant_source_share=0.0,
                concentration_alert=False,
                threshold=self._threshold,
            )

        counts: dict[str, int] = {}
        for a in articles:
            counts[a.source_feed] = counts.get(a.source_feed, 0) + 1

        total = len(articles)
        hhi = sum((c / total) ** 2 for c in counts.values())
        dominant = max(counts, key=lambda k: counts[k])
        dominant_share = counts[dominant] / total
        alert = dominant_share > self._threshold

        if alert:
            logger.warning(
                "source_concentration_alert — source={source} holds {pct:.1f}% "
                "of batch ({count}/{total}). Threshold: {thr:.0f}%. "
                "Add or reactivate feeds to dilute concentration.",
                source=dominant,
                pct=dominant_share * 100,
                count=counts[dominant],
                total=total,
                thr=self._threshold * 100,
            )

        report = DiversityReport(
            total_articles=total,
            unique_sources=len(counts),
            source_distribution=dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            source_concentration_index=round(hhi, 4),
            dominant_source=dominant,
            dominant_source_share=round(dominant_share, 4),
            concentration_alert=alert,
            threshold=self._threshold,
        )

        logger.info(
            "diversity — sources={n}, HHI={hhi:.4f}, "
            "dominant={dom} ({pct:.1f}%){alert}",
            n=report.unique_sources,
            hhi=hhi,
            dom=dominant,
            pct=dominant_share * 100,
            alert=" [ALERT]" if alert else "",
        )

        return report

    def to_dict(self, report: DiversityReport) -> dict:
        """Return a JSON-serializable representation for the run_report."""
        return {
            "total_articles": report.total_articles,
            "unique_sources": report.unique_sources,
            "source_distribution": report.source_distribution,
            "source_concentration_index": report.source_concentration_index,
            "dominant_source": report.dominant_source,
            "dominant_source_share": report.dominant_source_share,
            "concentration_alert": report.concentration_alert,
            "threshold": report.threshold,
        }
