"""
Audit pipeline for identity resolution: validates targets, detects collisions,
enqueues suspicious cases for human review, and generates audit reports.

Rules enforced:
  R1 — at most one official handle per (target × platform)
  R2 — institutional account names (Prefeitura, Câmara, etc.) must not
       inherit a natural-person person_id without strong evidence
  R3 — pairs of targets whose normalised names share ≥ 2 significant tokens
       (≥ 4 chars) are collision-prone and require extra context to resolve

Usage:
    auditor = IdentityAuditor(targets)
    violations = auditor.validate_targets()
    report_md = auditor.generate_audit_report(review_queue=[])
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

from mapear_domain.entity_resolution.confidence_scorer import (
    IDENTITY_RESOLUTION_VERSION,
    _normalize,
    _tokens,
)

if TYPE_CHECKING:
    from mapear_domain.entity_resolution.person_resolver import ResolutionResult, Target


_INSTITUTIONAL_TOKENS = frozenset(
    {
        "prefeitura",
        "camara",
        "câmara",
        "secretaria",
        "gabinete",
        "governo",
        "secom",
        "assessoria",
        "imprensa",
        "comunicacao",
        "comunicação",
        "municipio",
        "município",
        "administracao",
        "administração",
        "vereadores",
        "legislativo",
        "paroquia",
        "santuario",
        "santuário",
        "ministerio",
        "ministério",
        "fundacao",
        "fundação",
        "instituto",
    }
)

_LOW_CONFIDENCE_REVIEW_THRESHOLD = 0.75


class ViolationKind(str, Enum):
    HANDLE_COLLISION = "handle_collision"
    INSTITUTIONAL_NAME = "institutional_name"
    NAME_COLLISION_PRONE = "name_collision_prone"
    LOW_CONFIDENCE = "low_confidence"
    INVALID_HANDLE_FORMAT = "invalid_handle_format"


# Handle format rules per platform.
# X: https://help.x.com/en/managing-your-account/x-username-rules
# Instagram: https://help.instagram.com/583107688369069
# TikTok: https://support.tiktok.com/en/getting-started/setting-up-your-profile/changing-your-username
# Facebook allows either a vanity page handle or a profile.php?id=<numeric> path.
_HANDLE_PATTERNS: dict[str, re.Pattern[str]] = {
    "x": re.compile(r"^[A-Za-z0-9_]{1,15}$"),
    "instagram": re.compile(r"^[A-Za-z0-9._]{1,30}$"),
    "tiktok": re.compile(r"^[A-Za-z0-9._]{1,24}$"),
    # Facebook: vanity page (letters, digits, dot, hyphen) OR numeric profile path.
    "facebook": re.compile(r"^(?:[A-Za-z0-9.\-]{1,50}|profile\.php\?id=\d+)$"),
}


def validate_handle_format(platform: str, handle: str) -> str | None:
    """Return ``None`` when the handle matches the platform's format rules.

    Returns a human-readable reason string otherwise. Used by the auditor
    (for static validation) and by platform adapters (for pre-flight
    filtering, so an obviously malformed handle never reaches the API).
    """
    if not handle:
        return "empty"
    stripped = _strip_handle(handle)
    if not stripped:
        return "empty_after_strip"
    pattern = _HANDLE_PATTERNS.get(platform)
    if pattern is None:
        return None
    if not pattern.match(stripped):
        return f"does_not_match_{platform}_format"
    return None


@dataclass
class ValidationViolation:
    kind: ViolationKind
    person_id: str
    detail: str
    other_person_id: str | None = None
    severity: str = "warning"


@dataclass
class ReviewItem:
    post_id: str
    platform: str
    handle: str
    page_name: str | None
    person_id: str | None
    confidence: float
    scope_status: str
    reasons: list[str]
    candidates: list[str]
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    identity_resolution_version: str = IDENTITY_RESOLUTION_VERSION

    def as_dict(self) -> dict:
        return {
            "post_id": self.post_id,
            "platform": self.platform,
            "handle": self.handle,
            "page_name": self.page_name,
            "person_id": self.person_id,
            "confidence": self.confidence,
            "scope_status": self.scope_status,
            "reasons": self.reasons,
            "candidates": self.candidates,
            "created_at": self.created_at,
            "identity_resolution_version": self.identity_resolution_version,
        }


def _strip_handle(handle: str) -> str:
    h = handle.strip().lower().removeprefix("@")
    for prefix in (
        "https://facebook.com/",
        "https://www.facebook.com/",
        "https://instagram.com/",
        "https://www.instagram.com/",
        "https://twitter.com/",
        "https://x.com/",
        "https://tiktok.com/@",
        "https://www.tiktok.com/@",
        "https://tiktok.com/",
        "https://www.tiktok.com/",
    ):
        if h.startswith(prefix):
            h = h.removeprefix(prefix)
    return h.rstrip("/")


def is_institutional_name(page_name: str | None) -> bool:
    """True if page_name contains tokens associated with institutional accounts."""
    if not page_name:
        return False
    norm_tokens = {t for t in _normalize(page_name).split() if len(t) >= 4}
    return bool(norm_tokens & _INSTITUTIONAL_TOKENS)


class IdentityAuditor:
    """Validate a list of Target objects and flag suspicious resolutions."""

    def __init__(self, targets: list[Target]) -> None:
        self._targets = targets

    def validate_targets(self) -> list[ValidationViolation]:
        """Run all static validation rules against the loaded targets."""
        violations: list[ValidationViolation] = []
        violations.extend(self._detect_handle_collisions())
        violations.extend(self._detect_collision_prone_pairs())
        violations.extend(self._detect_invalid_handle_formats())
        return violations

    def should_enqueue(
        self,
        result: ResolutionResult,
        post_id: str,
        platform: str,
        handle: str,
        page_name: str | None = None,
        bio: str | None = None,
    ) -> tuple[bool, list[str]]:
        """Decide whether this resolution needs human review.

        Returns (enqueue: bool, reasons: list[str]).
        """
        from mapear_domain.entity_resolution.person_resolver import ScopeStatus

        reasons: list[str] = []

        if (
            result.scope_status == ScopeStatus.IN_SCOPE
            and result.confidence < _LOW_CONFIDENCE_REVIEW_THRESHOLD
        ):
            reasons.append(f"low_confidence:{result.confidence:.3f}")

        if result.scope_status == ScopeStatus.AMBIGUOUS:
            reasons.append("ambiguous_scope")

        if is_institutional_name(page_name):
            reasons.append("institutional_page_name")

        if result.confidence_breakdown and result.confidence_breakdown.name_sim < 0.20:
            reasons.append("name_mismatch")

        return bool(reasons), reasons

    def generate_audit_report(
        self,
        violations: list[ValidationViolation] | None = None,
        review_queue: list[ReviewItem] | None = None,
    ) -> str:
        """Generate a markdown audit report for the current targets."""
        if violations is None:
            violations = self.validate_targets()
        if review_queue is None:
            review_queue = []

        errors = [v for v in violations if v.severity == "error"]
        warnings = [v for v in violations if v.severity == "warning"]

        handle_collisions = [
            v for v in violations if v.kind == ViolationKind.HANDLE_COLLISION
        ]
        collision_prone = [
            v for v in violations if v.kind == ViolationKind.NAME_COLLISION_PRONE
        ]

        lines: list[str] = [
            f"# Auditoria de Resolução de Identidade — {IDENTITY_RESOLUTION_VERSION}",
            "",
            f"**Data:** {datetime.now(UTC).strftime('%Y-%m-%d')}  ",
            f"**Algoritmo:** `{IDENTITY_RESOLUTION_VERSION}` (confiança calculada por observação)",  # noqa: E501
            "",
            "## Sumário",
            "",
            "| Métrica | Valor |",
            "|---------|-------|",
            f"| Total de targets | {len(self._targets)} |",
            f"| Violações críticas (ERROR) | {len(errors)} |",
            f"| Avisos (WARNING) | {len(warnings)} |",
            f"| — Colisões de handle | {len(handle_collisions)} |",
            f"| — Pares suscetíveis a colisão de nome | {len(collision_prone)} |",
            f"| Itens na fila de revisão | {len(review_queue)} |",
            "",
        ]

        if errors:
            lines += [
                "## Violações Críticas (ERROR)",
                "",
            ]
            for v in errors:
                lines.append(
                    f"- **{v.kind.value}** `{v.person_id}`"
                    + (f" ↔ `{v.other_person_id}`" if v.other_person_id else "")
                    + f": {v.detail}"
                )
            lines.append("")

        if handle_collisions:
            lines += [
                "## Colisões de Handle (ERROR)",
                "",
                "| Platform | Handle | Actor 1 | Actor 2 |",
                "|----------|--------|---------|---------|",
            ]
            for v in handle_collisions:
                detail_parts = v.detail.split("'")
                handle_str = detail_parts[1] if len(detail_parts) > 1 else v.detail
                platform_str = v.detail.split(" handle")[0]
                row = (
                    f"| {platform_str} | `{handle_str}` "
                    f"| `{v.person_id}` | `{v.other_person_id}` |"
                )
                lines.append(row)
            lines.append("")

        if collision_prone:
            lines += [
                "## Pares Suscetíveis a Colisão de Nome (WARNING)",
                "",
                "Estes pares compartilham tokens significativos — "
                "resolução requer corroboração de contexto.",
                "",
                "| Actor 1 | Actor 2 | Tokens comuns |",
                "|---------|---------|--------------|",
            ]
            for v in collision_prone:
                lines.append(
                    f"| `{v.person_id}` | `{v.other_person_id}` | {v.detail} |"
                )
            lines.append("")

        lines += [
            "## Fila de Revisão (snapshot atual)",
            "",
        ]
        if review_queue:
            lines += [
                "| Post ID | Platform | Handle | Person ID | Confiança | Razões |",
                "|---------|----------|--------|-----------|-----------|--------|",
            ]
            for item in review_queue[:50]:
                lines.append(
                    f"| {item.post_id[:12]}… | {item.platform} | {item.handle} "
                    f"| `{item.person_id}` | {item.confidence:.3f} "
                    f"| {', '.join(item.reasons)} |"
                )
            if len(review_queue) > 50:
                lines.append(
                    f"| *(+ {len(review_queue) - 50} itens omitidos)* | | | | | |"
                )
        else:
            lines.append("*Fila vazia — nenhum caso pendente.*")
        lines.append("")

        lines += [
            "## Distribuição de `resolution_confidence` (v2)",
            "",
            "A partir de `v2`, a confiança não é mais constante. Faixas esperadas:",
            "",
            "| Faixa | Significado |",
            "|-------|-------------|",
            "| 0.85 – 0.95 | Handle + nome correto (match limpo) |",
            "| 0.65 – 0.85 | Handle correto, nome parcial (nome curto / apelido) |",
            "| 0.40 – 0.65 | Handle correto, nome divergente → revisar |",
            "| < 0.40 | Sem handle; match fraco → OUT_OF_SCOPE ou revisão |",
            "",
            "Valores < 0.75 com scope `IN_SCOPE` são automaticamente enfileirados "
            "para revisão no `identity_review_queue`.",
        ]

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    def _detect_handle_collisions(self) -> list[ValidationViolation]:
        violations: list[ValidationViolation] = []
        platforms: dict[str, object] = {
            "facebook": lambda t: t.facebook_page,
            "instagram": lambda t: t.instagram_username,
            "x": lambda t: t.x_handle,
            "tiktok": lambda t: t.tiktok_handle,
        }
        for platform, getter in platforms.items():
            seen: dict[str, str] = {}
            for t in self._targets:
                raw = getter(t)  # type: ignore[operator]
                handle = _strip_handle(raw) if raw else ""
                if not handle:
                    continue
                if handle in seen:
                    violations.append(
                        ValidationViolation(
                            kind=ViolationKind.HANDLE_COLLISION,
                            person_id=t.person_id,
                            other_person_id=seen[handle],
                            detail=(
                                f"{platform} handle '{handle}' compartilhado por "
                                f"{seen[handle]} e {t.person_id}"
                            ),
                            severity="error",
                        )
                    )
                else:
                    seen[handle] = t.person_id
        return violations

    def _detect_invalid_handle_formats(self) -> list[ValidationViolation]:
        """Flag handles that violate platform format rules (X ≤15 chars etc.).

        Added after the 2026-04-24 DLQ audit surfaced `paulinhofreirern` (16
        chars) being retried by XAdapter every run and clogging the DLQ with
        deterministic ``HTTP 400`` responses. Pre-flight validation fails loud
        in the seed instead of burning API quota.
        """
        violations: list[ValidationViolation] = []
        platforms: dict[str, object] = {
            "facebook": lambda t: t.facebook_page,
            "instagram": lambda t: t.instagram_username,
            "x": lambda t: t.x_handle,
            "tiktok": lambda t: t.tiktok_handle,
        }
        for platform, getter in platforms.items():
            for t in self._targets:
                raw = getter(t)  # type: ignore[operator]
                if not raw:
                    continue
                reason = validate_handle_format(platform, raw)
                if reason is None:
                    continue
                violations.append(
                    ValidationViolation(
                        kind=ViolationKind.INVALID_HANDLE_FORMAT,
                        person_id=t.person_id,
                        detail=(f"{platform} handle {raw!r} is invalid: {reason}"),
                        severity="error",
                    )
                )
        return violations

    def _detect_collision_prone_pairs(self) -> list[ValidationViolation]:
        """Flag pairs whose canonical names share a significant token (>=4 chars).

        Aliases are excluded to avoid false positives. Canonical-name comparison
        reliably catches shared FIRST NAME or SURNAME collision risks.
        """
        violations: list[ValidationViolation] = []
        canonical_tokens: list[tuple[str, set[str]]] = [
            (t.person_id, {tok for tok in _tokens(t.name) if len(tok) >= 4})
            for t in self._targets
        ]

        for i, (pid1, toks1) in enumerate(canonical_tokens):
            for pid2, toks2 in canonical_tokens[i + 1 :]:
                overlap = toks1 & toks2
                if overlap:
                    violations.append(
                        ValidationViolation(
                            kind=ViolationKind.NAME_COLLISION_PRONE,
                            person_id=pid1,
                            other_person_id=pid2,
                            detail=f"`{sorted(overlap)}`",
                            severity="warning",
                        )
                    )
        return violations


class IdentityReviewQueue:
    """Thread-safe in-memory queue for suspicious identity resolutions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[ReviewItem] = []

    def push(self, item: ReviewItem) -> None:
        with self._lock:
            self._items.append(item)

    def snapshot(self) -> list[ReviewItem]:
        with self._lock:
            return list(self._items)

    def drain(self) -> list[ReviewItem]:
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
