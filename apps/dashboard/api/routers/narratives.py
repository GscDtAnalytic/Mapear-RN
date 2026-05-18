from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.bq import DATASET_SILVER, query, tbl, df_to_records

router = APIRouter()

# Modelo de embedding do corpus em silver_narrative_embeddings. DEVE ser o
# mesmo usado pelo job de clustering (ver infra/main.tf e mapear-infra/config.py)
# — embeddar a query com outro modelo gera vetores de dimensão incompatível
# (mpnet = 768d) e resultados sem sentido.
EMBEDDING_MODEL = os.getenv(
    "MAPEAR_EMBEDDINGS_MODEL", "paraphrase-multilingual-mpnet-base-v2"
)


@lru_cache(maxsize=1)
def _encoder():
    """Carrega o sentence-transformer uma vez por processo. O peso (~1GB) é
    pré-baixado na imagem Docker, então não há download em runtime."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL)


@router.get("/narratives/clusters")
def clusters(days: int = 30):
    df = query(
        f"""
        SELECT c.cluster_id, c.cluster_label,
               c.cluster_size AS article_count,
               c.cluster_run_date,
               a.title AS centroid_title
        FROM {tbl('fct_narrative_cluster_daily')} c
        LEFT JOIN {tbl('gold_articles')} a
               ON c.centroid_content_hash = a.content_hash
        WHERE c.cluster_run_date >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
          AND c.cluster_id != -1
        ORDER BY c.cluster_run_date DESC, c.cluster_size DESC
        LIMIT 30
        """,
        days=days,
    )
    return df_to_records(df)


@router.get("/narratives/recent-articles")
def recent_articles(days: int = 14, limit: int = 20):
    df = query(
        f"""
        SELECT content_hash, title, source_feed, published_at,
               narrative_summary, sentiment_overall
        FROM {tbl('gold_articles')}
        WHERE is_rn_relevant = TRUE
          AND narrative_summary IS NOT NULL
          AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        ORDER BY published_at DESC LIMIT @lim
        """,
        days=days,
        lim=limit,
    )
    return df_to_records(df)


class SearchRequest(BaseModel):
    query: str
    k: int = 5
    region: Optional[str] = "rn"


@router.post("/narratives/search")
def search(req: SearchRequest):
    api_key = os.getenv("MAPEAR_LLM_API_KEY")
    if not api_key or api_key == "CHANGE_ME":
        raise HTTPException(
            status_code=503,
            detail="Busca com IA indisponível: MAPEAR_LLM_API_KEY não configurada no servidor.",
        )

    try:
        embedding = _encoder().encode(req.query).tolist()
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Busca com IA indisponível: dependência sentence-transformers ausente na imagem.",
        )

    # VECTOR_SEARCH expõe a linha do corpus no struct `base`; filtramos o
    # corpus pelo mesmo embedding_model da query para garantir 768 dims.
    emb_literal = ", ".join(repr(float(x)) for x in embedding)
    sql = f"""
    SELECT
        base.content_hash,
        ga.title,
        ga.narrative_summary,
        ga.published_at,
        ga.source_feed,
        distance
    FROM VECTOR_SEARCH(
        (
            SELECT content_hash, embedding
            FROM {tbl('silver_narrative_embeddings', DATASET_SILVER)}
            WHERE embedding_model = '{EMBEDDING_MODEL}'
        ),
        'embedding',
        (SELECT [{emb_literal}] AS embedding),
        top_k => {int(req.k)},
        distance_type => 'COSINE'
    )
    LEFT JOIN {tbl('gold_articles')} ga
      ON base.content_hash = ga.content_hash
    ORDER BY distance ASC
    """
    hits = df_to_records(query(sql))

    try:
        import anthropic
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Busca com IA indisponível: dependência anthropic ausente na imagem.",
        )

    client = anthropic.Anthropic(api_key=api_key)

    context_text = "\n\n".join(
        f"[{i+1}] {h.get('title') or '(sem título)'}\n{h.get('narrative_summary') or ''}"
        for i, h in enumerate(hits)
    )
    prompt = (
        "Você é um analista político especialista no Rio Grande do Norte, Brasil.\n"
        "Com base apenas nos documentos abaixo, responda à pergunta em português.\n"
        "Cite as fontes pelo número entre colchetes. Se os documentos não "
        "responderem à pergunta, diga isso claramente.\n\n"
        f"Pergunta: {req.query}\n\nDocumentos:\n{context_text}\n\nResposta:"
    )
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    answer = msg.content[0].text if msg.content else ""

    return {"answer": answer, "sources": hits, "query": req.query}
