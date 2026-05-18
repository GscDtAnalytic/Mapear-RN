"""MLflow tracking wrapper for the political-sentiment eval harness.

The harness in ``mapear-nlp/eval/run.py`` already produces a self-
contained dict with everything an MLflow run needs:

* per-threshold params (we read them off the live classifier instead
  of from the metrics dict to avoid drift),
* metrics — F1 macro, accuracy, per-class precision / recall / F1,
* tags — ``rule_version``, ``model_version``, ``n_cases``, ``n_xfail``,
* artifacts — the full metrics JSON, and optionally the gold-set CSV
  that produced the run.

The wrapper is deliberately small. MLflow's call surface is stable and
documented; this module exists to (a) pick a sensible default
tracking URI, (b) decide *which* fields land where, and (c) absorb
MLflow exceptions so logging failure never blocks the eval gate.

Resolution of the tracking URI
------------------------------

1. If ``tracking_uri`` is passed explicitly to :func:`log_eval_run`,
   use it.
2. Else, if the ``MLFLOW_TRACKING_URI`` env var is set, use it.
3. Else, default to ``file://<repo-root>/mlruns`` — i.e. a directory
   at the monorepo root so every package logs to the same store and
   ``mlflow ui`` (run from the repo root) finds everything.

The repo root is detected by walking up from this file until a marker
(``CLAUDE.md`` or ``Makefile`` at root) is found; if not found we
fall back to the current working directory's ``./mlruns``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from loguru import logger

_EXPERIMENT_DEFAULT = "mapear-political-sentiment"

# Tag keys we promote from the metrics dict. Anything outside this set
# is logged as a regular metric (numeric) or skipped (non-scalar).
_TAG_KEYS: tuple[str, ...] = (
    "rule_version",
    "model_version",
    "n_cases",
    "n_xfail",
    "n_xpass_unexpected",
)


def _detect_repo_root() -> Path | None:
    """Walk up from this file looking for the monorepo root marker."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "CLAUDE.md").exists() and (parent / "Makefile").exists():
            return parent
    return None


def _resolve_tracking_uri(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("MLFLOW_TRACKING_URI")
    if env:
        return env
    root = _detect_repo_root()
    base = root if root is not None else Path.cwd()
    return f"file://{(base / 'mlruns').resolve()}"


def log_eval_run(
    metrics: dict[str, Any],
    *,
    experiment: str = _EXPERIMENT_DEFAULT,
    run_name: str | None = None,
    tracking_uri: str | None = None,
    extra_artifacts: list[Path] | None = None,
    extra_params: dict[str, Any] | None = None,
    extra_artifact_dicts: dict[str, Any] | None = None,
) -> str | None:
    """Log one eval-harness run to MLflow.

    Args:
        metrics: The dict returned by ``mapear-nlp/eval/run.py::evaluate``
            or ``eval/shadow.py``. Every scalar under ``metrics["metrics"]``
            is logged; the nested ``per_class`` block is flattened with
            ``{label}_{stat}`` keys.
        experiment: MLflow experiment name. One per "model" — defaults
            to ``mapear-political-sentiment``.
        run_name: Optional human-readable run name; MLflow generates one
            if omitted.
        tracking_uri: Override tracking URI. See module docstring.
        extra_artifacts: Files to attach as artifacts (e.g. the gold
            CSV). Each must exist or it is skipped with a warning.
        extra_params: Extra MLflow params to log alongside the
            classifier thresholds — typically the classifier's
            threshold values themselves (caller has the live object).
        extra_artifact_dicts: Mapping of ``{filename: dict}`` to be
            persisted as JSON artifacts inside this run. Use for
            comparison reports and other in-memory payloads that have
            no filesystem path.

    Returns:
        The MLflow run_id on success, ``None`` if MLflow failed.
    """
    try:
        import mlflow  # noqa: PLC0415 — local import keeps mlflow opt-in
    except ImportError:
        logger.warning(
            "mlflow not installed; skipping MLflow logging. "
            "Install via `poetry -C mapear-mlops install`."
        )
        return None

    uri = _resolve_tracking_uri(tracking_uri)
    try:
        mlflow.set_tracking_uri(uri)
        mlflow.set_experiment(experiment)
        with mlflow.start_run(run_name=run_name) as run:
            _log_tags(mlflow, metrics)
            _log_params(mlflow, extra_params or {})
            _log_metrics(mlflow, metrics)
            _log_artifacts(mlflow, metrics, extra_artifacts or [])
            for name, payload in (extra_artifact_dicts or {}).items():
                mlflow.log_dict(payload, name)
            logger.info(
                "Logged eval run to MLflow: experiment={exp} run_id={rid} uri={uri}",
                exp=experiment,
                rid=run.info.run_id,
                uri=uri,
            )
            return run.info.run_id
    except Exception as exc:  # noqa: BLE001 — observability layer
        logger.warning("MLflow logging failed ({exc}); continuing without it.", exc=exc)
        return None


def _log_tags(mlflow_mod: Any, metrics: dict[str, Any]) -> None:
    for key in _TAG_KEYS:
        if key in metrics and metrics[key] is not None:
            mlflow_mod.set_tag(key, str(metrics[key]))


def _log_params(mlflow_mod: Any, extra_params: dict[str, Any]) -> None:
    for key, value in extra_params.items():
        mlflow_mod.log_param(key, value)


def _log_metrics(mlflow_mod: Any, metrics: dict[str, Any]) -> None:
    """Log everything numeric under ``metrics["metrics"]``.

    Scalars at the top level become metrics with the same name. The
    nested ``per_class`` block (label → {stat → value}) is flattened to
    ``{label}_{stat}``. Anything else is silently skipped.
    """
    m = metrics.get("metrics") or {}
    for key, value in m.items():
        if key == "per_class":
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            mlflow_mod.log_metric(key, float(value))
    for label, vals in (m.get("per_class") or {}).items():
        if not isinstance(vals, dict):
            continue
        for stat, value in vals.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                mlflow_mod.log_metric(f"{label}_{stat}", float(value))


def _log_artifacts(mlflow_mod: Any, metrics: dict[str, Any], extra: list[Path]) -> None:
    # Persist the full metrics dict as an artifact so the audit trail
    # is self-contained — including misclassified case ids and the
    # confusion matrix that we did not promote to top-level metrics.
    mlflow_mod.log_dict(metrics, "metrics.json")
    for path in extra:
        p = Path(path)
        if p.exists():
            mlflow_mod.log_artifact(str(p))
        else:
            logger.warning("Artifact path not found, skipping: {p}", p=p)
