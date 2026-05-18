"""Unit tests for mapear_mlops.tracking.log_eval_run.

Uses a tmp_path-based file store so tests are hermetic and parallel-safe.
"""

from __future__ import annotations

from pathlib import Path

import mlflow
import pytest

from mapear_mlops.tracking import log_eval_run

# --- Helpers ---------------------------------------------------------------


def _sample_metrics(rule_version: str = "abc123", f1: float = 0.85) -> dict:
    return {
        "rule_version": rule_version,
        "model_version": "political-sentiment-v0.1.0",
        "n_cases": 50,
        "n_xfail": 0,
        "n_xpass_unexpected": 0,
        "confusion": {
            "FAVORABLE": {"FAVORABLE": 10},
            "WARNING": {"WARNING": 20},
            "ALERT": {"ALERT": 20},
        },
        "metrics": {
            "f1_macro": f1,
            "accuracy": f1,
            "per_class": {
                "FAVORABLE": {
                    "precision": 1.0,
                    "recall": 1.0,
                    "f1": 1.0,
                    "support": 10,
                },
                "WARNING": {"precision": 1.0, "recall": 1.0, "f1": 1.0, "support": 20},
                "ALERT": {"precision": 1.0, "recall": 1.0, "f1": 1.0, "support": 20},
            },
        },
        "misclassified": [],
        "predictions": {"A001": {"predicted": "ALERT", "expected": "ALERT"}},
    }


def _tracking_uri(tmp_path: Path) -> str:
    return f"file://{(tmp_path / 'mlruns').resolve()}"


# --- Tests ----------------------------------------------------------------


def test_log_run_writes_tags_metrics_and_artifact(tmp_path: Path) -> None:
    metrics = _sample_metrics()
    run_id = log_eval_run(metrics, tracking_uri=_tracking_uri(tmp_path))
    assert run_id is not None

    mlflow.set_tracking_uri(_tracking_uri(tmp_path))
    run = mlflow.get_run(run_id)

    assert run.data.tags["rule_version"] == "abc123"
    assert run.data.tags["model_version"] == "political-sentiment-v0.1.0"
    assert run.data.tags["n_cases"] == "50"

    assert run.data.metrics["f1_macro"] == pytest.approx(0.85)
    assert run.data.metrics["accuracy"] == pytest.approx(0.85)
    assert run.data.metrics["ALERT_f1"] == pytest.approx(1.0)
    assert run.data.metrics["WARNING_support"] == pytest.approx(20)

    artifact_uri = run.info.artifact_uri.replace("file://", "")
    assert (Path(artifact_uri) / "metrics.json").exists()


def test_extra_artifact_is_logged(tmp_path: Path) -> None:
    extra = tmp_path / "gold_set.csv"
    extra.write_text("case_id,polarity\nA001,-0.6\n")

    run_id = log_eval_run(
        _sample_metrics(),
        tracking_uri=_tracking_uri(tmp_path),
        extra_artifacts=[extra],
    )
    assert run_id is not None

    mlflow.set_tracking_uri(_tracking_uri(tmp_path))
    run = mlflow.get_run(run_id)
    artifact_root = Path(run.info.artifact_uri.replace("file://", ""))
    assert (artifact_root / "gold_set.csv").exists()


def test_extra_params_are_logged(tmp_path: Path) -> None:
    extra_params = {
        "polarity_negative": -0.35,
        "velocity_spike": 0.7,
        "volume_spike": 15,
    }
    run_id = log_eval_run(
        _sample_metrics(),
        tracking_uri=_tracking_uri(tmp_path),
        extra_params=extra_params,
    )
    assert run_id is not None

    mlflow.set_tracking_uri(_tracking_uri(tmp_path))
    run = mlflow.get_run(run_id)
    # MLflow stringifies all params on log; values come back as str.
    assert run.data.params["polarity_negative"] == "-0.35"
    assert run.data.params["volume_spike"] == "15"


def test_two_runs_accumulate_under_same_experiment(tmp_path: Path) -> None:
    uri = _tracking_uri(tmp_path)
    rid1 = log_eval_run(_sample_metrics("v1", 0.80), tracking_uri=uri)
    rid2 = log_eval_run(_sample_metrics("v2", 0.95), tracking_uri=uri)
    assert rid1 and rid2 and rid1 != rid2

    mlflow.set_tracking_uri(uri)
    exp = mlflow.get_experiment_by_name("mapear-political-sentiment")
    # output_format="list" avoids pandas, which mlflow-skinny omits.
    runs = mlflow.search_runs(experiment_ids=[exp.experiment_id], output_format="list")
    assert len(runs) == 2
    assert {r.data.tags["rule_version"] for r in runs} == {"v1", "v2"}


def test_missing_optional_keys_do_not_crash(tmp_path: Path) -> None:
    """Robustness: harness may omit per_class or n_xfail in some envs."""
    metrics = {
        "rule_version": "minimal",
        "model_version": "political-sentiment-v0.1.0",
        "metrics": {"f1_macro": 0.5},  # no accuracy, no per_class
    }
    run_id = log_eval_run(metrics, tracking_uri=_tracking_uri(tmp_path))
    assert run_id is not None

    mlflow.set_tracking_uri(_tracking_uri(tmp_path))
    run = mlflow.get_run(run_id)
    assert run.data.metrics["f1_macro"] == pytest.approx(0.5)
    assert "accuracy" not in run.data.metrics


def test_env_var_overrides_default_when_no_explicit_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = _tracking_uri(tmp_path)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    run_id = log_eval_run(_sample_metrics())
    assert run_id is not None

    mlflow.set_tracking_uri(uri)
    run = mlflow.get_run(run_id)
    assert run.data.tags["rule_version"] == "abc123"


def test_explicit_uri_takes_precedence_over_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "env"
    explicit_dir = tmp_path / "explicit"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"file://{env_dir.resolve()}")

    explicit_uri = f"file://{explicit_dir.resolve()}"
    rid = log_eval_run(_sample_metrics(), tracking_uri=explicit_uri)
    assert rid is not None

    # The explicit dir got the run; the env-var dir is empty.
    assert any(explicit_dir.rglob("metrics.json"))
    assert not any(env_dir.rglob("metrics.json")) if env_dir.exists() else True
