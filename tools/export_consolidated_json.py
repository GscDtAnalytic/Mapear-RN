#!/usr/bin/env python3
"""
Export consolidated JSON from GCS Parquet files (RSS gold + social silver).

Downloads parquet batches from gs://your-gcp-project-data-lake/, consolidates
into a single JSON with metadata, and uploads to GCS exports/.

Sources included:
  - RSS: gold/batch=*/data.parquet
  - Instagram: silver/social/platform=instagram/batch=*/data.parquet
  - Facebook:  silver/social/platform=facebook/batch=*/data.parquet
  - TikTok:    silver/social/platform=tiktok/batch=*/data.parquet
  - X:         silver/social/platform=x/batch=*/data.parquet
"""

import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


BUCKET = "gs://your-gcp-project-data-lake"
TODAY = datetime.now(timezone.utc).strftime("%Y%m%d")
OUTPUT_FILENAME = f"mapear_consolidated_{TODAY}.json"

RSS_GOLD_PATH = f"{BUCKET}/gold/"
SOCIAL_SILVER_BASE = f"{BUCKET}/silver/social"
SOCIAL_PLATFORMS = ("facebook", "instagram", "tiktok", "x")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_OUTPUT = PROJECT_ROOT / "data" / "exports" / OUTPUT_FILENAME
GCS_OUTPUT = f"{BUCKET}/exports/{OUTPUT_FILENAME}"


def gsutil_ls(path: str) -> list[str]:
    result = subprocess.run(
        ["gsutil", "ls", "-r", path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  Warning: gsutil ls failed for {path}: {result.stderr.strip()}")
        return []
    return [
        line.strip()
        for line in result.stdout.strip().split("\n")
        if line.strip().endswith(".parquet")
    ]


def download_parquets(gcs_paths: list[str], local_dir: Path) -> list[Path]:
    local_files = []
    for gcs_path in gcs_paths:
        parts = gcs_path.split("/")
        batch_part = [p for p in parts if p.startswith("batch=")]
        batch_name = batch_part[0] if batch_part else "unknown"
        local_file = local_dir / f"{batch_name}.parquet"

        result = subprocess.run(
            ["gsutil", "cp", gcs_path, str(local_file)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            local_files.append(local_file)
        else:
            print(f"  Warning: failed to download {gcs_path}: {result.stderr.strip()}")
    return local_files


def read_and_concat(local_files: list[Path]) -> pd.DataFrame:
    if not local_files:
        return pd.DataFrame()
    dfs = []
    for f in local_files:
        try:
            df = pd.read_parquet(f)
            dfs.append(df)
        except Exception as e:
            print(f"  Warning: failed to read {f.name}: {e}")
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


def df_to_records(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []

    import numpy as np

    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    records = df.to_dict(orient="records")
    clean_records = []
    for record in records:
        clean = {}
        for k, v in record.items():
            try:
                if isinstance(v, (np.ndarray,)):
                    clean[k] = v.tolist()
                elif isinstance(v, list):
                    clean[k] = v
                elif isinstance(v, (str, bool, dict)):
                    clean[k] = v
                elif v is None or (hasattr(v, "__float__") and pd.isna(v)):
                    clean[k] = None
                elif isinstance(v, (np.integer,)):
                    clean[k] = int(v)
                elif isinstance(v, (np.floating,)):
                    clean[k] = float(v) if not pd.isna(v) else None
                else:
                    clean[k] = v
            except (TypeError, ValueError):
                clean[k] = v
        clean_records.append(clean)

    return clean_records


def build_rss_record(row: dict) -> dict:
    return {
        "title": row.get("title"),
        "url": str(row.get("url", "")),
        "source_feed": row.get("source_feed"),
        "published_at": row.get("published_at"),
        "content_summary": (row.get("content_clean") or "")[:500] or None,
        "entities": row.get("entities") or row.get("mentioned_cities", []),
        "sentiment": (
            {
                "overall": row.get("sentiment_overall"),
                "by_entity": row.get("sentiment_by_entity"),
            }
            if row.get("sentiment_overall") is not None
            else None
        ),
        "topics": row.get("topics", []),
        "rn_relevant": row.get("is_rn_relevant"),
        **{
            k: v
            for k, v in {
                "content_hash": row.get("content_hash"),
                "topic_id": row.get("topic_id"),
                "trend_score": row.get("trend_score"),
                "source_type": row.get("source_type"),
            }.items()
            if v is not None
        },
    }


def build_social_record(row: dict) -> dict:
    return {
        "post_id": row.get("post_id"),
        "platform": row.get("platform"),
        "url": row.get("url"),
        "author_handle": row.get("author_handle"),
        "author_display_name": row.get("author_display_name"),
        "author_verified": row.get("author_verified"),
        "text": row.get("text"),
        "language": row.get("language"),
        "published_at": row.get("published_at"),
        "engagement": {
            "likes": row.get("likes"),
            "comments": row.get("comments"),
            "shares": row.get("shares"),
            "views": row.get("views"),
        },
        "is_repost": row.get("is_repost"),
        "is_reply": row.get("is_reply"),
        "parent_post_id": row.get("parent_post_id"),
        "entities": row.get("entities", []),
        "mentioned_cities": row.get("mentioned_cities", []),
        "mentioned_mayors": row.get("mentioned_mayors", []),
        "mentioned_governors": row.get("mentioned_governors", []),
        "mentioned_parties": row.get("mentioned_parties", []),
        "mentioned_persons": row.get("mentioned_persons", []),
        "rn_relevant": row.get("is_rn_relevant"),
        "sentiment": (
            {
                "overall": row.get("sentiment_overall"),
                "label": row.get("sentiment_label"),
                "by_entity": row.get("sentiment_by_entity"),
                "confidence_score": row.get("confidence_score"),
                "risk_score": row.get("risk_score"),
            }
            if row.get("sentiment_overall") is not None
            else None
        ),
        "person_id": row.get("person_id"),
        "scope_status": row.get("scope_status"),
        "resolution_confidence": row.get("resolution_confidence"),
        "source_type": "social",
        "batch_id": row.get("batch_id"),
    }


def main():
    print(f"=== Mapear-RN Consolidated Export ({TODAY}) ===\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # --- RSS Gold ---
        print("1. Listing RSS gold batches...")
        rss_files = gsutil_ls(RSS_GOLD_PATH)
        rss_batch_count = len(rss_files)
        print(f"   Found {rss_batch_count} RSS gold batch(es)")

        (tmpdir / "rss").mkdir(parents=True, exist_ok=True)
        rss_local = download_parquets(rss_files, tmpdir / "rss")
        df_rss = read_and_concat(rss_local)
        print(f"   RSS records: {len(df_rss)}")

        # --- Social Platforms ---
        social_batch_counts: dict[str, int] = {}
        social_records: dict[str, list[dict]] = {}

        for i, platform in enumerate(SOCIAL_PLATFORMS, start=2):
            print(f"\n{i}. Listing {platform} silver batches...")
            path = f"{SOCIAL_SILVER_BASE}/platform={platform}/"
            files = gsutil_ls(path)
            social_batch_counts[platform] = len(files)
            print(f"   Found {len(files)} {platform} batch(es)")

            if files:
                platform_dir = tmpdir / platform
                platform_dir.mkdir(parents=True, exist_ok=True)
                local_files = download_parquets(files, platform_dir)
                df = read_and_concat(local_files)
                print(f"   {platform} records: {len(df)}")
                records_raw = df_to_records(df)
                social_records[platform] = [build_social_record(r) for r in records_raw]
            else:
                social_records[platform] = []

        # --- Convert RSS to records ---
        print(f"\n{len(SOCIAL_PLATFORMS) + 2}. Converting RSS to JSON records...")
        rss_records_raw = df_to_records(df_rss)
        rss_articles = [build_rss_record(r) for r in rss_records_raw]

        total_social = sum(len(v) for v in social_records.values())

        # --- Build consolidated JSON ---
        consolidated = {
            "metadata": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "rss_batches": rss_batch_count,
                "rss_layer": "gold",
                "total_rss_articles": len(rss_articles),
                "social_batches": social_batch_counts,
                "social_layer": "silver",
                "total_social_posts": total_social,
                "social_by_platform": {p: len(v) for p, v in social_records.items()},
                "total_records": len(rss_articles) + total_social,
            },
            "rss_articles": rss_articles,
            "social_posts": social_records,
        }

        # --- Save locally ---
        step = len(SOCIAL_PLATFORMS) + 3
        print(f"\n{step}. Saving files...")
        LOCAL_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with open(LOCAL_OUTPUT, "w", encoding="utf-8") as f:
            json.dump(consolidated, f, ensure_ascii=False, indent=2, default=str)

        file_size_mb = LOCAL_OUTPUT.stat().st_size / (1024 * 1024)
        print(f"   Local: {LOCAL_OUTPUT} ({file_size_mb:.2f} MB)")

        # --- Upload to GCS ---
        print(f"   Uploading to {GCS_OUTPUT}...")
        result = subprocess.run(
            ["gsutil", "cp", str(LOCAL_OUTPUT), GCS_OUTPUT],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"   GCS: {GCS_OUTPUT}")
        else:
            print(f"   Warning: GCS upload failed: {result.stderr.strip()}")

        # --- Summary ---
        print(f"\n{'='*60}")
        print("RESUMO:")
        print(
            f"  RSS articles:        {len(rss_articles)} ({rss_batch_count} batches gold)"
        )
        for platform in SOCIAL_PLATFORMS:
            count = len(social_records[platform])
            batches = social_batch_counts[platform]
            print(f"  {platform:<20} {count} ({batches} batches silver)")
        print(f"  Total registros:     {len(rss_articles) + total_social}")
        print(f"  Tamanho do arquivo:  {file_size_mb:.2f} MB")
        print(f"  Caminho local:       {LOCAL_OUTPUT}")
        print(f"  Caminho GCS:         {GCS_OUTPUT}")

        # --- Preview ---
        print(f"\n{'='*60}")
        print("PREVIEW — 1 RSS article:")
        print(
            json.dumps(rss_articles[:1], ensure_ascii=False, indent=2, default=str)[
                :2000
            ]
        )

        for platform in SOCIAL_PLATFORMS:
            posts = social_records[platform]
            print(f"\nPREVIEW — 1 {platform} post:")
            print(
                json.dumps(posts[:1], ensure_ascii=False, indent=2, default=str)[:1500]
            )


if __name__ == "__main__":
    main()
