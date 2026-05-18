"""Per-person reach aggregates for the political-sentiment classifier (RSS).

Closes BL-28 in its **single-batch approximation** form: groups the
articles of the current batch by ``person_id`` and returns
``(volume_24h, velocity, engagement)`` per person, ready to feed the
classifier. Before this module the RSS pipeline was wiring zeros, which
made the ALERT branch unreachable and silently degraded every RSS row
to FAVORABLE / WARNING.

Approximation
-------------

The Cloud Scheduler cron for RSS runs every 8h, so the batch in scope
is ≤8h of articles. The classifier's WARNING / ALERT volume thresholds
(8 / 15) still discriminate meaningfully on this window:

* 8+ articles about the same person in a single 8h batch is a real
  signal — it is *more* concentrated than the same volume spread over
  24h, so calling it "volume_24h" *underestimates* a true 24h count.
  The classifier doesn't suffer from that — it just means we under-
  count slow-burns that span batches without a single-batch spike.
  Slow-burn coverage is provided by the dbt mart layer at daily grain;
  it is not the classifier's job.

* engagement is always 0. RSS feeds carry no per-article likes /
  shares / views — that signal is a social-only construct. The ALERT
  rule is ``polarity ≤ -0.35 AND velocity ≥ 0.7 AND (volume_24h ≥ 15
  OR engagement ≥ 5000)``. Without engagement, RSS ALERTs require a
  volume spike — which is the right product semantics: an ALERT for
  RSS means "many newsrooms ran a critical piece in a short window".

Cross-batch ideal
-----------------

The full BL-28 closure (rolling 24h volume that spans batches) needs
either reading prior silver from BQ at pipeline start or maintaining
a rolling sidecar table in ``mapear_silver``. Both add a storage round
trip per run and are deferred to Eixo 1 (lakehouse + streaming).

Velocity
--------

Formula mirrors :class:`mapear_nlp.trend_scorer.TrendScorer` so
both signals feeding the classifier (TrendScorer for entities,
PersonReach for ``person_id``) use comparable units — articles per
hour, normalized to ``[0.1, 1.0]`` with a 5/h cap for the saturation
point. A single article gives 0.1 (lower bound), two-or-more articles
within the same instant give 0.5 (interpreted as a burst).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime

from mapear_domain.models.base import GoldArticle

# Velocity calibration — articles/hour at which velocity saturates to 1.0.
# Mirrors TrendScorer's ``min(velocity / 5.0, 1.0)``.
_VELOCITY_SATURATION_PER_HOUR = 5.0


def compute_rss_reach_per_person(
    articles: Iterable[GoldArticle],
) -> dict[str, tuple[int, float, int]]:
    """Group articles by person_id; return (volume_24h, velocity, engagement)."""
    by_person: dict[str, list[GoldArticle]] = defaultdict(list)
    for article in articles:
        if not article.person_id:
            continue
        by_person[article.person_id].append(article)

    out: dict[str, tuple[int, float, int]] = {}
    for person_id, group in by_person.items():
        out[person_id] = (
            len(group),
            _velocity(group),
            0,  # see module docstring — RSS has no engagement signal
        )
    return out


def _velocity(articles: list[GoldArticle]) -> float:
    """Per-person velocity in [0.1, 1.0]."""
    times: list[datetime] = []
    for a in articles:
        t = a.published_at
        if t is None:
            # No published_at; the article is still evidence of mention now.
            # Skip — using extracted_at would inflate velocity for stale feeds
            # whose publish dates are missing.
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        times.append(t)

    if len(times) < 2:
        return 0.1

    times.sort()
    span_hours = (times[-1] - times[0]).total_seconds() / 3600
    if span_hours <= 0:
        # All publications at the same instant — a burst pattern.
        # 0.5 lands well below the WARNING threshold (0.4) is met,
        # but below ALERT (0.7) — the classifier still needs polarity
        # and reach to escalate.
        return 0.5

    rate_per_hour = len(times) / span_hours
    return min(rate_per_hour / _VELOCITY_SATURATION_PER_HOUR, 1.0)


__all__ = ["compute_rss_reach_per_person"]
