"""LLM client protocol and factory."""

from __future__ import annotations

from typing import Protocol

from mapear_infra.config import LLMConfig


class LLMError(RuntimeError):
    """Raised when an LLM call fails or returns no usable text."""


class LLMClient(Protocol):
    """Minimal text-completion interface used by the narrative explainer.

    Providers map this to their respective SDK call. The narrative
    explainer is a single-shot prompt → string interaction so this
    keeps the surface small; richer interactions (tools, structured
    output) belong on a future class.
    """

    provider: str
    model: str

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> str: ...


def _resolve_api_key(cfg: LLMConfig) -> str:
    if cfg.api_key:
        return cfg.api_key
    if cfg.api_key_secret:
        # Cloud Secret Manager. Imported lazily so local + test paths
        # don't need google-cloud-secret-manager installed.
        from google.cloud import secretmanager  # type: ignore[import-untyped]

        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(name=cfg.api_key_secret)
        return response.payload.data.decode("utf-8")
    raise LLMError(
        f"No API key for provider={cfg.provider}. Set MAPEAR_LLM_API_KEY "
        "locally, or MAPEAR_LLM_API_KEY_SECRET to a Secret Manager "
        "resource name in prod."
    )


def get_llm_client(cfg: LLMConfig) -> LLMClient:
    """Return a concrete client for the configured provider."""
    if cfg.provider == "anthropic":
        from mapear_nlp.llm.anthropic_client import AnthropicClient

        return AnthropicClient(model=cfg.model, api_key=_resolve_api_key(cfg))
    raise LLMError(
        f"Unsupported MAPEAR_LLM_PROVIDER={cfg.provider!r}. v1 supports "
        "'anthropic'; openai/vertex are stubs reserved for v2."
    )
