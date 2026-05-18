"""LLM synthesis layer — Eixo 2 v2c.

Formats the top-k :class:`~mapear_nlp.rag.retriever.NarrativeHit` objects
into a context block and prompts Claude Haiku for a Portuguese-language
synthesis.  Follows the same never-raise convention as the narrative
explainer: errors land in :attr:`RAGAnswer.error` rather than propagating.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mapear_nlp.llm.client import LLMClient
    from mapear_nlp.rag.retriever import NarrativeHit


_PROMPT_TEMPLATE = """\
Você é um analista assistente do projeto Mapear-RN, um sistema de \
monitoramento sociopolítico do Rio Grande do Norte para as eleições 2026.

Abaixo estão as {n} narrativas mais similares à consulta do analista, \
recuperadas do arquivo histórico. Cada narrativa é um resumo gerado por IA \
de um artigo jornalístico classificado como alerta político.

CONSULTA DO ANALISTA:
{query}

NARRATIVAS RECUPERADAS (por similaridade semântica — menor distância = mais similar):
{context}

Com base exclusivamente nas narrativas acima, responda à consulta do analista \
em 2 a 4 frases em português. Cite datas, posicionamentos (favor/contra/neutro) \
e padrões de cluster quando relevantes. Se as narrativas não forem suficientes \
para responder com segurança, diga isso claramente.\
"""


@dataclass(frozen=True)
class RAGAnswer:
    """Result of a single RAG query."""

    query: str
    answer: str
    hits: list[NarrativeHit] = field(default_factory=list)
    model: str = ""
    k: int = 5
    region: str | None = None
    embedding_model: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    error: str | None = None


def _format_hit(rank: int, hit: NarrativeHit) -> str:
    date_str = (
        hit.published_at.strftime("%Y-%m-%d")
        if hit.published_at
        else "data desconhecida"
    )
    lines = [f"[{rank}] ({date_str}) dist={hit.distance:.4f}"]
    lines.append(f"    {hit.narrative_summary}")
    meta: list[str] = []
    if hit.person_name:
        role = f" ({hit.person_role})" if hit.person_role else ""
        meta.append(f"alvo: {hit.person_name}{role}")
    if hit.stance_label:
        conf = f"/{hit.stance_confidence}" if hit.stance_confidence else ""
        meta.append(f"posicionamento: {hit.stance_label}{conf}")
    if hit.cluster_id is not None and hit.cluster_id >= 0:
        cluster_info = f"cluster #{hit.cluster_id}"
        if hit.cluster_size:
            cluster_info += f" ({hit.cluster_size} membros)"
        meta.append(cluster_info)
    if meta:
        lines.append("    " + " | ".join(meta))
    return "\n".join(lines)


def generate(
    query: str,
    hits: list[NarrativeHit],
    *,
    llm_client: LLMClient,
    max_tokens: int = 400,
    temperature: float = 0.2,
    timeout_seconds: float = 30.0,
    region: str | None = None,
    embedding_model: str = "",
) -> RAGAnswer:
    """Synthesize a Portuguese-language answer from retrieved narratives.

    Never raises — errors are captured in :attr:`RAGAnswer.error`.
    """
    if not hits:
        return RAGAnswer(
            query=query,
            answer="Nenhuma narrativa similar encontrada no arquivo histórico.",
            hits=[],
            model=llm_client.model,
            region=region,
            embedding_model=embedding_model,
        )

    context = "\n\n".join(_format_hit(i + 1, h) for i, h in enumerate(hits))
    prompt = _PROMPT_TEMPLATE.format(n=len(hits), query=query, context=context)

    try:
        answer_text = llm_client.complete(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )
        return RAGAnswer(
            query=query,
            answer=answer_text,
            hits=hits,
            model=llm_client.model,
            k=len(hits),
            region=region,
            embedding_model=embedding_model,
        )
    except Exception as exc:  # noqa: BLE001
        return RAGAnswer(
            query=query,
            answer="",
            hits=hits,
            model=llm_client.model,
            k=len(hits),
            region=region,
            embedding_model=embedding_model,
            error=str(exc),
        )
