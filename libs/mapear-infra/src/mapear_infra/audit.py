"""Audit log for content leaving the warehouse (Eixo 6 light).

Records every LLM call (or attempted call) made by the pipelines, so
that an LGPD / SOC-2 review can answer: *who* (tenant) sent *what*
(content_hash) to *which provider* under *which prompt + redaction*,
and *when*. The log entry purposely does NOT include the prompt body
or the model output — only references to them. Sensitive substance
lives only in the GCS narrative cache (already content-addressed and
access-controlled).

Emit-shape: a single ``loguru.bind(...).info(...)`` line with
``audit=true`` so downstream Cloud Logging filters can route audit
entries to a separate log bucket / sink without touching the rest of
the pipeline logs. A future v2 will materialise these into a BQ table
(``mapear_audit.llm_calls``) once volume + retention requirements
firm up — for v1 the structured-log shape is the contract.

The function never raises: a logging failure must not bring down the
pipeline. ``status="logging_failed"`` is reserved for the rare case
where the logger sink itself throws.
"""

from __future__ import annotations

from typing import Literal

from loguru import logger

AuditStatus = Literal[
    "cache_hit",
    "cache_miss_ok",
    "cache_miss_error",
    "skipped_no_llm",
    "skipped_non_alert",
    "logging_failed",
]


def log_llm_call(
    *,
    tenant_id: str | None,
    region: str,
    content_hash: str,
    prompt_version: str,
    provider: str,
    model: str,
    redaction_level: str,
    redaction_counts: dict[str, int],
    status: AuditStatus,
    latency_ms: float | None = None,
    error: str | None = None,
) -> None:
    """Emit one structured audit entry. Never raises.

    ``redaction_counts`` is a category→count map (``{"email": 2, "cpf": 1}``)
    so an analyst can see at a glance whether anything was scrubbed
    without having to re-run the redactor. Empty when nothing matched
    or when redaction was disabled.
    """
    try:
        logger.bind(
            audit=True,
            audit_kind="llm_call",
            tenant_id=tenant_id,
            region=region,
            content_hash=content_hash,
            prompt_version=prompt_version,
            provider=provider,
            model=model,
            redaction_level=redaction_level,
            redaction_counts=dict(redaction_counts) if redaction_counts else {},
            redaction_total=sum(redaction_counts.values()) if redaction_counts else 0,
            status=status,
            latency_ms=latency_ms,
            error=error,
        ).info(
            "llm_call audit: status={status} provider={provider} hash={hash}",
            status=status,
            provider=provider,
            hash=content_hash[:12],
        )
    except Exception as exc:  # noqa: BLE001 — audit must never raise
        # Best-effort second-channel signal that the audit sink is sick.
        # Falls back to print so even a broken loguru config surfaces it.
        import sys

        sys.stderr.write(
            f"AUDIT EMIT FAILED for {content_hash[:12]} status={status}: {exc}\n"
        )


PersonaAuditStatus = Literal[
    "persona_created",
    "persona_unchanged",
    "logging_failed",
]


def log_persona_resolution(
    *,
    tenant_id: str | None,
    region: str | None,
    persona_id: str,
    member_count: int,
    platforms: tuple[str, ...],
    confidence: float,
    resolution_version: str,
    status: PersonaAuditStatus = "persona_created",
    job_run_id: str | None = None,
) -> None:
    """Audit entry for a cross-platform persona materialisation — Eixo 3 v2b.

    persona_id links accounts across platforms — a sensitive derived
    identifier under LGPD. Each persona created (and optionally each
    unchanged-re-emission) lands as a structured loguru line so an
    operator can answer "which platforms got stitched, when, why".
    The line carries only the *fact* of the stitch (persona_id +
    platforms + count + confidence) — no PII. Cross-reference to the
    raw evidence happens via persona_id against silver_author_personas.

    Never raises — the audit sink must not bring down the resolution
    job.
    """
    try:
        logger.bind(
            audit=True,
            audit_kind="author_persona",
            tenant_id=tenant_id,
            region=region,
            persona_id=persona_id,
            member_count=member_count,
            platforms=list(platforms),
            confidence=round(confidence, 4),
            resolution_version=resolution_version,
            status=status,
            job_run_id=job_run_id,
        ).info(
            "persona audit: status={status} persona={persona_id} n={member_count}",
            status=status,
            persona_id=persona_id,
            member_count=member_count,
        )
    except Exception as exc:  # noqa: BLE001 — audit must never raise
        import sys

        sys.stderr.write(
            f"AUDIT EMIT FAILED for persona {persona_id} status={status}: {exc}\n"
        )
