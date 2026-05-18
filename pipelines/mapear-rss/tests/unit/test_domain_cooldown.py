"""Tests for per-domain adaptive cooldown and retry budgets."""

from mapear_rss.extraction.domain_cooldown import (
    RETRY_BUDGET_DEFAULTS,
    DomainCooldown,
    error_class_for,
    retry_budget,
)

URL_A = "https://example.com/a"
URL_B = "https://other.com/b"


class TestCooldown:
    def test_fresh_domain_is_cool(self) -> None:
        cd = DomainCooldown(base_seconds=10)
        assert cd.is_cool(URL_A)

    def test_record_block_parks_domain(self) -> None:
        cd = DomainCooldown(base_seconds=60)
        until = cd.record_block(URL_A, "blocked_bot", now=1000.0)
        assert until == 1060.0
        assert not cd.is_cool(URL_A, now=1030.0)
        assert cd.is_cool(URL_A, now=1100.0)

    def test_exponential_growth_capped(self) -> None:
        cd = DomainCooldown(base_seconds=10, max_seconds=30, growth_factor=2.0)
        cd.record_block(URL_A, "blocked_bot", now=0.0)  # +10
        cd.record_block(URL_A, "blocked_bot", now=0.0)  # +20
        cd.record_block(URL_A, "blocked_bot", now=0.0)  # +30 (capped)
        cd.record_block(URL_A, "blocked_bot", now=0.0)  # still capped
        snap = cd.snapshot()
        assert snap["example.com"]["cooldown_remaining_s"] <= 30

    def test_success_resets(self) -> None:
        cd = DomainCooldown(base_seconds=60)
        cd.record_block(URL_A, "blocked_bot", now=1000.0)
        cd.record_success(URL_A)
        assert cd.is_cool(URL_A, now=1001.0)

    def test_skips_counted(self) -> None:
        cd = DomainCooldown(base_seconds=60)
        cd.record_block(URL_A, "http_429", now=0.0)
        # Two is_cool calls while parked should both increment skip counter
        cd.is_cool(URL_A, now=1.0)
        cd.is_cool(URL_A, now=2.0)
        assert cd.total_skips() == 2

    def test_independent_domains(self) -> None:
        cd = DomainCooldown(base_seconds=60)
        cd.record_block(URL_A, "blocked_bot", now=0.0)
        assert cd.is_cool(URL_B, now=10.0)


class TestErrorClass:
    def test_bot_block_mapping(self) -> None:
        assert error_class_for("blocked_bot") == "bot_block"
        assert error_class_for("http_403") == "bot_block"

    def test_rate_limit_mapping(self) -> None:
        assert error_class_for("http_429") == "rate_limit"

    def test_parser_hard_mapping(self) -> None:
        assert error_class_for("selector_missing") == "parser_hard"
        assert error_class_for("empty_content") == "parser_hard"

    def test_parser_soft_mapping(self) -> None:
        assert error_class_for("parser_failure") == "parser"

    def test_transient_default(self) -> None:
        assert error_class_for("timeout") == "transient"
        assert error_class_for("http_5xx") == "transient"
        assert error_class_for(None) == "transient"


class TestTriggerThreshold:
    def test_single_block_does_not_arm_when_threshold_2(self) -> None:
        cd = DomainCooldown(base_seconds=60, trigger_threshold=2)
        until = cd.record_block(
            URL_A, "blocked_bot", error_class="bot_block", now=1000.0
        )
        assert until == 0.0  # not armed yet
        assert cd.is_cool(URL_A, now=1030.0)  # still fetchable
        assert cd.applied_count() == 0

    def test_second_block_arms_cooldown(self) -> None:
        cd = DomainCooldown(base_seconds=60, trigger_threshold=2)
        cd.record_block(URL_A, "blocked_bot", error_class="bot_block", now=1000.0)
        until = cd.record_block(
            URL_A, "blocked_bot", error_class="bot_block", now=1000.0
        )
        # growth_exp = consecutive (2) - threshold (2) = 0, window = 60 * 2**0
        assert until == 1060.0
        assert cd.applied_count() == 1
        assert cd.reason_distribution() == {"bot_block": 1}

    def test_parser_class_never_arms(self) -> None:
        cd = DomainCooldown(base_seconds=60, trigger_threshold=1)
        for _ in range(5):
            out = cd.record_block(
                URL_A, "selector_missing", error_class="parser", now=1000.0
            )
            assert out == 0.0
        assert cd.is_cool(URL_A, now=1500.0)
        assert cd.applied_count() == 0
        snap = cd.snapshot()["example.com"]
        assert snap["parser_flags"] == 5

    def test_rate_limit_uses_larger_base(self) -> None:
        cd = DomainCooldown(
            base_seconds=60,
            rate_limit_base_seconds=120,
            trigger_threshold=1,
        )
        until = cd.record_block(URL_A, "http_429", error_class="rate_limit", now=1000.0)
        assert until == 1120.0  # 60 -> would be wrong, 120 from rate_limit base
        assert cd.reason_distribution() == {"rate_limit": 1}

    def test_rate_limit_uses_its_own_cap(self) -> None:
        cd = DomainCooldown(
            base_seconds=60,
            max_seconds=14400,
            rate_limit_base_seconds=900,
            rate_limit_max_seconds=3600,
            trigger_threshold=1,
            growth_factor=2.0,
        )
        # 1st: 900, 2nd: 1800, 3rd: 3600 (capped), 4th: still 3600
        for _ in range(4):
            cd.record_block(URL_A, "http_429", error_class="rate_limit", now=0.0)
        snap = cd.snapshot()["example.com"]
        assert snap["cooldown_remaining_s"] <= 3600

    def test_transient_class_no_cooldown(self) -> None:
        cd = DomainCooldown(base_seconds=60, trigger_threshold=1)
        out = cd.record_block(URL_A, "timeout", error_class="transient", now=1000.0)
        assert out == 0.0
        assert cd.is_cool(URL_A, now=1500.0)


class TestParserHard:
    def test_parser_hard_parks_after_threshold(self) -> None:
        cd = DomainCooldown(
            base_seconds=60,
            parser_hard_seconds=21600,
            trigger_threshold=2,
            parser_hard_trigger=1,  # first hit arms (legacy behaviour)
        )
        until = cd.record_block(
            URL_A, "selector_missing", error_class="parser_hard", now=1000.0
        )
        assert until == 1000.0 + 21600
        assert not cd.is_cool(URL_A, now=1000.0 + 3600)
        assert cd.is_cool(URL_A, now=1000.0 + 25000)
        assert cd.applied_count() == 1
        assert cd.reason_distribution() == {"parser_hard": 1}

    def test_parser_hard_respects_trigger(self) -> None:
        # N=3: first two failures do NOT arm cooldown, third one does.
        cd = DomainCooldown(
            parser_hard_seconds=21600,
            parser_hard_trigger=3,
        )
        assert (
            cd.record_block(
                URL_A, "selector_missing", error_class="parser_hard", now=0.0
            )
            == 0.0
        )
        assert cd.is_cool(URL_A, now=1.0)
        assert (
            cd.record_block(
                URL_A, "selector_missing", error_class="parser_hard", now=0.0
            )
            == 0.0
        )
        assert cd.is_cool(URL_A, now=1.0)
        until = cd.record_block(
            URL_A, "selector_missing", error_class="parser_hard", now=0.0
        )
        assert until == 21600.0
        assert not cd.is_cool(URL_A, now=1.0)
        assert cd.applied_count() == 1

    def test_parser_hard_ignores_parser_disabled_flag(self) -> None:
        cd = DomainCooldown(
            parser_hard_seconds=21600, parser_disabled=True, parser_hard_trigger=1
        )
        until = cd.record_block(
            URL_A, "empty_content", error_class="parser_hard", now=0.0
        )
        assert until > 0.0

    def test_parser_soft_still_does_not_park_when_disabled(self) -> None:
        cd = DomainCooldown(parser_disabled=True)
        out = cd.record_block(URL_A, "parser_failure", error_class="parser", now=0.0)
        assert out == 0.0
        assert cd.is_cool(URL_A, now=1.0)


class TestReset:
    def test_reset_cooldown_releases_parked_domains(self) -> None:
        import time

        now = time.time()
        cd = DomainCooldown(base_seconds=3600, trigger_threshold=1)
        cd.record_block(URL_A, "blocked_bot", now=now)
        cd.record_block(URL_B, "blocked_bot", now=now)
        assert not cd.is_cool(URL_A, now=now + 1)
        assert not cd.is_cool(URL_B, now=now + 1)

        released = cd.reset()
        assert released == 2
        assert cd.is_cool(URL_A, now=now + 1)
        assert cd.is_cool(URL_B, now=now + 1)

    def test_reset_force_clears_all_state(self) -> None:
        cd = DomainCooldown(base_seconds=3600, trigger_threshold=1)
        cd.record_block(URL_A, "blocked_bot", now=0.0)

        released = cd.reset(force=True)
        assert released == 1
        assert cd.applied_count() == 0
        assert cd.reason_distribution() == {}

    def test_reset_soft_skips_already_expired(self) -> None:
        import time

        past = time.time() - 7200  # 2h ago
        cd = DomainCooldown(base_seconds=10, trigger_threshold=1)
        # Park at past, window (10s) already expired by now
        cd.record_block(URL_A, "blocked_bot", now=past)
        # Soft reset finds no active windows
        released = cd.reset()
        assert released == 0


class TestForceScrapeBypass:
    def test_force_scrape_env_bypasses_cooldown(self, monkeypatch) -> None:
        cd = DomainCooldown(base_seconds=3600, trigger_threshold=1)
        cd.record_block(URL_A, "blocked_bot", now=0.0)
        assert not cd.is_cool(URL_A, now=1.0)

        monkeypatch.setenv("FORCE_SCRAPE", "true")
        assert cd.is_cool(URL_A, now=1.0)

    def test_force_scrape_false_does_not_bypass(self, monkeypatch) -> None:
        cd = DomainCooldown(base_seconds=3600, trigger_threshold=1)
        cd.record_block(URL_A, "blocked_bot", now=0.0)

        monkeypatch.setenv("FORCE_SCRAPE", "false")
        assert not cd.is_cool(URL_A, now=1.0)

    def test_force_scrape_unset_normal_behavior(self, monkeypatch) -> None:
        cd = DomainCooldown(base_seconds=3600, trigger_threshold=1)
        cd.record_block(URL_A, "blocked_bot", now=0.0)

        monkeypatch.delenv("FORCE_SCRAPE", raising=False)
        assert not cd.is_cool(URL_A, now=1.0)


class TestRetryBudget:
    def test_defaults_exist_for_known_types(self) -> None:
        for key in (
            "timeout",
            "connection_reset",
            "http_5xx",
            "http_429",
            "http_403",
            "blocked_bot",
            "http_404",
        ):
            assert key in RETRY_BUDGET_DEFAULTS

    def test_overrides_win(self) -> None:
        assert retry_budget("blocked_bot", {"blocked_bot": 5}) == 5

    def test_unknown_uses_fallback(self) -> None:
        assert retry_budget("totally_new_error") == 2
