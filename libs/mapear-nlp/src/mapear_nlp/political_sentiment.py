"""Political sentiment classifier — FAVORABLE / WARNING / ALERT overlay.

Sits on top of the existing ``SentimentAnalyzer`` (GCP NL polarity) and
the ``TrendScorer`` velocity score. Combines polarity + velocity +
volume + engagement into a three-class label plus an explainability
payload (``decision_factors``) that ships to Gold.

Why an overlay instead of a new model: the goal is actionable political
intelligence, not generic polarity. A post with polarity=-0.6 is only
an ALERT if it's also spreading fast AND reaches many eyeballs —
otherwise it's just a grumpy mention. The overlay encodes that domain
rule in one place so dashboards stay coherent and recalibration
happens by changing thresholds, not retraining.

Versioning: every run stamps ``rule_version`` (hash of active
thresholds) and ``model_version`` (semver of the classifier). Both
are persisted to Gold for reproducibility and A/B analysis.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from typing import Literal

MODEL_VERSION = "political-sentiment-v0.1.0"

SentimentLabel = Literal["FAVORABLE", "WARNING", "ALERT"]


@dataclass(frozen=True)
class ClassificationThresholds:
    """All tunable knobs in one place — change here, bump rule_version.

    Defaults come from the plan (§7.2). Calibration after the first
    production week may move these; the ``rule_version`` hash lets
    downstream analysis reconstruct which regime produced each row.
    """

    polarity_negative: float = -0.35
    polarity_warning: float = -0.10
    polarity_positive: float = 0.20
    volume_spike: int = 7  # RN regional scale: ~8 articles/day/person at peak
    volume_warning: int = 4  # was 8 — calibrated to RN news volume
    velocity_spike: float = 0.45  # was 0.7 — observed velocity ≈ 0.5 at crisis level
    velocity_warning: float = 0.2  # was 0.4
    engagement_high: int = 5000
    # Minimum confidence below which a label is downgraded to WARNING.
    # Prevents FAVORABLE on weak-positive polarity with no supporting signal,
    # and ALERT on marginal evidence that barely crossed all three thresholds.
    min_confidence_favorable: float = 0.30
    min_confidence_alert: float = 0.40

    def rule_version(self) -> str:
        """Stable short hash of the current threshold set — stored per row."""
        payload = ":".join(f"{k}={v}" for k, v in sorted(asdict(self).items()))
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class DecisionFactor:
    """One signal that contributed to the final label."""

    name: str
    value: float
    weight: float
    source: str

    def as_dict(self) -> dict[str, str | float]:
        # Gold persists factors as a list<struct> — keep keys stable.
        return {
            "name": self.name,
            "value": float(self.value),
            "weight": float(self.weight),
            "source": self.source,
        }


@dataclass(frozen=True)
class ClassificationResult:
    label: SentimentLabel
    confidence: float
    risk_score: float
    factors: list[DecisionFactor] = field(default_factory=list)
    rule_version: str = ""
    model_version: str = MODEL_VERSION

    def factors_as_dicts(self) -> list[dict[str, str | float]]:
        return [f.as_dict() for f in self.factors]


class PoliticalSentimentClassifier:
    """Compose polarity, velocity, volume, engagement into a political label.

    Input is numeric — callers wire polarity from ``SentimentAnalyzer``,
    velocity from ``TrendScorer`` and volume/engagement/recurrence from
    Silver aggregates. Keeping inputs numeric (not Pydantic models) lets
    the classifier stay agnostic to the content shape (RSS article vs
    SocialPost).
    """

    def __init__(self, thresholds: ClassificationThresholds | None = None) -> None:
        self._thresholds = thresholds or ClassificationThresholds()

    @property
    def thresholds(self) -> ClassificationThresholds:
        return self._thresholds

    def classify(
        self,
        *,
        polarity: float,
        volume_24h: int,
        velocity: float,
        engagement: int,
        recurrence: float = 0.0,
    ) -> ClassificationResult:
        """Classify a single mention context.

        Args:
            polarity: Signed polarity score in [-1, 1] (GCP NL API).
            volume_24h: Number of mentions of the same ``person_id`` in
                the last 24h — anchors the signal to the target, not the
                post's own virality.
            velocity: TrendScorer score in [0, 1].
            engagement: likes + comments + shares (+views/100 for video).
            recurrence: Share of the last 7 days (0..1) where the target
                had at least one mention; de-noises one-off spikes.
        """
        t = self._thresholds
        factors: list[DecisionFactor] = []

        # -- ALERT: simultaneous negative polarity + high velocity + reach -----
        if (
            polarity <= t.polarity_negative
            and velocity >= t.velocity_spike
            and (volume_24h >= t.volume_spike or engagement >= t.engagement_high)
        ):
            factors.append(
                DecisionFactor(
                    name="polarity_critical",
                    value=polarity,
                    weight=0.40,
                    source="gcp_nl_v2",
                )
            )
            factors.append(
                DecisionFactor(
                    name="velocity_high",
                    value=velocity,
                    weight=0.30,
                    source="trend_scorer",
                )
            )
            reach_value = volume_24h if volume_24h >= t.volume_spike else engagement
            reach_source = (
                "volume_24h" if volume_24h >= t.volume_spike else "engagement"
            )
            factors.append(
                DecisionFactor(
                    name="volume_or_engagement_spike",
                    value=float(reach_value),
                    weight=0.30,
                    source=reach_source,
                )
            )
            if recurrence >= 0.5:
                factors.append(
                    DecisionFactor(
                        name="recurrence_sustained",
                        value=recurrence,
                        weight=0.0,  # informational only; already in ALERT
                        source="stg_social__counts",
                    )
                )
            return self._result("ALERT", factors, polarity, velocity, engagement)

        # -- WARNING: deterioration emerging (2+ signals) ---------------------
        if polarity <= t.polarity_warning and (
            velocity >= t.velocity_warning or volume_24h >= t.volume_warning
        ):
            factors.append(
                DecisionFactor(
                    name="polarity_negative",
                    value=polarity,
                    weight=0.50,
                    source="gcp_nl_v2",
                )
            )
            if velocity >= t.velocity_warning:
                factors.append(
                    DecisionFactor(
                        name="velocity_elevated",
                        value=velocity,
                        weight=0.30,
                        source="trend_scorer",
                    )
                )
            if volume_24h >= t.volume_warning:
                factors.append(
                    DecisionFactor(
                        name="volume_elevated",
                        value=float(volume_24h),
                        weight=0.20,
                        source="volume_24h",
                    )
                )
            return self._result("WARNING", factors, polarity, velocity, engagement)

        # -- FAVORABLE: positive polarity OR neutral with strong engagement ---
        if polarity >= t.polarity_positive or (
            polarity >= t.polarity_warning and engagement >= t.engagement_high
        ):
            factors.append(
                DecisionFactor(
                    name="polarity_positive",
                    value=polarity,
                    weight=0.60,
                    source="gcp_nl_v2",
                )
            )
            if engagement >= t.engagement_high:
                factors.append(
                    DecisionFactor(
                        name="engagement_high",
                        value=float(engagement),
                        weight=0.40,
                        source="engagement",
                    )
                )
            return self._result("FAVORABLE", factors, polarity, velocity, engagement)

        # -- Low-signal default: WARNING; continuous formula gives low score ---
        factors.append(
            DecisionFactor(
                name="insufficient_signal",
                value=polarity,
                weight=1.0,
                source="default",
            )
        )
        return self._result("WARNING", factors, polarity, velocity, engagement)

    # --- internals -------------------------------------------------------

    def _result(
        self,
        label: SentimentLabel,
        factors: list[DecisionFactor],
        polarity: float,
        velocity: float,
        engagement: int,
        *,
        override_confidence: float | None = None,
    ) -> ClassificationResult:
        if override_confidence is not None:
            confidence = override_confidence
        else:
            confidence = self._compute_confidence(
                factors, polarity, velocity, engagement
            )

        # Gate labels by minimum confidence — prevents FAVORABLE/ALERT on
        # marginal evidence (e.g. polarity barely above threshold, no velocity).
        t = self._thresholds
        if (
            label == "FAVORABLE"
            and confidence < t.min_confidence_favorable
            or label == "ALERT"
            and confidence < t.min_confidence_alert
        ):
            label = "WARNING"

        risk = self._compute_risk(polarity, velocity, engagement)
        return ClassificationResult(
            label=label,
            confidence=round(confidence, 4),
            risk_score=round(risk, 4),
            factors=factors,
            rule_version=self._thresholds.rule_version(),
            model_version=MODEL_VERSION,
        )

    @staticmethod
    def _compute_confidence(
        factors: list[DecisionFactor],
        polarity: float = 0.0,
        velocity: float = 0.0,
        engagement: int = 0,
    ) -> float:
        """Confidence in [0, 1] — blends structural evidence with signal magnitudes.

        Two ALERT rows with identical factor structure but different actual
        polarity/velocity values now produce different confidence scores instead
        of collapsing onto the same sigmoid output.

        structural: sigmoid over sum of factor weights (same as before, but now
        just one component).
        signal: weighted combination of |polarity|, velocity, log-scaled engagement.
        Final = 0.55 * structural + 0.45 * signal.
        """
        weight_total = sum(max(f.weight, 0.0) for f in factors)
        if weight_total <= 0:
            return 0.3
        structural = 1.0 / (1.0 + math.exp(-3.0 * (weight_total - 0.9)))
        polarity_mag = min(abs(polarity), 1.0)
        velocity_mag = min(max(velocity, 0.0), 1.0)
        engagement_mag = min(math.log1p(max(engagement, 0)) / math.log1p(50000), 1.0)
        signal = 0.45 * polarity_mag + 0.35 * velocity_mag + 0.20 * engagement_mag
        return min(max(0.55 * structural + 0.45 * signal, 0.0), 1.0)

    @staticmethod
    def _compute_risk(polarity: float, velocity: float, engagement: int) -> float:
        """Normalized 0..1 risk — higher = more costly if ignored.

        0.5 * max(-polarity, 0) + 0.3 * velocity + 0.2 * log-scaled engagement.
        Positive polarity clamps the first term to 0 so "favorable but
        viral" posts don't look risky.
        """
        neg_polarity = max(-polarity, 0.0)
        engagement_norm = min(math.log1p(max(engagement, 0)) / math.log1p(50000), 1.0)
        return min(
            0.5 * neg_polarity + 0.3 * max(velocity, 0.0) + 0.2 * engagement_norm,
            1.0,
        )
