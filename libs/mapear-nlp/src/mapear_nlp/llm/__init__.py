"""LLM client abstraction — Eixo 2 v1.

The pipelines depend on the ``LLMClient`` protocol below; concrete
clients (Anthropic / OpenAI / Vertex) plug in via
``get_llm_client(settings.llm)``. New providers add one module
exposing ``complete(prompt, *, max_tokens, temperature, timeout)`` and
register in ``get_llm_client``.
"""

from mapear_nlp.llm.client import LLMClient, LLMError, get_llm_client

__all__ = ["LLMClient", "LLMError", "get_llm_client"]
