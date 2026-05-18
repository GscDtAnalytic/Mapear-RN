"""CLI for the Eixo 2 v2c RAG layer.

Ad-hoc operator tool — not a scheduled job.  Embeds the query, retrieves
the top-k narratives from BigQuery, and synthesises a Portuguese-language
answer via Claude Haiku.  Writes JSON to *stdout* or to ``--out``.

Usage::

    poetry run python -m mapear_nlp.rag.run_rag \\
        --query "Quais narrativas coordenadas atacaram o candidato X?" \\
        --region rn \\
        --k 5

Environment variables required:
  GCP_PROJECT_ID
  MAPEAR_LLM_API_KEY (or MAPEAR_LLM_API_KEY_SECRET)

Optional:
  GCP_BQ_DATASET_SILVER  (default: mapear_silver)
  GCP_BQ_DATASET_GOLD    (default: mapear_gold)
  MAPEAR_EMBEDDINGS_MODEL (default: paraphrase-multilingual-mpnet-base-v2)
  MAPEAR_LLM_MODEL        (default: claude-haiku-4-5-20251001)
  MAPEAR_LLM_RAG_MAX_TOKENS (default: 400)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _log(msg: str) -> None:
    sys.stderr.write(f"[rag] {msg}\n")
    sys.stderr.flush()


def run(
    query: str,
    *,
    region: str | None,
    k: int,
    project: str,
    silver_ds: str,
    gold_ds: str,
    embedding_model: str,
    max_tokens: int = 400,
    out_path: Path | None = None,
    bq_client: Any | None = None,
    embedding_client: Any | None = None,
    llm_client: Any | None = None,
) -> dict:
    """Execute the full RAG pipeline and return the answer dict.

    Optional injected clients allow unit tests to run without GCP credentials
    or a real LLM.  When ``None``, real clients are built from env/settings.
    """
    from mapear_infra.config import get_settings

    from mapear_nlp.rag.generator import generate
    from mapear_nlp.rag.retriever import retrieve

    settings = get_settings()

    if bq_client is None:
        from google.cloud import bigquery

        bq_client = bigquery.Client(project=project)

    if embedding_client is None:
        from mapear_nlp.embeddings.client import get_embedding_client

        embedding_client = get_embedding_client(settings.embeddings)

    if llm_client is None:
        from mapear_nlp.llm.client import get_llm_client

        llm_client = get_llm_client(settings.llm)

    _log(f"retrieving top-{k} narratives | query={query!r} region={region}")
    hits = retrieve(
        query,
        embedding_client=embedding_client,
        bq_client=bq_client,
        project=project,
        silver_ds=silver_ds,
        gold_ds=gold_ds,
        embedding_model=embedding_model,
        region=region,
        k=k,
    )
    _log(f"retrieved {len(hits)} hits")

    answer = generate(
        query,
        hits,
        llm_client=llm_client,
        max_tokens=max_tokens,
        region=region,
        embedding_model=embedding_model,
    )
    _log(f"generated answer ({len(answer.answer)} chars)")

    result = {
        "query": answer.query,
        "answer": answer.answer,
        "region": answer.region,
        "k": answer.k,
        "model": answer.model,
        "embedding_model": answer.embedding_model,
        "generated_at": answer.generated_at.isoformat(),
        "error": answer.error,
        "hits": [
            {
                "rank": i + 1,
                "content_hash": h.content_hash,
                "distance": h.distance,
                "published_at": h.published_at.isoformat() if h.published_at else None,
                "narrative_summary": h.narrative_summary,
                "person_name": h.person_name,
                "person_role": h.person_role,
                "stance_label": h.stance_label,
                "stance_confidence": h.stance_confidence,
                "cluster_id": h.cluster_id,
                "cluster_size": h.cluster_size,
            }
            for i, h in enumerate(hits)
        ],
    }

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if out_path is not None:
        out_path.write_text(payload, encoding="utf-8")
        _log(f"written to {out_path}")
    else:
        sys.stdout.write(payload + "\n")

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RAG over narrative archive — Eixo 2 v2c"
    )
    parser.add_argument(
        "--query", required=True, help="Natural-language question in pt-BR"
    )
    parser.add_argument("--region", default=None, help="Region slug filter (e.g. 'rn')")
    parser.add_argument(
        "--k", type=int, default=5, help="Number of nearest narratives to retrieve"
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="Output JSON path (default: stdout)"
    )
    parser.add_argument(
        "--embedding-model",
        default=os.environ.get(
            "MAPEAR_EMBEDDINGS_MODEL", "paraphrase-multilingual-mpnet-base-v2"
        ),
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=int(os.environ.get("MAPEAR_LLM_RAG_MAX_TOKENS", "400")),
    )
    args = parser.parse_args()

    project = os.environ.get("GCP_PROJECT_ID", "")
    if not project:
        sys.stderr.write("[rag] GCP_PROJECT_ID is required\n")
        return 1

    silver_ds = os.environ.get("GCP_BQ_DATASET_SILVER", "mapear_silver")
    gold_ds = os.environ.get("GCP_BQ_DATASET_GOLD", "mapear_gold")

    result = run(
        args.query,
        region=args.region,
        k=args.k,
        project=project,
        silver_ds=silver_ds,
        gold_ds=gold_ds,
        embedding_model=args.embedding_model,
        max_tokens=args.max_tokens,
        out_path=args.out,
    )

    if result.get("error"):
        sys.stderr.write(f"[rag] error: {result['error']}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
