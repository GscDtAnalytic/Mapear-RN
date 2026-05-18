"""Parallel orchestration of all social platform pipelines.

Launches FB, IG, TT, X concurrently via asyncio.gather() + ThreadPoolExecutor.
A synchronization barrier (the gather call itself) collects all results before
consolidated metrics and the latency JSON report are emitted.

Each platform is independently protected by a CircuitBreaker — a single-platform
failure never blocks the others.

Usage::

    python -m mapear_social --platform=all
    ENVIRONMENT=local python -m mapear_social --platform=all --mode=backfill
"""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from mapear_infra.circuit_breaker import CircuitBreaker, CircuitState
from mapear_infra.metrics import (
    circuit_breaker_state,
    social_pipeline_latency,
)

PLATFORMS: tuple[str, ...] = ("facebook", "instagram", "tiktok", "x")

_REPORT_PATH = Path("/tmp/mapear_social_latency_report.json")


@dataclass
class PlatformRunResult:
    platform: str
    success: bool
    latency_seconds: float
    scraped_count: int = 0
    filtered_count: int = 0
    stored_count: int = 0
    errors_count: int = 0
    exit_code: int = 0
    error_message: str = ""
    cb_state: str = CircuitState.CLOSED.value


def _set_cb_metric(platform: str, state: CircuitState) -> None:
    circuit_breaker_state.labels(domain=platform).set(
        1 if state == CircuitState.OPEN else 0
    )


async def _run_platform_in_thread(
    executor: ThreadPoolExecutor,
    platform: str,
    cb: CircuitBreaker,
    run_pipeline_fn: Any,
    pipeline_kwargs: dict,
) -> PlatformRunResult:
    """Execute one platform pipeline in a thread, guarded by circuit breaker."""
    if not cb.allow_call():
        _set_cb_metric(platform, CircuitState.OPEN)
        logger.warning(
            "Circuit breaker OPEN for {platform} — skipping this run",
            platform=platform,
        )
        return PlatformRunResult(
            platform=platform,
            success=False,
            latency_seconds=0.0,
            error_message="circuit_breaker_open",
            cb_state=CircuitState.OPEN.value,
        )

    loop = asyncio.get_event_loop()
    start = time.monotonic()
    metrics: dict[str, int] = {"scraped": 0, "filtered": 0, "stored": 0, "errors": 0}

    def _run() -> None:
        run_pipeline_fn(cli_platform=platform, _metrics=metrics, **pipeline_kwargs)

    try:
        await loop.run_in_executor(executor, _run)
        latency = time.monotonic() - start
        cb.on_success()
        _set_cb_metric(platform, CircuitState.CLOSED)
        social_pipeline_latency.labels(platform=platform).observe(latency)
        logger.info(
            "platform={platform} success latency={latency:.1f}s "
            "scraped={scraped} filtered={filtered} stored={stored} errors={errors}",
            platform=platform,
            latency=latency,
            **metrics,
        )
        return PlatformRunResult(
            platform=platform,
            success=True,
            latency_seconds=latency,
            scraped_count=metrics["scraped"],
            filtered_count=metrics["filtered"],
            stored_count=metrics["stored"],
            errors_count=metrics["errors"],
            cb_state=cb.state.value,
        )
    except SystemExit as exc:
        latency = time.monotonic() - start
        cb.on_failure()
        _set_cb_metric(platform, cb.state)
        logger.error(
            "platform={platform} SystemExit({code}) after {latency:.1f}s",
            platform=platform,
            code=exc.code,
            latency=latency,
        )
        return PlatformRunResult(
            platform=platform,
            success=False,
            latency_seconds=latency,
            scraped_count=metrics["scraped"],
            errors_count=metrics["errors"],
            exit_code=exc.code or 1,
            error_message=f"SystemExit({exc.code})",
            cb_state=cb.state.value,
        )
    except Exception as exc:
        latency = time.monotonic() - start
        cb.on_failure()
        _set_cb_metric(platform, cb.state)
        logger.exception(
            "platform={platform} crashed after {latency:.1f}s: {err}",
            platform=platform,
            latency=latency,
            err=str(exc),
        )
        return PlatformRunResult(
            platform=platform,
            success=False,
            latency_seconds=latency,
            scraped_count=metrics["scraped"],
            errors_count=metrics["errors"],
            error_message=str(exc),
            cb_state=cb.state.value,
        )


async def _orchestrate(
    platforms: tuple[str, ...],
    circuit_breakers: dict[str, CircuitBreaker],
    run_pipeline_fn: Any,
    pipeline_kwargs: dict,
) -> list[PlatformRunResult]:
    """Core coroutine: fan-out → barrier → consolidated report."""
    executor = ThreadPoolExecutor(
        max_workers=len(platforms),
        thread_name_prefix="mapear-social",
    )
    wall_start = time.monotonic()

    tasks = [
        _run_platform_in_thread(
            executor=executor,
            platform=p,
            cb=circuit_breakers[p],
            run_pipeline_fn=run_pipeline_fn,
            pipeline_kwargs=pipeline_kwargs,
        )
        for p in platforms
    ]

    # ── SYNCHRONIZATION BARRIER ────────────────────────────────────────────────
    results: list[PlatformRunResult] = await asyncio.gather(*tasks)
    # ──────────────────────────────────────────────────────────────────────────

    wall_time = time.monotonic() - wall_start
    executor.shutdown(wait=False)

    _emit_report(results, wall_time)
    return results


def _emit_report(results: list[PlatformRunResult], wall_time: float) -> None:
    """Write JSON latency report and structured summary log."""
    succeeded = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    latencies = [r.latency_seconds for r in results]
    sequential_estimate = sum(latencies)

    sorted_lats = sorted(latencies)
    n = len(sorted_lats)
    p50 = sorted_lats[max(0, int(n * 0.5) - 1)] if n else 0.0
    p95 = sorted_lats[min(int(n * 0.95), n - 1)] if n else 0.0
    latency_reduction_pct = (
        (sequential_estimate - wall_time) / sequential_estimate * 100
        if sequential_estimate > 0
        else 0.0
    )

    report: dict[str, Any] = {
        "run_at": datetime.now(UTC).isoformat(),
        "wall_time_seconds": round(wall_time, 3),
        "sequential_estimate_seconds": round(sequential_estimate, 3),
        "latency_reduction_pct": round(latency_reduction_pct, 1),
        "p50_seconds": round(p50, 3),
        "p95_seconds": round(p95, 3),
        "platforms": {
            r.platform: {
                "success": r.success,
                "latency_seconds": round(r.latency_seconds, 3),
                "scraped": r.scraped_count,
                "filtered": r.filtered_count,
                "stored": r.stored_count,
                "errors": r.errors_count,
                "cb_state": r.cb_state,
                "error_message": r.error_message or None,
            }
            for r in results
        },
    }

    try:
        _REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        logger.info("Latency report written to {path}", path=_REPORT_PATH)
    except OSError as exc:
        logger.warning("Could not write latency report: {err}", err=exc)

    logger.info(
        "parallel_run_complete"
        " wall={wall:.1f}s sequential_estimate={seq:.1f}s"
        " latency_reduction={reduction:.1f}%"
        " p50={p50:.1f}s p95={p95:.1f}s"
        " ok={ok}/{total}",
        wall=wall_time,
        seq=sequential_estimate,
        reduction=latency_reduction_pct,
        p50=p50,
        p95=p95,
        ok=len(succeeded),
        total=len(results),
    )
    if failed:
        logger.warning(
            "Failed platforms: {platforms}",
            platforms={r.platform: r.error_message for r in failed},
        )


def run_all_platforms(
    platforms: tuple[str, ...] = PLATFORMS,
    circuit_breakers: dict[str, CircuitBreaker] | None = None,
    _run_pipeline_fn: Any = None,
    **pipeline_kwargs: Any,
) -> bool:
    """Run all platform pipelines in parallel.

    Returns True only when every platform succeeded.

    ``_run_pipeline_fn`` is injected in tests to replace the real pipeline.
    ``circuit_breakers`` can be pre-built to carry state across retry attempts
    within the same process lifetime.
    """
    from mapear_social.pipeline import run_pipeline as _default

    cbs = circuit_breakers or {p: CircuitBreaker(p) for p in platforms}
    fn = _run_pipeline_fn or _default

    results = asyncio.run(_orchestrate(platforms, cbs, fn, pipeline_kwargs))
    return all(r.success for r in results)
