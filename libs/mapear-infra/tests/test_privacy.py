"""Tests for the PII redactor (Eixo 6 light)."""

from __future__ import annotations

import pytest

from mapear_infra.privacy import RedactionLevel, parse_level, redact

# --- BR patterns: emails ----------------------------------------------------


def test_masked_email_replaces_with_token() -> None:
    result = redact("Contato: joao@exemplo.com.br hoje.", level=RedactionLevel.MASKED)
    assert result.text == "Contato: [email] hoje."
    assert result.counts == {"email": 1}


def test_masked_multiple_emails() -> None:
    text = "Envie para a@b.com ou c@d.gov.br"
    result = redact(text, level=RedactionLevel.MASKED)
    assert result.text == "Envie para [email] ou [email]"
    assert result.counts == {"email": 2}


# --- BR patterns: CPF / CNPJ -----------------------------------------------


def test_masked_cpf_with_separators() -> None:
    result = redact("CPF 123.456.789-09 em sigilo", level=RedactionLevel.MASKED)
    assert result.text == "CPF [cpf] em sigilo"
    assert result.counts == {"cpf": 1}


def test_masked_cpf_bare_11_digits() -> None:
    result = redact("ID 12345678909 cadastrado", level=RedactionLevel.MASKED)
    assert "[cpf]" in result.text
    assert result.counts == {"cpf": 1}


def test_masked_cnpj_with_separators() -> None:
    result = redact("CNPJ 12.345.678/0001-99 lançado", level=RedactionLevel.MASKED)
    assert result.text == "CNPJ [cnpj] lançado"
    assert result.counts == {"cnpj": 1}


def test_masked_cnpj_bare_14_digits() -> None:
    result = redact("Empresa 12345678000199 sancionada", level=RedactionLevel.MASKED)
    assert "[cnpj]" in result.text
    assert result.counts == {"cnpj": 1}


def test_cpf_allowlist_passes_through() -> None:
    # Known placeholder CPF — common in test data — must not be redacted.
    result = redact("Placeholder 00000000000 ainda", level=RedactionLevel.MASKED)
    assert result.text == "Placeholder 00000000000 ainda"
    assert result.counts == {}


# --- BR patterns: telefone --------------------------------------------------


@pytest.mark.parametrize(
    "phone_text",
    [
        "(84) 99999-9999",
        "+55 84 99999-9999",
        "84 99999-9999",
        "84999999999",
        "+5584999999999",
        "(84) 3231-1234",  # landline
    ],
)
def test_masked_phone_variations(phone_text: str) -> None:
    result = redact(f"Ligue {phone_text} hoje", level=RedactionLevel.MASKED)
    assert "[phone]" in result.text
    assert result.counts.get("phone", 0) >= 1


# --- Levels -----------------------------------------------------------------


def test_none_level_passes_text_unchanged() -> None:
    text = "Email a@b.com CPF 123.456.789-09"
    result = redact(text, level=RedactionLevel.NONE)
    assert result.text == text
    assert result.counts == {}


def test_dropped_level_removes_match() -> None:
    result = redact("Contato: joao@exemplo.com ainda", level=RedactionLevel.DROPPED)
    assert result.text == "Contato:  ainda"
    assert result.counts == {"email": 1}


def test_pseudonymized_level_is_stable_across_calls() -> None:
    key = b"test-hmac-key"
    text = "Email a@b.com"
    r1 = redact(text, level=RedactionLevel.PSEUDONYMIZED, hmac_key=key)
    r2 = redact(text, level=RedactionLevel.PSEUDONYMIZED, hmac_key=key)
    assert r1.text == r2.text
    # 8-char tag follows "[email:" prefix.
    assert "[email:" in r1.text
    assert r1.counts == {"email": 1}


def test_pseudonymized_level_changes_with_key() -> None:
    text = "Email a@b.com"
    r1 = redact(text, level=RedactionLevel.PSEUDONYMIZED, hmac_key=b"key-A")
    r2 = redact(text, level=RedactionLevel.PSEUDONYMIZED, hmac_key=b"key-B")
    assert r1.text != r2.text  # different key → different tag


def test_pseudonymized_without_key_raises() -> None:
    with pytest.raises(ValueError, match="hmac_key"):
        redact("Email a@b.com", level=RedactionLevel.PSEUDONYMIZED)


# --- parse_level ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, RedactionLevel.MASKED),
        ("", RedactionLevel.MASKED),
        ("none", RedactionLevel.NONE),
        ("MASKED", RedactionLevel.MASKED),
        ("Pseudonymized", RedactionLevel.PSEUDONYMIZED),
        ("dropped", RedactionLevel.DROPPED),
    ],
)
def test_parse_level_round_trip(raw: str | None, expected: RedactionLevel) -> None:
    assert parse_level(raw) is expected


def test_parse_level_raises_on_unknown_value() -> None:
    with pytest.raises(ValueError, match="Unknown MAPEAR_LLM_PII_LEVEL"):
        parse_level("garbage")


# --- Idempotency ------------------------------------------------------------


def test_redaction_is_idempotent() -> None:
    text = "Email a@b.com CPF 123.456.789-09"
    once = redact(text, level=RedactionLevel.MASKED)
    twice = redact(once.text, level=RedactionLevel.MASKED)
    # After first pass, tokens like [email] / [cpf] are not PII anymore.
    assert once.text == twice.text
    assert twice.counts == {}


# --- Real-world fixtures ----------------------------------------------------


def test_news_excerpt_redacts_only_pii() -> None:
    text = (
        "Em coletiva nesta tarde, o governador Fátima Bezerra anunciou "
        "cortes de R$ 200 milhões na saúde. A denúncia foi enviada por "
        "email anonimo@exemplo.com, e o CPF 123.456.789-09 apareceu "
        "anexado por engano. Para mais informações, ligue (84) 3232-1234."
    )
    result = redact(text, level=RedactionLevel.MASKED)
    assert "Fátima Bezerra" in result.text  # public figure: not redacted
    assert "saúde" in result.text  # ordinary content: not redacted
    assert "R$ 200" in result.text  # financial figures: not redacted
    assert "[email]" in result.text
    assert "[cpf]" in result.text
    assert "[phone]" in result.text
    assert result.total_redactions == 3
