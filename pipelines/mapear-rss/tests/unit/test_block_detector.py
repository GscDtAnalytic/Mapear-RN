"""Tests for the anti-bot block detector."""

from mapear_rss.extraction.block_detector import classify_failure, detect

CLOUDFLARE_HTML = """
<html><head><title>Just a moment...</title></head>
<body>Checking your browser before accessing the site.
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script>
</body></html>
"""

CAPTCHA_HTML = """
<html><body>
<div class="g-recaptcha" data-sitekey="abc"></div>
Please verify you are a human.
</body></html>
"""

CF_CHLOPT_HTML = (
    "<html><body><script>var _cf_chl_opt={cType:'managed'};</script></body></html>"
)

DATADOME_HTML = (
    "<html><head><title>DataDome</title></head>"
    "<body><p>Access to this resource is protected by DataDome.</p></body></html>"
)

VALID_HTML = (
    "<html><head><title>Notícia</title></head><body>"
    + ("<p>Conteúdo real de um artigo jornalístico. </p>" * 10)
    + "</body></html>"
)


class TestDetect:
    def test_cloudflare_header_alone_not_blocked(self) -> None:
        # CF sends cf-ray on ALL responses including real article HTML.
        # Headers alone must NOT trigger a block — only challenge body markers do.
        signals = detect(200, {"cf-ray": "abc123", "server": "cloudflare"}, VALID_HTML)
        assert not signals.cloudflare_detected
        assert not signals.blocked
        # Headers are still recorded for telemetry.
        assert "header:cf-ray" in signals.markers_hit
        assert "header:server=cloudflare" in signals.markers_hit

    def test_cloudflare_body_challenge(self) -> None:
        signals = detect(503, {}, CLOUDFLARE_HTML)
        assert signals.cloudflare_detected or signals.js_challenge_detected
        assert signals.blocked

    def test_cloudflare_header_plus_challenge_body_blocked(self) -> None:
        # Real challenge page: CF headers AND challenge body → blocked.
        signals = detect(
            200,
            {"cf-ray": "abc123", "server": "cloudflare"},
            CLOUDFLARE_HTML,
        )
        assert signals.blocked

    def test_captcha_detected(self) -> None:
        signals = detect(200, {}, CAPTCHA_HTML)
        assert signals.captcha_detected
        assert signals.blocked

    def test_empty_body_flagged(self) -> None:
        signals = detect(200, {}, "")
        assert signals.empty_body
        assert not signals.blocked  # empty != blocked, parser_failure path

    def test_403_with_small_body_is_waf(self) -> None:
        signals = detect(403, {}, "<html>denied</html>")
        assert signals.waf_detected
        assert signals.blocked

    def test_valid_html_clean(self) -> None:
        signals = detect(200, {"content-type": "text/html"}, VALID_HTML)
        assert not signals.blocked
        assert not signals.empty_body

    def test_403_cfray_checking_browser_blocked(self) -> None:
        # 403 + CF header + challenge body → blocked (test case 2 from spec).
        body = (
            "<html><body>Checking your browser before accessing the site.</body></html>"
        )
        signals = detect(
            403,
            {"cf-ray": "abc123", "server": "cloudflare"},
            body,
        )
        assert signals.blocked

    def test_200_cfray_cf_chl_opt_blocked(self) -> None:
        # 200 + CF header + _cf_chl_opt body → blocked (test case 3 from spec).
        signals = detect(200, {"cf-ray": "abc123"}, CF_CHLOPT_HTML)
        assert signals.cloudflare_detected
        assert signals.blocked

    def test_datadome_body_detected(self) -> None:
        # DataDome challenge body → waf_detected (test case 4 from spec).
        signals = detect(200, {}, DATADOME_HTML)
        assert signals.waf_detected
        assert signals.blocked

    def test_cf_mitigated_challenge_header_blocked(self) -> None:
        # cf-mitigated=challenge → cloudflare_detected even without body markers.
        signals = detect(200, {"cf-ray": "x", "cf-mitigated": "challenge"}, VALID_HTML)
        assert signals.cloudflare_detected
        assert signals.blocked
        assert "header:cf-mitigated=challenge" in signals.markers_hit

    def test_cf_mitigated_other_value_not_blocked(self) -> None:
        # cf-mitigated with a value other than "challenge" → CDN telemetry only.
        signals = detect(200, {"cf-mitigated": "managed"}, VALID_HTML)
        assert not signals.cloudflare_detected
        assert not signals.blocked


class TestClassifyFailure:
    def test_timeout_exception_wins(self) -> None:
        signals = detect(200, {}, VALID_HTML)
        assert (
            classify_failure(200, signals, 500, exception_name="TimeoutException")
            == "timeout"
        )

    def test_blocked_beats_http_error(self) -> None:
        signals = detect(403, {}, CLOUDFLARE_HTML)
        assert classify_failure(403, signals, 0) == "blocked_bot"

    def test_404_canonical(self) -> None:
        signals = detect(404, {}, "")
        assert classify_failure(404, signals, 0) == "http_404"

    def test_selector_missing_when_html_ok_but_no_extract(self) -> None:
        signals = detect(200, {}, VALID_HTML)
        assert classify_failure(200, signals, 0) == "selector_missing"

    def test_unknown_fallback(self) -> None:
        signals = detect(200, {}, VALID_HTML)
        assert classify_failure(200, signals, 500) == "unknown"
