"""Out-of-band social post embedding job — Eixo 2 v2a social.

Designed to run as a Cloud Run Job on a daily cadence. Reads a JSONL
batch of ``SilverSocialPost`` rows from ``silver_social_posts``, embeds
the raw ``text`` field using ``CacheAwareEncoder``, and emits
``silver_social_post_embeddings`` rows.

Why embed raw text, not ``narrative_summary``
---------------------------------------------
``narrative_summary`` is only populated for ALERT-class posts (~5% of
rows). For the CIB content-similarity signal the question is "do these
authors copy the same message?" — that requires coverage across *all*
posts, not just the rare high-risk ones. Embedding raw text gives full
coverage; the model is multilingual so Portuguese slang + short form
posts are handled gracefully.

Why a separate table from ``silver_narrative_embeddings``
---------------------------------------------------------
RSS embeddings key by ``(content_hash, embedding_model)`` where the
embedded text is the LLM-generated ``narrative_summary`` (2-4
sentences). Social embeddings key by the same grain but the source text
is the raw post ``text`` (a few words to several paragraphs). Keeping
them in separate tables avoids confusion when the same ``content_hash``
appears in both RSS and social (unlikely but possible for reposts).

Inputs
------
JSONL of ``SilverSocialPost`` rows from ``silver_social_posts``.  The
orchestrator (Cloud Scheduler → Cloud Run Job) is expected to
``bq extract`` into GCS just before invoking; the script does not query
BigQuery directly to keep this module dependency-light. Required fields
per row: ``content_hash``, ``text``, ``published_at``, ``region``,
``tenant_id``. Rows with empty ``text`` are silently skipped.

Outputs
-------
JSONL file consumable by ``silver_social_post_embeddings`` (via BQ load
in the orchestrator). One row per ``(content_hash, embedding_model)``
in the input batch.

Local invocation
----------------

::

    poetry run python -m mapear_nlp.graph.run_social_embedding \\
        --posts /tmp/silver_social_posts.jsonl \\
        --out /tmp/social_embeddings.jsonl \\
        --region rn

The Makefile target ``make embed-social-posts`` wires this up with the
defaults read from ``MAPEAR_EMBEDDINGS_*`` settings.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from mapear_infra.config import get_settings

from mapear_nlp.embeddings.cache import EmbeddingCache
from mapear_nlp.embeddings.client import EmbeddingClient, get_embedding_client
from mapear_nlp.embeddings.encoder import CacheAwareEncoder


def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _load_posts(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file of SilverSocialPost rows.

    Filters to rows with non-empty ``text``. Posts with no text have
    nothing to embed and are silently skipped — the CIB graph simply
    will not have a content-similarity signal for their content_hash.
    """
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not row.get("text"):
                continue
            if isinstance(row.get("published_at"), str):
                row["published_at"] = _parse_iso(row["published_at"])
            rows.append(row)
    return rows


def _group_by_day_region(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[date, str | None], list[dict[str, Any]]]:
    groups: dict[tuple[date, str | None], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        published = row.get("published_at")
        if published is None:
            day = datetime.now(UTC).date()
        else:
            day = published.astimezone(UTC).date()
        groups[(day, row.get("region"))].append(row)
    return groups


def _build_encoder(
    *,
    embedding_client: EmbeddingClient | None,
    cache_bucket: str | None,
    cache_prefix: str,
    project_id: str,
    cache_enabled: bool,
) -> CacheAwareEncoder:
    if embedding_client is None:
        embedding_client = get_embedding_client(get_settings().embeddings)
    cache: EmbeddingCache | None = None
    if cache_enabled and cache_bucket:
        cache = EmbeddingCache.build(
            bucket_name=cache_bucket,
            project_id=project_id,
            prefix=cache_prefix,
        )
    return CacheAwareEncoder(client=embedding_client, cache=cache)


def run(
    posts_path: Path,
    out_path: Path,
    *,
    region_filter: str | None,
    pipeline_version: str,
    embedding_client: EmbeddingClient | None = None,
    cache_bucket: str | None = None,
    cache_prefix: str = "social_post_embeddings/",
    project_id: str = "",
    cache_enabled: bool = True,
    embedding_enabled: bool = True,
) -> int:
    """Embed social post text and write silver_social_post_embeddings rows.

    Returns the number of embedding rows written (0 when disabled or no
    eligible posts after filtering).
    """
    if not embedding_enabled:
        sys.stderr.write("social post embedding disabled — exiting 0\n")
        return 0

    rows = _load_posts(posts_path)
    if region_filter is not None:
        rows = [r for r in rows if r.get("region") == region_filter]
    if not rows:
        sys.stderr.write("no posts after filter — nothing to do\n")
        return 0

    encoder = _build_encoder(
        embedding_client=embedding_client,
        cache_bucket=cache_bucket,
        cache_prefix=cache_prefix,
        project_id=project_id,
        cache_enabled=cache_enabled,
    )

    groups = _group_by_day_region(rows)
    job_run_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    embedding_rows: list[dict[str, Any]] = []

    for (day, region), grouped in groups.items():
        # Deduplicate by content_hash within the group — same post can
        # appear multiple times in a lookback window export.
        seen: dict[str, dict[str, Any]] = {}
        for r in grouped:
            h = r.get("content_hash")
            if h and h not in seen:
                seen[h] = r
        deduped = list(seen.values())

        items = [(r["content_hash"], r["text"]) for r in deduped]
        encode_result = encoder.encode_with_hashes(items)
        sys.stderr.write(
            f"group ({day}, {region}): {len(items)} posts "
            f"({encode_result.cache_hits} cache hits, "
            f"{encode_result.encoded} encoded)\n"
        )

        for r, vec in zip(deduped, encode_result.vectors, strict=True):
            embedding_rows.append(
                {
                    "content_hash": r["content_hash"],
                    "embedding_model": encoder.model,
                    "embedding_dim": encoder.dim,
                    "embedding": vec,
                    "job_run_id": job_run_id,
                    "run_at": now.isoformat(),
                    "pipeline_version": pipeline_version,
                    "schema_version": 1,
                    "source_type": "social",
                    "region": region,
                    "tenant_id": r.get("tenant_id"),
                }
            )

    with out_path.open("w", encoding="utf-8") as fh:
        for row in embedding_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    sys.stderr.write(f"emitted {len(embedding_rows)} embedding rows\n")
    return len(embedding_rows)


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Embed social post text for CIB content-similarity (Eixo 2 v2a social)"  # noqa: E501
    )
    parser.add_argument(
        "--posts",
        type=Path,
        required=True,
        help="JSONL with SilverSocialPost rows from silver_social_posts",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output JSONL path for silver_social_post_embeddings rows",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="Filter posts to this region slug only.",
    )
    parser.add_argument("--pipeline-version", default="0.1.0")
    parser.add_argument(
        "--cache-bucket",
        default=settings.gcp.gcs_bucket_name or None,
        help="GCS bucket for the embedding cache. Empty disables the cache.",
    )
    parser.add_argument(
        "--cache-prefix",
        default=settings.embeddings.social_post_cache_gcs_prefix,
    )
    parser.add_argument(
        "--project-id",
        default=settings.gcp.project_id,
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the embedding cache (re-encode every post).",
    )
    args = parser.parse_args()

    run(
        posts_path=args.posts,
        out_path=args.out,
        region_filter=args.region,
        pipeline_version=args.pipeline_version,
        cache_bucket=args.cache_bucket,
        cache_prefix=args.cache_prefix,
        project_id=args.project_id,
        cache_enabled=not args.no_cache and settings.embeddings.cache_enabled,
        embedding_enabled=settings.embeddings.social_post_embedding_enabled,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
