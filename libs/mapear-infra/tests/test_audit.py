"""Tests for the LLM audit log (Eixo 6 light)."""

from __future__ import annotations

import json

from loguru import logger

from mapear_infra.audit import log_llm_call


def _capture(records: list[dict]):
    """Loguru sink that drops each record into ``records`` as a dict."""

    def sink(message):  # noqa: ANN001
        rec = message.record
        records.append(
            {
                "message": rec["message"],
                "extra": dict(rec["extra"]),
                "level": rec["level"].name,
            }
        )

    return sink


def test_log_llm_call_emits_structured_record() -> None:
    records: list[dict] = []
    handler_id = logger.add(_capture(records), level="INFO", serialize=False)
    try:
        log_llm_call(
            tenant_id="acme",
            region="rn",
            content_hash="0123456789abcdef",
            prompt_version="narrative_v1",
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            redaction_level="masked",
            redaction_counts={"email": 2, "cpf": 1},
            status="cache_miss_ok",
            latency_ms=850.3,
        )
    finally:
        logger.remove(handler_id)
    assert len(records) == 1
    extra = records[0]["extra"]
    assert extra["audit"] is True
    assert extra["audit_kind"] == "llm_call"
    assert extra["tenant_id"] == "acme"
    assert extra["region"] == "rn"
    assert extra["content_hash"] == "0123456789abcdef"
    assert extra["redaction_level"] == "masked"
    assert extra["redaction_counts"] == {"email": 2, "cpf": 1}
    assert extra["redaction_total"] == 3
    assert extra["status"] == "cache_miss_ok"
    assert extra["latency_ms"] == 850.3


def test_log_llm_call_with_empty_redaction_counts() -> None:
    records: list[dict] = []
    handler_id = logger.add(_capture(records), level="INFO", serialize=False)
    try:
        log_llm_call(
            tenant_id=None,
            region="rn",
            content_hash="abc",
            prompt_version="narrative_v1",
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            redaction_level="none",
            redaction_counts={},
            status="cache_hit",
        )
    finally:
        logger.remove(handler_id)
    assert len(records) == 1
    extra = records[0]["extra"]
    assert extra["redaction_counts"] == {}
    assert extra["redaction_total"] == 0


def test_log_llm_call_includes_error_field_on_failure() -> None:
    records: list[dict] = []
    handler_id = logger.add(_capture(records), level="INFO", serialize=False)
    try:
        log_llm_call(
            tenant_id="acme",
            region="rn",
            content_hash="abc",
            prompt_version="narrative_v1",
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            redaction_level="masked",
            redaction_counts={"email": 1},
            status="cache_miss_error",
            error="timeout after 30s",
        )
    finally:
        logger.remove(handler_id)
    assert records[0]["extra"]["error"] == "timeout after 30s"
    assert records[0]["extra"]["status"] == "cache_miss_error"


def test_log_llm_call_serialises_to_json_when_sink_does() -> None:
    """The structured fields must survive a serialised loguru sink — that's
    how production routes them to Cloud Logging.
    """
    seen: list[str] = []

    def serialised_sink(message):  # noqa: ANN001
        seen.append(message)

    handler_id = logger.add(serialised_sink, level="INFO", serialize=True)
    try:
        log_llm_call(
            tenant_id="acme",
            region="rn",
            content_hash="abc123",
            prompt_version="narrative_v1",
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            redaction_level="masked",
            redaction_counts={"phone": 1},
            status="cache_miss_ok",
        )
    finally:
        logger.remove(handler_id)
    payload = json.loads(seen[0])
    assert payload["record"]["extra"]["audit_kind"] == "llm_call"
    assert payload["record"]["extra"]["redaction_counts"] == {"phone": 1}
