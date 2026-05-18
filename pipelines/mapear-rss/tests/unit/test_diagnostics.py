"""Tests for the diagnostics collector and run report builder."""

from mapear_rss.extraction.block_detector import BlockSignals
from mapear_rss.extraction.diagnostics import (
    RUN_REPORT_VERSION,
    DiagnosticCollector,
    DiagnosticRecord,
    FetchCounters,
    filter_headers,
    hash_body_sample,
)


def _rec(**overrides) -> DiagnosticRecord:
    base = dict(
        url="https://example.com/a",
        domain="example.com",
        attempt=1,
        stage="fetch",
        status_code=200,
        final_url="https://example.com/a",
        latency_ms=100,
        content_length_bytes=1234,
        body_sample_hash="deadbeef",
        headers={"server": "nginx"},
        block_signals=BlockSignals().to_dict(),
        extractor_used="primary",
        extracted_chars=500,
        extraction_success=True,
        error_type=None,
    )
    base.update(overrides)
    return DiagnosticRecord(**base)


class TestFilterHeaders:
    def test_allowlist_enforced(self) -> None:
        out = filter_headers(
            {
                "Server": "nginx",
                "Set-Cookie": "secret=abc",
                "Authorization": "Bearer xyz",
                "CF-Ray": "abc",
            }
        )
        assert "server" in out
        assert "cf-ray" in out
        assert "set-cookie" not in out
        assert "authorization" not in out


class TestBodyHash:
    def test_none_returns_none(self) -> None:
        assert hash_body_sample(None) is None
        assert hash_body_sample("") is None

    def test_deterministic(self) -> None:
        assert hash_body_sample("abc") == hash_body_sample("abc")


class TestRunReport:
    def test_empty_report_has_zero_rate(self) -> None:
        c = DiagnosticCollector()
        r = c.build_report(
            discovered=0,
            fetched=0,
            extracted=0,
            unique=0,
            rn_relevant=0,
            cooldown_skips=0,
        )
        assert r["extraction_success_rate"] == 0.0
        assert r["per_domain"] == {}

    def test_per_domain_rates(self) -> None:
        c = DiagnosticCollector()
        c.record(_rec(extraction_success=True))
        c.record(
            _rec(
                extraction_success=False,
                extractor_used=None,
                extracted_chars=0,
                error_type="selector_missing",
            )
        )
        blocked_signals = BlockSignals(cloudflare_detected=True).to_dict()
        c.record(
            _rec(
                url="https://cf.com/x",
                domain="cf.com",
                extraction_success=False,
                extractor_used=None,
                extracted_chars=0,
                error_type="blocked_bot",
                block_signals=blocked_signals,
            )
        )

        r = c.build_report(
            discovered=10,
            fetched=3,
            extracted=1,
            unique=1,
            rn_relevant=0,
            cooldown_skips=0,
        )
        assert r["extraction_success_rate"] == round(1 / 3, 3)
        assert "cf.com" in r["blocked_domains"]
        per_domain = r["per_domain"]
        assert per_domain["example.com"]["parser_failure"] == 1
        assert per_domain["cf.com"]["blocked"] == 1
        assert r["top_failed_domains"]  # non-empty

    def test_fallback_count_reported(self) -> None:
        c = DiagnosticCollector()
        c.note_fallback_save()
        c.note_fallback_save()
        c.note_retry()
        r = c.build_report(
            discovered=0,
            fetched=0,
            extracted=0,
            unique=0,
            rn_relevant=0,
            cooldown_skips=0,
        )
        assert r["fallback_save_count"] == 2
        assert r["retries_total"] == 1

    def test_debug_domain_flag(self) -> None:
        c = DiagnosticCollector(debug_domains=frozenset({"example.com"}))
        assert c.should_emit_debug("https://example.com/a")
        assert not c.should_emit_debug("https://other.com/a")


class TestRunReportV2:
    def test_counters_split_main_vs_retry(self) -> None:
        c = DiagnosticCollector()
        counters = FetchCounters(
            fetched_main=9,
            fetched_retry=4,
            fetched_unique_urls=13,
            extracted_main=9,
            extracted_retry=4,
        )
        r = c.build_report(
            discovered=422,
            counters=counters,
            unique=12,
            rn_relevant=7,
            cooldown_skips=3,
            cooldown_applied_count=1,
            cooldown_reason_distribution={"bot_block": 1},
        )
        assert r["report_version"] == RUN_REPORT_VERSION
        assert r["fetched_main"] == 9
        assert r["fetched_retry"] == 4
        assert r["extracted_main"] == 9
        assert r["extracted_retry"] == 4
        assert r["fetched"] == 13
        assert r["extracted"] == 13
        assert r["extraction_success_rate"] == 1.0
        assert r["integrity_warning"] is False
        assert r["cooldown_applied_count"] == 1
        assert r["cooldown_reason_distribution"] == {"bot_block": 1}

    def test_rate_clamped_when_extracted_exceeds_fetched(self) -> None:
        """The 04-13 prod bug: fetched=9, extracted=13 must clamp to 1.0."""
        c = DiagnosticCollector()
        counters = FetchCounters(
            fetched_main=9,
            fetched_retry=0,
            fetched_unique_urls=9,
            extracted_main=13,
            extracted_retry=0,
        )
        r = c.build_report(
            discovered=0,
            counters=counters,
            unique=0,
            rn_relevant=0,
            cooldown_skips=0,
        )
        assert r["extraction_success_rate"] == 1.0
        assert r["integrity_warning"] is True
        # Aggregated `extracted` must not exceed `fetched` in the clamped view.
        assert r["extracted"] <= r["fetched"]

    def test_legacy_kwargs_still_work(self) -> None:
        c = DiagnosticCollector()
        r = c.build_report(
            discovered=5,
            fetched=4,
            extracted=2,
            unique=2,
            rn_relevant=1,
            cooldown_skips=0,
        )
        assert r["fetched_main"] == 4
        assert r["fetched_retry"] == 0
        assert r["extraction_success_rate"] == 0.5
        assert r["integrity_warning"] is False

    def test_parser_recovery_per_domain(self) -> None:
        c = DiagnosticCollector()
        c.record(_rec(domain="agorarn.com.br", extraction_success=True))
        c.note_parser_recovery("agorarn.com.br", "jsonld")
        c.note_parser_recovery("agorarn.com.br", "jsonld")
        c.note_parser_recovery("agorarn.com.br", "readability")
        r = c.build_report(
            discovered=1,
            counters=FetchCounters(
                fetched_main=1, extracted_main=1, fetched_unique_urls=1
            ),
            unique=1,
            rn_relevant=0,
            cooldown_skips=0,
        )
        recovery = r["per_domain"]["agorarn.com.br"]["parser_recovery_count"]
        assert recovery == {"jsonld": 2, "readability": 1}
