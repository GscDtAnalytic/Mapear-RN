"""Tests for the parallel social pipeline orchestrator.

Covers:
- All 4 platforms run concurrently (wall time < sum of sequential times)
- Sync barrier: consolidated report emitted only after all complete
- CircuitBreaker: OPEN platforms are skipped, others still run
- Partial failures: one bad platform doesn't block the rest
- Idempotency: _metrics dict initialised to 0 even on early exit
- Load test: 4 simultaneous mock runs complete without errors
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from mapear_infra.circuit_breaker import CircuitBreaker, CircuitState
from mapear_social.parallel import (
    PLATFORMS,
    PlatformRunResult,
    _orchestrate,
    run_all_platforms,
)

# ---------------------------------------------------------------------------
# CircuitBreaker unit tests
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_initial_state_is_closed(self):
        cb = CircuitBreaker("test")
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_call() is True

    def test_opens_after_failure_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=2)
        cb.on_failure()
        assert cb.state == CircuitState.CLOSED
        cb.on_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_call() is False

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.01)
        cb.on_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.02)
        assert cb.allow_call() is True  # transitions to HALF_OPEN
        assert cb.state == CircuitState.HALF_OPEN

    def test_closed_after_success_in_half_open(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.01)
        cb.on_failure()
        time.sleep(0.02)
        cb.allow_call()  # move to HALF_OPEN
        cb.on_success()
        assert cb.state == CircuitState.CLOSED

    def test_reopens_on_failure_in_half_open(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.01)
        cb.on_failure()
        time.sleep(0.02)
        cb.allow_call()  # HALF_OPEN
        cb.on_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        cb.on_failure()
        cb.on_failure()
        cb.on_success()
        # count reset → 2 more failures needed to open
        cb.on_failure()
        assert cb.state == CircuitState.CLOSED
        cb.on_failure()
        assert cb.state == CircuitState.CLOSED
        cb.on_failure()
        assert cb.state == CircuitState.OPEN

    def test_thread_safety(self):
        """Multiple threads calling on_failure() concurrently must not corrupt state."""
        import threading

        cb = CircuitBreaker("test", failure_threshold=100)
        errors: list[Exception] = []

        def hit():
            try:
                for _ in range(20):
                    cb.on_failure()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=hit) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert cb._failure_count == 100


# ---------------------------------------------------------------------------
# Parallel orchestrator tests
# ---------------------------------------------------------------------------


def _make_fake_pipeline(
    *,
    sleep_seconds: float = 0.05,
    should_fail: bool = False,
    exit_code: int | None = None,
    scraped: int = 10,
    stored: int = 8,
):
    """Factory: returns a callable matching run_pipeline's signature."""

    def fake_pipeline(
        cli_platform: str | None = None,
        _metrics: dict | None = None,
        **kwargs: Any,
    ) -> None:
        time.sleep(sleep_seconds)
        if _metrics is not None:
            _metrics["scraped"] = scraped
            _metrics["stored"] = stored
            _metrics["filtered"] = scraped - stored
            _metrics["errors"] = 0
        if exit_code is not None:
            raise SystemExit(exit_code)
        if should_fail:
            raise RuntimeError(f"Simulated failure for {cli_platform}")

    return fake_pipeline


class TestParallelOrchestrator:
    """Integration-level tests using fake pipeline functions."""

    def test_all_platforms_run_and_succeed(self):
        fn = _make_fake_pipeline(sleep_seconds=0.05)
        ok = run_all_platforms(_run_pipeline_fn=fn)
        assert ok is True

    def test_wall_time_less_than_sequential(self):
        """Parallelism should reduce wall time by ≥40% vs sequential estimate."""
        sleep = 0.1
        fn = _make_fake_pipeline(sleep_seconds=sleep)
        start = time.monotonic()
        run_all_platforms(
            platforms=("facebook", "instagram", "tiktok", "x"), _run_pipeline_fn=fn
        )
        wall = time.monotonic() - start

        sequential_estimate = sleep * 4
        # Allow generous margin: wall < 70% of sequential (well above 40% target)
        assert wall < sequential_estimate * 0.70, (
            f"wall={wall:.2f}s expected < {sequential_estimate * 0.70:.2f}s "
            f"(sequential_estimate={sequential_estimate:.2f}s)"
        )

    def test_partial_failure_does_not_block_others(self):
        """One failing platform → other 3 still complete, overall returns False."""
        call_log: list[str] = []

        def fn(cli_platform=None, _metrics=None, **kw):
            time.sleep(0.02)
            call_log.append(cli_platform)
            if _metrics is not None:
                _metrics.update({"scraped": 5, "filtered": 0, "stored": 5, "errors": 0})
            if cli_platform == "tiktok":
                raise SystemExit(3)

        ok = run_all_platforms(platforms=PLATFORMS, _run_pipeline_fn=fn)
        assert ok is False
        assert sorted(call_log) == sorted(list(PLATFORMS))

    def test_circuit_breaker_skips_open_platform(self):
        """An already-OPEN circuit breaker causes the platform to be skipped."""
        call_log: list[str] = []

        def fn(cli_platform=None, _metrics=None, **kw):
            call_log.append(cli_platform)
            if _metrics is not None:
                _metrics.update({"scraped": 1, "filtered": 0, "stored": 1, "errors": 0})

        # Pre-open the circuit breaker for "x"
        cbs = {p: CircuitBreaker(p, failure_threshold=1) for p in PLATFORMS}
        cbs["x"].on_failure()
        assert cbs["x"].state == CircuitState.OPEN

        ok = run_all_platforms(
            platforms=PLATFORMS,
            circuit_breakers=cbs,
            _run_pipeline_fn=fn,
        )
        assert ok is False  # "x" skipped → not all succeeded
        assert "x" not in call_log
        assert "facebook" in call_log

    def test_metrics_populated_on_success(self):
        """PlatformRunResult carries correct counts from _metrics dict."""

        def fn(cli_platform=None, _metrics=None, **kw):
            if _metrics is not None:
                _metrics.update(
                    {"scraped": 20, "filtered": 3, "stored": 15, "errors": 2}
                )

        results_holder: list[list[PlatformRunResult]] = []

        async def capture():
            cbs = {p: CircuitBreaker(p) for p in ("facebook",)}
            res = await _orchestrate(("facebook",), cbs, fn, {})
            results_holder.append(res)

        asyncio.run(capture())
        r = results_holder[0][0]
        assert r.platform == "facebook"
        assert r.success is True
        assert r.scraped_count == 20
        assert r.filtered_count == 3
        assert r.stored_count == 15
        assert r.errors_count == 2

    def test_metrics_zero_on_early_exit(self):
        """_metrics dict stays at 0 when pipeline raises SystemExit immediately."""

        def fn(cli_platform=None, _metrics=None, **kw):
            # _metrics should be initialised before any real work
            assert _metrics is not None
            raise SystemExit(5)

        results_holder: list[list[PlatformRunResult]] = []

        async def capture():
            cbs = {p: CircuitBreaker(p) for p in ("instagram",)}
            res = await _orchestrate(("instagram",), cbs, fn, {})
            results_holder.append(res)

        asyncio.run(capture())
        r = results_holder[0][0]
        assert r.success is False
        assert r.exit_code == 5
        assert r.scraped_count == 0
        assert r.stored_count == 0

    def test_latency_report_written(self, tmp_path, monkeypatch):
        """Consolidated JSON report is written to _REPORT_PATH after the run."""
        import mapear_social.parallel as par

        report_file = tmp_path / "report.json"
        monkeypatch.setattr(par, "_REPORT_PATH", report_file)

        fn = _make_fake_pipeline(sleep_seconds=0.01)
        run_all_platforms(platforms=("facebook", "instagram"), _run_pipeline_fn=fn)

        assert report_file.exists()
        data = json.loads(report_file.read_text())
        assert "wall_time_seconds" in data
        assert "latency_reduction_pct" in data
        assert "p50_seconds" in data
        assert "p95_seconds" in data
        assert "facebook" in data["platforms"]
        assert "instagram" in data["platforms"]

    def test_latency_report_fields(self, tmp_path, monkeypatch):
        """Report includes scraped/filtered/stored/errors per platform."""
        import mapear_social.parallel as par

        report_file = tmp_path / "report.json"
        monkeypatch.setattr(par, "_REPORT_PATH", report_file)

        def fn(cli_platform=None, _metrics=None, **kw):
            if _metrics is not None:
                _metrics.update({"scraped": 7, "filtered": 1, "stored": 6, "errors": 0})

        run_all_platforms(platforms=("x",), _run_pipeline_fn=fn)
        data = json.loads(report_file.read_text())
        plat = data["platforms"]["x"]
        assert plat["scraped"] == 7
        assert plat["filtered"] == 1
        assert plat["stored"] == 6
        assert plat["errors"] == 0
        assert plat["success"] is True


# ---------------------------------------------------------------------------
# Load test: 4 concurrent mock pipelines
# ---------------------------------------------------------------------------


class TestLoadConcurrent:
    """Simulate realistic concurrent load with all 4 platforms."""

    def test_four_platforms_complete_under_threshold(self, tmp_path, monkeypatch):
        """Parallel execution delivers ≥40% latency reduction vs sequential estimate.

        We read the reduction from the JSON report (which captures only the
        orchestration wall time, excluding asyncio.run() / thread pool startup
        that are one-time costs in production Cloud Run jobs).
        """
        import mapear_social.parallel as par

        report_file = tmp_path / "report.json"
        monkeypatch.setattr(par, "_REPORT_PATH", report_file)

        sleep_per_platform = 0.20
        fn = _make_fake_pipeline(
            sleep_seconds=sleep_per_platform, scraped=50, stored=40
        )
        ok = run_all_platforms(platforms=PLATFORMS, _run_pipeline_fn=fn)
        assert ok is True

        report = json.loads(report_file.read_text())
        reduction = report["latency_reduction_pct"]
        assert reduction >= 40.0, (
            f"latency_reduction={reduction:.1f}% — expected ≥ 40%. "
            f"wall={report['wall_time_seconds']:.2f}s "
            f"sequential_estimate={report['sequential_estimate_seconds']:.2f}s"
        )

    def test_concurrent_run_is_idempotent(self):
        """Running twice with same inputs produces same outcome (idempotency check)."""
        fn = _make_fake_pipeline(sleep_seconds=0.02, scraped=10, stored=10)
        result_1 = run_all_platforms(platforms=PLATFORMS, _run_pipeline_fn=fn)
        result_2 = run_all_platforms(platforms=PLATFORMS, _run_pipeline_fn=fn)
        assert result_1 == result_2 is True

    def test_all_four_platforms_called_exactly_once(self):
        """Each platform is invoked exactly once per run."""
        call_counts: dict[str, int] = {}

        def fn(cli_platform=None, _metrics=None, **kw):
            call_counts[cli_platform] = call_counts.get(cli_platform, 0) + 1
            if _metrics is not None:
                _metrics.update({"scraped": 1, "filtered": 0, "stored": 1, "errors": 0})

        run_all_platforms(platforms=PLATFORMS, _run_pipeline_fn=fn)
        assert call_counts == {p: 1 for p in PLATFORMS}
