"""Anthropic Messages API client — Eixo 2 v1 default provider."""

from __future__ import annotations

from mapear_nlp.llm.client import LLMError


class AnthropicClient:
    """Thin wrapper over the Anthropic Messages API.

    Held to the ``LLMClient`` protocol; provider-specific kwargs that
    don't belong on the protocol (system prompts, tool use, beta
    headers) stay private to this class.
    """

    provider = "anthropic"

    def __init__(self, model: str, api_key: str) -> None:
        # Imported lazily so production images that don't ship Anthropic
        # SDK (RSS + social currently route through here only when the
        # explainer is enabled) don't pay the import cost on cold start.
        import anthropic  # type: ignore[import-untyped]

        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> str:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout_seconds,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Anthropic call failed: {exc}") from exc

        # The Messages API returns content blocks; extract first text block.
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "")
                if text:
                    return text.strip()
        raise LLMError(
            f"Anthropic returned no text block (model={self.model}, "
            f"stop_reason={getattr(response, 'stop_reason', '?')})."
        )
