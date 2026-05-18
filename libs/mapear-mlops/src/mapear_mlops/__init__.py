"""mapear-mlops — MLOps utilities for Mapear-RN.

Stage 1D entrypoint. The single public surface today is
``log_eval_run`` — a thin wrapper over MLflow that takes the JSON the
eval harness already emits and logs the right pieces as params /
metrics / tags / artifacts.
"""

from mapear_mlops.tracking import log_eval_run

__all__ = ["log_eval_run"]
