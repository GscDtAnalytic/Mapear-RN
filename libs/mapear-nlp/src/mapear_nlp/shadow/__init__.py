"""Stage 1E v2 — warehouse persistence shadow.

Reusable scorer that the live RSS and social pipelines call to produce
``SilverEventShadow`` rows alongside their gold/silver writes when
``MAPEAR_SHADOW_RULE_VERSION_YAML`` is set. The Stage 1E v1 operator
flow (``mapear-nlp/eval/shadow.py``) stays unchanged for ad-hoc CSV
comparisons.
"""

from mapear_nlp.shadow.scorer import (
    ShadowScorer,
    build_shadow_scorer,
    load_shadow_thresholds,
)

__all__ = ["ShadowScorer", "build_shadow_scorer", "load_shadow_thresholds"]
