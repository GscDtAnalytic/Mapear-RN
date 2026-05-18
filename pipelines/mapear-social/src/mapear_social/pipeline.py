"""End-to-end orchestration for the mapear-social ETL.

Runs one pass for a single platform (Facebook, Instagram, or X):

    1. Load rn_targets + filter by adapter.targets_with_handle (platform-specific).
    2. Trigger the Apify actor, poll to terminal, fetch dataset items.
    3. parse_item → SocialPost (raises SchemaDriftError on actor shape changes).
    4. Persist raw parquet via typed SOCIAL_RAW_SCHEMA → warehouse MERGE on post_id.
    5. Silver enrichment: NER + GCP sentiment on each post.
    6. PersonResolver.resolve_best — handle first, fall back to NER mentions.
    7. PoliticalSentimentClassifier — FAVORABLE / WARNING / ALERT overlay.
    8. IN_SCOPE gate: only IN_SCOPE rows are persisted to Silver (Gold pool).

Usage:
    python -m mapear_social --platform=facebook
    ENVIRONMENT=local python -m mapear_social --platform=instagram

The pipeline is intentionally platform-scoped (one Cloud Run Job per
platform) so that:

* Per-platform Apify quotas stay isolated — a Facebook outage does not
  take IG / X with it.
* Cloud Scheduler cadences can diverge (``0 */6`` vs ``0 */8`` vs
  ``0 */4``) to match each platform's refresh rhythm.
* Logs/metrics are naturally labelled by ``platform`` with no extra
  bookkeeping.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from loguru import logger

from mapear_domain.entity_resolution import (
    IdentityAuditor,
    IdentityReviewQueue,
    PersonResolver,
    ReviewItem,
    ScopeStatus,
    set_targets_seed_path,
)
from mapear_domain.region import Region, load_region
from mapear_domain.rn_entities import set_seed_path
from mapear_infra.audit import log_llm_call
from mapear_infra.logging import setup_logging
from mapear_infra.metrics import (
    bq_load_failures,
    entity_fill_cities,
    entity_fill_politicians,
    language_detected_total,
    language_fill_pct,
    mayor_author_base_city_fill_pct,
    ner_person_noise_rate,
    social_errors_total,
    social_filtered_total,
    social_pipeline_latency,
    social_scraped_total,
    social_stored_total,
    start_metrics_server,
)
from mapear_infra.privacy import parse_level
from mapear_infra.tracing import setup_tracing
from mapear_infra.watermark import WatermarkManager
from mapear_nlp.language_detector import LanguageDetection, detect_and_normalize
from mapear_nlp.llm.client import LLMError, get_llm_client
from mapear_nlp.matchers.region_matcher import RegionMatcher
from mapear_nlp.narrative_cache import NarrativeCache
from mapear_nlp.narrative_explainer import NarrativeExplainer
from mapear_nlp.ner import NERExtractor
from mapear_nlp.political_sentiment import PoliticalSentimentClassifier
from mapear_nlp.sentiment import SentimentAnalyzer
from mapear_nlp.shadow import build_shadow_scorer
from mapear_social import __version__ as PIPELINE_VERSION  # noqa: N812
from mapear_social.adapters import SchemaDriftError, XAdapter, get_adapter
from mapear_social.adapters.base import PlatformAdapter
from mapear_social.apify_client import ApifyClient, ApifyError
from mapear_social.coactivation import build_activation_records
from mapear_social.config import SocialSettings, get_social_settings
from mapear_social.models import SocialPost
from mapear_social.parquet_schemas import (
    SOCIAL_AUTHOR_ACTIVATIONS_SCHEMA,
    SOCIAL_DLQ_SCHEMA,
    SOCIAL_RAW_SCHEMA,
    SOCIAL_SILVER_SCHEMA,
)
from mapear_storage.loaders.factory import get_storage_writer, get_warehouse_loader
from mapear_storage.loaders.parquet_writer import (
    EVENT_SHADOW_SCHEMA,
    records_to_dataframe,
    write_dataframe_as_parquet,
)

ELECTORAL_CUTOFF_DATE = date(2025, 1, 1)

# X-pipeline: if ≥50% of handles error with HTTP 401/403, abort the run
# instead of advancing the watermark. Tolerates a minority of suspended /
# locked accounts while catching the systemic failure mode (token revoked
# for the whole bearer token), as described in issues #41 and #42.
X_AUTH_FAIL_THRESHOLD = 0.5

_PAYLOAD_LIST_KEYS: tuple[str, ...] = (
    "startUrls",
    "usernames",
    "twitterHandles",
    "profiles",
)


def _stamp_tenant_id(df, tenant_id):  # type: ignore[no-untyped-def]
    """Overwrite the ``tenant_id`` column with the active tenant.

    Stage 2B v1 data plane: every row written from this pipeline gets
    stamped with ``settings.mapear_tenant_id`` right before the parquet
    write. The Pydantic models default ``tenant_id`` to None, so this is
    where the actual value lands. No-op when the env var is unset
    (single-tenant deployment / legacy).
    """
    if tenant_id is None:
        return df
    df = df.copy()
    df["tenant_id"] = tenant_id
    return df


def _guard_nonempty_payload(
    adapter: PlatformAdapter,
    payload: dict[str, Any],
    platform: str,
    bound_logger: Any,
) -> None:
    """Exit before calling Apify if the payload contains no scrape targets.

    Protects against edge cases where all handles from rn_targets.csv are
    blank or normalize to empty strings, which would result in a pointless
    (or erroring) Apify call that burns quota and produces no data.
    """
    has_targets = any(bool(payload.get(k)) for k in _PAYLOAD_LIST_KEYS)
    if not has_targets:
        bound_logger.error(
            "Apify payload has no scrape targets for {platform}: all target lists "
            "({keys}) are empty. Confirm rn_targets.csv has valid handles and rebuild "
            "the image. Exiting without calling Apify.",
            platform=platform,
            keys=", ".join(_PAYLOAD_LIST_KEYS),
        )
        raise SystemExit(5)


def _resolve_platform(settings: SocialSettings, cli_platform: str | None) -> str:
    """CLI flag wins over env var — matches the RSS ``--dry-run`` pattern."""
    platform = cli_platform or os.environ.get("SOCIAL_PLATFORM") or settings.platform
    if platform not in ("facebook", "instagram", "x", "tiktok"):
        raise SystemExit(f"Unsupported platform: {platform!r}")
    return platform


def _point_seeds_at_dbt() -> None:
    """Point the region/targets seed loaders at the shared dbt/seeds directory.

    Same hunt order as the RSS pipeline — relative paths
    first (dev / CI), then the docker working directory. We do not fail
    loud when a path is missing because each seed has its own fallback
    and the pipeline surfaces the downstream error (empty targets list).
    """
    rn_seeds = [
        Path("../dbt/seeds/rn_cities_mayors.csv"),
        Path("dbt/seeds/rn_cities_mayors.csv"),
    ]
    for seed in rn_seeds:
        if seed.exists():
            set_seed_path(seed)
            break

    target_seeds = [
        Path("../dbt/seeds/rn_targets.csv"),
        Path("dbt/seeds/rn_targets.csv"),
    ]
    for seed in target_seeds:
        if seed.exists():
            set_targets_seed_path(seed)
            break


def _build_entity_list(ner_result: dict) -> list[tuple[str, str]]:
    """Flatten NER output into the (name, type) tuples SentimentAnalyzer expects."""
    entities: list[tuple[str, str]] = []
    for city in ner_result.get("mentioned_cities", []):
        entities.append((city, "city"))
    for mayor in ner_result.get("mentioned_mayors", []):
        entities.append((mayor, "mayor"))
    for governor in ner_result.get("mentioned_governors", []):
        entities.append((governor, "governor"))
    for party in ner_result.get("mentioned_parties", []):
        entities.append((party, "party"))
    return entities


def _merge_unique(existing: list[str], new: list[str]) -> tuple[list[str], int]:
    """Append items from new that are not already in existing (case-insensitive).

    Preserves insertion order: NER entities come first, matcher additions follow.
    Mirrors the same helper in Mapear-RSS/pipeline.py — keep both in sync.
    """
    seen = {x.lower() for x in existing}
    added = [x for x in new if x.lower() not in seen]
    return existing + added, len(added)


def _enrich_with_region_matcher(
    ner_result: dict,
    text: str,
    platform: str,
    author_handle: str | None,
    region: Region,
    matcher: RegionMatcher,
) -> tuple[dict, dict[str, int]]:
    """Merge RegionMatcher text matching + handle-based resolution into ner_result.

    Three enrichment paths, applied in order:
      1. NER (already in ner_result — counted, not re-run).
      2. RegionMatcher text scan — catches aliases, short names, accented variants.
      3. Handle resolution — when author handle maps to a monitored politician,
         adds that politician + their city even if the name doesn't appear in text
         (resolves M3/M4/M9/M10 class of xfails from the golden set).

    Deduplication is case-insensitive across all three paths so that NER output
    of "natal" and RegionMatcher output of "Natal" do not produce two entries.
    Order is preserved: NER → matcher → handle.

    Returns:
        (enriched_ner_result, counts) where counts = {"ner": int, "matcher": int,
        "handle": int} — number of *new* entity-mentions each source contributed.
        Useful for batch-level logging to diagnose source mix in production.

    No field is ever removed from ner_result; only additive merges occur.
    """
    ner_cities: list[str] = list(ner_result.get("mentioned_cities") or [])
    ner_mayors: list[str] = list(ner_result.get("mentioned_mayors") or [])
    ner_governors: list[str] = list(ner_result.get("mentioned_governors") or [])
    ner_parties: list[str] = list(ner_result.get("mentioned_parties") or [])
    # NER genérico não distingue governor_candidate/senator/deputy_federal/vice_governor
    # como categorias separadas — só o RegionMatcher produz esses campos. Inicializa
    # vazio para que o merge abaixo seja simétrico com os outros entity types.
    ner_candidates: list[str] = list(ner_result.get("mentioned_candidates") or [])
    ner_politicians: list[str] = list(ner_result.get("mentioned_politicians") or [])
    ner_count = (
        len(ner_cities)
        + len(ner_mayors)
        + len(ner_governors)
        + len(ner_parties)
        + len(ner_candidates)
        + len(ner_politicians)
    )

    # Path 2: RegionMatcher text scan — case-insensitive merge preserves NER order
    match_result = matcher.match(text)
    cities, c = _merge_unique(ner_cities, match_result.mentioned_cities)
    mayors, m = _merge_unique(ner_mayors, match_result.mentioned_mayors)
    governors, g = _merge_unique(ner_governors, match_result.mentioned_governors)
    parties, p = _merge_unique(ner_parties, match_result.mentioned_parties)
    candidates, cd = _merge_unique(ner_candidates, match_result.mentioned_candidates)
    politicians, pl = _merge_unique(ner_politicians, match_result.mentioned_politicians)
    matcher_count = c + m + g + p + cd + pl

    # Path 3: handle-based author resolution (social only — RSS has no author handle)
    handle_count = 0
    if author_handle:
        politician = region.get_politician_by_handle(platform, author_handle)
        if politician:
            if politician.role == "mayor":
                mayors, added = _merge_unique(mayors, [politician.name])
                handle_count += added
                if politician.city:
                    cities, added = _merge_unique(cities, [politician.city])
                    handle_count += added
            elif politician.role == "governor":
                governors, added = _merge_unique(governors, [politician.name])
                handle_count += added

    enriched = dict(ner_result)
    enriched["mentioned_cities"] = cities
    enriched["mentioned_mayors"] = mayors
    enriched["mentioned_governors"] = governors
    enriched["mentioned_parties"] = parties
    enriched["mentioned_candidates"] = candidates
    enriched["mentioned_politicians"] = politicians

    return enriched, {
        "ner": ner_count,
        "matcher": matcher_count,
        "handle": handle_count,
    }


def _volume_by_person(silver_rows: list[dict]) -> dict[str, int]:
    """Aggregate mentions per person in the current batch.

    Used by the political classifier as a cheap stand-in for the 24h
    rolling volume until the scheduled query is wired up (BL-28). The
    approximation is fine for shadow mode because the point is to see
    *relative* signal across the batch.
    """
    counts: dict[str, int] = {}
    for row in silver_rows:
        pid = row.get("person_id")
        if pid:
            counts[pid] = counts.get(pid, 0) + 1
    return counts


def _classifier_inputs(
    row: dict, volume_map: dict[str, int]
) -> tuple[float, int, float, int]:
    """Derive the four numeric classifier inputs from a silver row.

    Single source of truth for the Stage 5 classification loop *and* the
    Stage 5.5b shadow path — keeping them in one place means the shadow
    regime never drifts from the primary regime's view of the inputs.

    Returns ``(polarity, volume_24h, velocity, engagement)``.
    """
    person_id = row["person_id"]
    volume_24h = volume_map.get(person_id, 0) if person_id else 0
    engagement = (
        (row.get("likes") or 0) + (row.get("comments") or 0) + (row.get("shares") or 0)
    )
    polarity = row.get("sentiment_overall") or 0.0
    # Velocity stub — TrendScorer lives off cross-batch aggregates (BL-28).
    # Crude in-batch proxy: volume_share clamped to [0, 1].
    velocity = min(volume_24h / 10.0, 1.0) if volume_24h else 0.0
    return float(polarity), int(volume_24h), float(velocity), int(engagement)


def _apply_shadow_to_silver_rows(
    silver_rows: list[dict],
    volume_map: dict[str, int],
    shadow_scorer,  # noqa: ANN001  - ShadowScorer | None
) -> list:
    """Stage 5.5b: produce SilverEventShadow rows for already-classified posts.

    Operates on the dicts after Stage 5 stamped ``sentiment_label`` etc.
    The primary :class:`ClassificationResult` is reconstructed from those
    stamped fields — no second primary-classifier call. Returns an empty
    list when ``shadow_scorer`` is None.
    """
    if shadow_scorer is None or not silver_rows:
        return []

    from mapear_nlp.political_sentiment import ClassificationResult

    shadow_rows = []
    for row in silver_rows:
        polarity, volume_24h, velocity, engagement = _classifier_inputs(row, volume_map)
        primary = ClassificationResult(
            label=row["sentiment_label"],
            confidence=row["confidence_score"],
            risk_score=row["risk_score"],
            rule_version=row["rule_version"],
            model_version=row["model_version"],
        )
        shadow_rows.append(
            shadow_scorer.score(
                content_hash=row["content_hash"],
                polarity=polarity,
                volume_24h=volume_24h,
                velocity=velocity,
                engagement=engagement,
                primary=primary,
                person_id=row.get("person_id"),
                ingestion_run_id=row.get("ingestion_run_id"),
            )
        )
    return shadow_rows


async def _fetch_items(
    token: str,
    actor_id: str,
    payload: dict[str, Any],
    poll_timeout: int,
    page_size: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Run an Apify actor and return ``(actor_run_id, items)``.

    Wraps ApifyClient's async context manager so the caller can stay sync.
    """
    async with ApifyClient(
        token=token,
        poll_timeout_seconds=poll_timeout,
        dataset_page_size=page_size,
    ) as client:
        run, items = await client.run_actor(actor_id, payload)
        run_id = run_id = getattr(run, "run_id", None) or getattr(run, "id", None)
        if not run_id:
            raise ApifyError(
                "ActorRun returned empty run_id — cannot correlate records"
            )
        return run_id, items


def _parse_social_posts(
    adapter: PlatformAdapter,
    items: list[dict[str, Any]],
    actor_run_id: str,
    ingestion_run_id: str,
) -> tuple[list[SocialPost], list[dict]]:
    """Best-effort parse: schema drift on a single item doesn't kill the run.

    Every failure lands in a DLQ-bound payload list that the pipeline
    persists alongside raw. Same pattern as YT transcript failures.
    """
    parsed: list[SocialPost] = []
    dlq: list[dict] = []
    for raw in items:
        # Catch-all for sparse sentinel items before calling parse_item.
        # Any item with ≤2 top-level keys can never be a valid social post —
        # it is always a sentinel/error response from the actor (e.g. {"noResults":
        # true}, {"error": "..."}).  Classify immediately so the fatal schema-drift
        # path is not triggered by unrecognised actor sentinels.
        if len(raw) <= 2:
            keys = sorted(raw.keys())
            if "error" in raw or "errorDescription" in raw:
                detail = raw.get("errorDescription") or raw.get("error") or ""
                err_msg = f"non_post_item: actor error — {detail!r} (keys: {keys!r})"
            else:
                err_msg = f"non_post_item: sparse sentinel ({len(raw)} keys: {keys!r})"
            dlq.append(
                {
                    "platform": adapter.platform,
                    "actor_run_id": actor_run_id,
                    "ingestion_run_id": ingestion_run_id,
                    "reason": "non_post_item",
                    "error": err_msg,
                    "raw_keys": keys,
                    "raw": raw,
                    "captured_at": datetime.now(UTC).isoformat(),
                }
            )
            logger.warning(
                "Non-post sentinel skipped ({platform}): {err}",
                platform=adapter.platform,
                err=err_msg,
            )
            continue
        try:
            parsed.append(
                adapter.parse_item(
                    raw,
                    actor_run_id=actor_run_id,
                    ingestion_run_id=ingestion_run_id,
                )
            )
        except SchemaDriftError as exc:
            err_str = str(exc)
            if err_str.startswith("page_unavailable:"):
                reason = "page_unavailable"
            elif err_str.startswith("non_post_item:"):
                reason = "non_post_item"
            else:
                reason = "schema_drift"
            dlq.append(
                {
                    "platform": adapter.platform,
                    "actor_run_id": actor_run_id,
                    "ingestion_run_id": ingestion_run_id,
                    "reason": reason,
                    "error": err_str,
                    "raw_keys": sorted(raw.keys()),
                    "raw": raw,
                    "captured_at": datetime.now(UTC).isoformat(),
                }
            )
            level = (
                "warning"
                if reason in ("non_post_item", "page_unavailable")
                else "error"
            )
            getattr(logger, level)(
                "schema drift on {platform} item [{reason}]: {err}",
                platform=adapter.platform,
                reason=reason,
                err=err_str,
            )
        except Exception as exc:  # noqa: BLE001 — we want every parse failure in DLQ
            dlq.append(
                {
                    "platform": adapter.platform,
                    "actor_run_id": actor_run_id,
                    "ingestion_run_id": ingestion_run_id,
                    "reason": "parse_error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "raw": raw,
                    "captured_at": datetime.now(UTC).isoformat(),
                }
            )
            logger.exception(
                "parse_item crashed for {platform}: {err}",
                platform=adapter.platform,
                err=str(exc),
            )
    return parsed, dlq


def _dedup_intra_batch(posts: list[SocialPost]) -> list[SocialPost]:
    """Deduplicate by post_id within the batch.

    Apify pagination can repeat items if the dataset grows between
    pages; upstream MERGE handles cross-run dedup but intra-batch
    duplicates would break MERGE (``duplicate keys in source``).
    """
    seen: set[str] = set()
    unique: list[SocialPost] = []
    for p in posts:
        if p.post_id in seen:
            continue
        seen.add(p.post_id)
        unique.append(p)
    return unique


def _build_silver_row(
    post: SocialPost,
    ner_result: dict,
    sentiment: dict,
    resolution: Any,
    classification: Any,
    batch_id: str,
    lang_detection: LanguageDetection | None = None,
    author_base_city: str | None = None,
    is_backfill: bool = False,
    effective_cutoff_date: datetime | None = None,
) -> dict:
    """Flatten SocialPost + enrichment into the SOCIAL_SILVER_SCHEMA shape."""
    engagement = post.engagement
    _lang = lang_detection or LanguageDetection(
        language=None, confidence=None, reason="not_provided"
    )
    return {
        "post_id": post.post_id,
        "platform": post.platform,
        "url": str(post.url),
        "author_handle": post.account.handle,
        "author_display_name": post.author_display_name or post.account.display_name,
        "author_verified": post.account.verified,
        "text": post.text,
        "language": _lang.language,
        "language_confidence": _lang.confidence,
        "language_reason": _lang.reason,
        "published_at": post.published_at,
        "extracted_at": post.extracted_at,
        "likes": engagement.likes,
        "comments": engagement.comments,
        "shares": engagement.shares,
        "views": engagement.views,
        "is_repost": post.is_repost,
        "is_reply": post.is_reply,
        "parent_post_id": post.parent_post_id,
        "entities": ner_result.get("entities", []),
        "mentioned_cities": ner_result.get("mentioned_cities", []),
        "mentioned_mayors": ner_result.get("mentioned_mayors", []),
        "mentioned_governors": ner_result.get("mentioned_governors", []),
        "mentioned_parties": ner_result.get("mentioned_parties", []),
        "mentioned_candidates": ner_result.get("mentioned_candidates", []),
        "mentioned_politicians": ner_result.get("mentioned_politicians", []),
        "mentioned_persons": ner_result.get("mentioned_persons", []),
        "is_rn_relevant": ner_result.get("is_rn_relevant", False),
        "sentiment_overall": sentiment.get("sentiment_overall"),
        "sentiment_by_entity": sentiment.get("sentiment_by_entity", []),
        "person_id": resolution.person_id,
        "scope_status": resolution.scope_status.value,
        "resolution_confidence": resolution.confidence,
        "sentiment_label": classification.label if classification else None,
        "confidence_score": classification.confidence if classification else None,
        "risk_score": classification.risk_score if classification else None,
        "decision_factors": (
            classification.factors_as_dicts() if classification else []
        ),
        "content_hash": post.content_hash,
        "actor_run_id": post.actor_run_id,
        "ingestion_run_id": post.ingestion_run_id,
        "rule_version": classification.rule_version if classification else None,
        "model_version": classification.model_version if classification else None,
        "pipeline_version": PIPELINE_VERSION,
        "source_type": "social",
        "batch_id": batch_id,
        "author_base_city": author_base_city,
        "data_type": "backfill" if is_backfill else "incremental",
        "effective_cutoff_date": effective_cutoff_date,
        "identity_resolution_version": resolution.identity_resolution_version,
        # V1 canonical computed fields — derived here because _build_silver_row
        # returns a plain dict; the @computed_field on SilverSocialPost is never
        # evaluated, so dataframe_to_table would fill both columns with NULL.
        "content_rn_relevant": bool(ner_result.get("is_rn_relevant", False)),
        "author_in_scope": resolution.scope_status.value == "IN_SCOPE",
        # Eixo 2 v1 — filled for ALERT rows by _apply_social_narrative_explainer.
        "narrative_summary": None,
        "narrative_prompt_version": None,
    }


def _build_social_narrative_explainer(
    settings,
) -> NarrativeExplainer | None:  # noqa: ANN001
    """Return a configured NarrativeExplainer or None when LLM is unconfigured.

    Eixo 2 v1 for the social pipeline — mirrors the RSS _build_narrative_explainer.
    Opt-in: pipelines run unchanged when no API key is present (CI / local).
    """
    cfg = settings.llm
    if not cfg.api_key and not cfg.api_key_secret:
        logger.info(
            "LLM provider unconfigured (no MAPEAR_LLM_API_KEY or _SECRET) — "
            "social narrative explainer disabled"
        )
        return None
    try:
        client = get_llm_client(cfg)
    except LLMError as exc:
        logger.warning(
            "Could not build LLM client, social narrative explainer disabled: {err}",
            err=exc,
        )
        return None
    cache: NarrativeCache | None = None
    if cfg.cache_enabled and settings.gcp.gcs_bucket_name and settings.gcp.project_id:
        try:
            cache = NarrativeCache.build(
                bucket_name=settings.gcp.gcs_bucket_name,
                project_id=settings.gcp.project_id,
                prefix=cfg.cache_gcs_prefix,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not build narrative cache, running uncached: {err}", err=exc
            )
    redaction_level = parse_level(cfg.pii_level)
    hmac_key = cfg.pii_hmac_key.encode("utf-8") if cfg.pii_hmac_key else None
    return NarrativeExplainer(
        llm_client=client,
        cache=cache,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
        timeout_seconds=cfg.timeout_seconds,
        redaction_level=redaction_level,
        hmac_key=hmac_key,
    )


def _apply_social_narrative_explainer(
    silver_rows: list[dict],
    explainer: NarrativeExplainer | None,
    *,
    tenant_id: str | None,
    region_id: str,
    provider: str,
    model: str,
    coverage: str = "alert",
) -> None:
    """Stamp narrative_summary on rows in-place + emit audit log.

    Eixo 2 v1 for social. Mirrors the RSS _apply_narrative_explainer but
    operates on flat silver_rows dicts instead of GoldArticle objects.
    Social posts have no title, so title="" is passed (renders as "(no title)").
    With ``coverage="all"`` the ALERT gate is lifted — positive and neutral
    posts also get an LLM summary (Eixo 2 v2d).
    """
    if explainer is None or not silver_rows:
        return
    alert_count = 0
    explained = 0
    for row in silver_rows:
        if coverage != "all" and row.get("sentiment_label") != "ALERT":
            continue
        alert_count += 1
        person_name = (
            row["mentioned_persons"][0]
            if row.get("mentioned_persons")
            else "(não identificado)"
        )
        velocity = next(
            (
                f.get("value", 0.0)
                for f in (row.get("decision_factors") or [])
                if f.get("name") == "velocity"
            ),
            0.0,
        )
        volume = next(
            (
                f.get("value", 0)
                for f in (row.get("decision_factors") or [])
                if f.get("name") == "volume"
            ),
            0,
        )
        result = explainer.explain(
            content_hash=row["content_hash"],
            title="",
            content=row.get("text") or "",
            person_name=person_name,
            person_role="",
            polarity=float(row.get("sentiment_overall") or 0.0),
            velocity=float(velocity),
            volume=int(volume),
            decision_factors=row.get("decision_factors") or [],
            rule_version=row.get("rule_version") or "",
        )
        if result.summary:
            row["narrative_summary"] = result.summary
            row["narrative_prompt_version"] = result.prompt_version
            explained += 1
        if result.cache_hit:
            audit_status = "cache_hit"
        elif result.error:
            audit_status = "cache_miss_error"
        else:
            audit_status = "cache_miss_ok"
        log_llm_call(
            tenant_id=tenant_id,
            region=region_id,
            content_hash=row["content_hash"],
            prompt_version=result.prompt_version,
            provider=provider,
            model=model,
            redaction_level=result.redaction_level,
            redaction_counts=result.redaction_counts or {},
            status=audit_status,
            error=result.error,
        )
    logger.info(
        "Social narrative explainer: {explained}/{alerts} ALERT rows summarised",
        explained=explained,
        alerts=alert_count,
    )


def _dlq_entries_to_records(dlq: list[dict], actor_id: str) -> list[dict]:
    """Convert DLQ entries to the SOCIAL_DLQ_SCHEMA record shape.

    Adds ``actor_id`` (known at the pipeline level, not inside the parser)
    and serialises ``raw`` to JSON so the full payload is queryable in BQ.
    """
    import json

    records = []
    for entry in dlq:
        raw = entry.get("raw") or {}
        raw_keys = entry.get("raw_keys") or sorted(raw.keys())
        records.append(
            {
                "ingestion_run_id": entry["ingestion_run_id"],
                "platform": entry["platform"],
                "actor_id": actor_id,
                "actor_run_id": entry["actor_run_id"],
                "error_type": entry["reason"],
                "error_message": entry["error"],
                "raw_payload_json": json.dumps(raw, ensure_ascii=False, default=str),
                "raw_keys_json": json.dumps(raw_keys),
                "created_at": datetime.fromisoformat(entry["captured_at"]),
            }
        )
    return records


def run_pipeline(
    cli_platform: str | None = None,
    backfill_since: datetime | None = None,
    mode: str = "incremental",
    cutoff_date: date | None = None,
    lookback_days: int | None = None,
    _metrics: dict | None = None,
) -> None:
    """Execute one pass of the mapear-social ETL for a single platform.

    ``_metrics`` is an optional mutable dict populated with operational
    counters (scraped, filtered, stored, errors) for the caller (e.g. the
    parallel orchestrator) to consume after the run. Keys are initialised
    to 0 on entry so callers can safely read them even after early exits.
    """
    if _metrics is not None:
        _metrics.setdefault("scraped", 0)
        _metrics.setdefault("filtered", 0)
        _metrics.setdefault("stored", 0)
        _metrics.setdefault("errors", 0)
    setup_logging()
    start_metrics_server()
    setup_tracing(service_name="mapear-social")
    _point_seeds_at_dbt()

    settings = get_social_settings()
    region = load_region(settings.mapear_region)
    platform = _resolve_platform(settings, cli_platform)
    adapter = get_adapter(platform)

    # --- Temporal governance setup ---
    run_started_at = datetime.now(UTC)
    is_backfill = (mode == "backfill") or (backfill_since is not None)
    watermark_manager = WatermarkManager(platform)

    _resolved_cutoff = cutoff_date or ELECTORAL_CUTOFF_DATE
    _cutoff_dt = datetime(
        _resolved_cutoff.year, _resolved_cutoff.month, _resolved_cutoff.day, tzinfo=UTC
    )
    _wm: datetime | None = (
        watermark_manager.get_watermark() if not is_backfill else None
    )

    if is_backfill:
        effective_cutoff = backfill_since if backfill_since is not None else _cutoff_dt
    elif lookback_days is not None:
        effective_cutoff = max(
            run_started_at - timedelta(days=lookback_days), _cutoff_dt
        )
    elif _wm is not None:
        effective_cutoff = max(_wm, _cutoff_dt)
    else:
        effective_cutoff = _cutoff_dt

    min_published_at = effective_cutoff

    def _save_wm() -> None:
        if not is_backfill:
            watermark_manager.save_watermark(run_started_at)

    ingestion_run_id = f"ing-{platform}-{uuid.uuid4().hex[:12]}"
    batch_id = run_started_at.strftime("%Y%m%d_%H%M%S")
    bound_logger = logger.bind(
        platform=platform,
        ingestion_run_id=ingestion_run_id,
        batch_id=batch_id,
    )
    apify_token_hint = (
        (settings.apify.token[:4] + "****") if settings.apify.token else "MISSING"
    )
    # `apify_token` é explícito por plataforma: FB/IG/TikTok usam Apify;
    # X usa Bearer Token nativo (X_BEARER_TOKEN, não Apify) — `MISSING` é
    # o estado normal e esperado para X. Ver TDT-X-PIPELINE-APIFY-LOG.
    bound_logger.info(
        "Starting mapear-social pipeline"
        " env={env} actor_id={actor_id} apify_token={apify_token_hint}",
        env=settings.environment.value,
        actor_id=adapter.actor_id,
        apify_token_hint=apify_token_hint,
    )
    if is_backfill:
        bound_logger.info(
            "Backfill mode for {platform}: collecting posts since {since} "
            "(watermark will NOT be updated)",
            platform=platform,
            since=effective_cutoff.isoformat(),
        )
    else:
        bound_logger.info(
            "Incremental mode for {platform}: effective_cutoff={cutoff} "
            "(watermark={wm}, cutoff_floor={floor}, lookback_days={lb})",
            platform=platform,
            cutoff=effective_cutoff.isoformat(),
            wm=_wm.isoformat() if _wm else None,
            floor=_cutoff_dt.isoformat(),
            lb=lookback_days,
        )

    resolver = PersonResolver(region=region)
    targets = resolver.list_targets()
    platform_targets = adapter.targets_with_handle(targets)
    dropped = len(targets) - len(platform_targets)
    bound_logger.info(
        "Loaded {n} targets, {kept} with a {platform} handle (dropped {dropped})",
        n=len(targets),
        kept=len(platform_targets),
        platform=platform,
        dropped=dropped,
    )
    if not platform_targets:
        _handle_col = {
            "facebook": "facebook_page",
            "instagram": "instagram_username",
            "x": "x_handle",
            "tiktok": "tiktok_handle",
        }[platform]
        bound_logger.warning(
            "No {platform} targets — all {total} seed rows have an empty "
            "{handle_col!r} column in rn_targets.csv. "
            "Populate handles and rebuild the image.",
            platform=platform,
            total=len(targets),
            handle_col=_handle_col,
        )
        return

    # ---- Stage 1: fetch posts ----
    # X platform uses the native API v2 (Apify blocked 2026-04-22).
    # All other platforms (FB / IG / TikTok) continue via Apify.
    if platform == "x":
        x_adapter = cast(XAdapter, adapter)
        if not x_adapter.has_bearer_token:
            bound_logger.error(
                "X_BEARER_TOKEN is not set — cannot call X API v2. "
                "Set the secret in Secret Manager (x-bearer-token) or export "
                "X_BEARER_TOKEN locally."
            )
            raise SystemExit(2)
        actor_run_id = f"x-api-{uuid.uuid4().hex[:12]}"
        bound_logger = bound_logger.bind(actor_run_id=actor_run_id)
        bound_logger.info("=== Stage 1: X API v2 (native, no Apify) ===")
        posts, dlq, x_stats = x_adapter.fetch_posts_via_api(
            platform_targets,
            ingestion_run_id=ingestion_run_id,
            actor_run_id=actor_run_id,
            start_time=min_published_at,
        )
        bound_logger.info(
            "X API v2 stats — handles_attempted={att} successful_calls={ok} "
            "auth_failures={auth} api_errors={api} users_not_found={nf}",
            att=x_stats["handles_attempted"],
            ok=x_stats["successful_calls"],
            auth=x_stats["auth_failures"],
            api=x_stats["api_errors"],
            nf=x_stats["users_not_found"],
        )

        # Issue #41/#42 — abort when token is revoked instead of masking
        # the failure as a clean run with zero posts. Threshold guards
        # against single suspended accounts tripping the alarm.
        attempted = x_stats["handles_attempted"]
        auth_fail_ratio = x_stats["auth_failures"] / attempted if attempted > 0 else 0.0
        if attempted > 0 and auth_fail_ratio >= X_AUTH_FAIL_THRESHOLD:
            bound_logger.error(
                "X API auth failure on {n}/{total} handles ({pct:.0%}) — "
                "X_BEARER_TOKEN likely expired or revoked. Aborting run "
                "without advancing watermark so the next run re-processes "
                "this window after the token is rotated.",
                n=x_stats["auth_failures"],
                total=attempted,
                pct=auth_fail_ratio,
            )
            if _metrics is not None:
                _metrics["errors"] += len(dlq)
            raise SystemExit(6)

        posts = _dedup_intra_batch(posts)
        # Client-side cutoff safety net (server-side start_time already filtered)
        if min_published_at is not None:
            before = len(posts)
            posts = [p for p in posts if p.published_at >= min_published_at]
            dropped_wm = before - len(posts)
            if dropped_wm > 0:
                bound_logger.info(
                    "Cutoff filter (X): dropped {n} posts older than {cutoff}",
                    n=dropped_wm,
                    cutoff=min_published_at.isoformat(),
                )
            if _metrics is not None:
                _metrics["filtered"] += dropped_wm
        if _metrics is not None:
            _metrics["scraped"] = len(posts)
            _metrics["errors"] += len(dlq)
        bound_logger.info(
            "X API v2 returned {ok} posts, {drift} dropped to DLQ",
            ok=len(posts),
            drift=len(dlq),
        )
        if not posts:
            # Watermark gating (#42): only advance when the API answered
            # successfully on at least one handle AND no handle errored.
            # An empty run with any api/auth error is *not* a legitimate
            # empty — next run must reprocess this window.
            had_errors = x_stats["auth_failures"] > 0 or x_stats["api_errors"] > 0
            legitimate_empty = x_stats["successful_calls"] > 0 and not had_errors
            if legitimate_empty:
                _save_wm()
                bound_logger.info(
                    "X API: 0 posts after watermark filter (legitimate empty"
                    " — {ok} handles answered 200 with no new posts) — "
                    "watermark advanced.",
                    ok=x_stats["successful_calls"],
                )
            else:
                bound_logger.warning(
                    "X API: 0 posts AND errors detected (auth={auth} "
                    "api={api}) — watermark NOT advanced; next run will "
                    "re-process this window.",
                    auth=x_stats["auth_failures"],
                    api=x_stats["api_errors"],
                )
            return
    else:
        if not settings.apify.token:
            bound_logger.error(
                "APIFY_TOKEN is empty — cannot trigger actor. "
                "Set the secret in Secret Manager (apify-token) or export APIFY_TOKEN "
                "locally."
            )
            raise SystemExit(2)

        # Push the temporal cutoff into the actor input so Apify returns
        # only posts newer than ``since`` — the filter that actually cuts
        # billing (Apify charges per item scraped, not per item kept).
        # A 6h buffer before ``min_published_at`` absorbs clock skew and
        # edge posts right at the watermark; the client-side cutoff at
        # ``pipeline.py:576`` still drops anything older than
        # ``min_published_at`` before persisting.
        apify_since = (
            min_published_at - timedelta(hours=6)
            if min_published_at is not None
            else None
        )
        payload = adapter.build_input(platform_targets, since=apify_since)

        _guard_nonempty_payload(adapter, payload, platform, bound_logger)

        # Log the full payload sanitized — token lives in the HTTP header, not here.
        _sanitized_payload = {
            k: (
                v[:5] + [f"… +{len(v) - 5} more"]
                if isinstance(v, list) and len(v) > 5
                else v
            )
            for k, v in payload.items()
        }
        bound_logger.info(
            "Apify payload (sanitized) for {platform}: {payload}",
            platform=platform,
            payload=_sanitized_payload,
        )

        bound_logger.info(
            "=== Stage 1: Apify actor {actor} ===", actor=adapter.actor_id
        )
        try:
            actor_run_id, items = asyncio.run(
                _fetch_items(
                    token=settings.apify.token,
                    actor_id=adapter.actor_id,
                    payload=payload,
                    poll_timeout=settings.apify.poll_timeout_seconds,
                    page_size=settings.apify.dataset_page_size,
                )
            )
        except ApifyError as exc:
            # status_code populado em ActorPayloadError (4xx HTTP); None nos
            # demais (timeout, rate-limit, run failure). Alert A-06 filtra
            # `extra.status_code=401` — TDT-SOCIAL-PIPELINE-STRUCTURED-LOGGING.
            bound_logger.error(
                "Apify run failed: {err}",
                err=str(exc),
                status_code=exc.status_code,
            )
            raise SystemExit(3) from exc

        bound_logger = bound_logger.bind(actor_run_id=actor_run_id)
        bound_logger.info(
            "Apify actor returned {count} items (actor_run_id={run})",
            count=len(items),
            run=actor_run_id,
        )
        if not items:
            _save_wm()
            bound_logger.info("Actor returned 0 items — nothing to persist.")
            return

        # Expand container-level items (e.g. instagram-profile-scraper returns
        # one profile per item with posts nested under latestPosts). Other
        # adapters return items unchanged via the default identity implementation.
        items = adapter.expand_items(items)
        if not items:
            _save_wm()
            bound_logger.info(
                "Actor returned 0 post items after expansion — nothing to persist."
            )
            return

        # Schema probe: log the first item's top-level keys before mass-parsing so
        # that any downstream SchemaDriftError can be correlated with the actual
        # actor output shape without digging through DLQ payloads.
        first_keys = sorted(items[0].keys())
        bound_logger.info(
            "Schema probe: first item has {n} keys: {keys}",
            n=len(first_keys),
            keys=first_keys,
        )

        # --- Stage 2: parse + dedup ---
        posts, dlq = _parse_social_posts(
            adapter,
            items,
            actor_run_id=actor_run_id,
            ingestion_run_id=ingestion_run_id,
        )
        posts = _dedup_intra_batch(posts)
        # Cutoff filter — applied post-parse/dedup, not counted as DLQ
        if min_published_at is not None:
            before = len(posts)
            posts = [p for p in posts if p.published_at >= min_published_at]
            dropped_wm = before - len(posts)
            if dropped_wm > 0:
                bound_logger.info(
                    "Cutoff filter: dropped {n} posts older than {cutoff}",
                    n=dropped_wm,
                    cutoff=min_published_at.isoformat(),
                )
            if _metrics is not None:
                _metrics["filtered"] += dropped_wm
        if _metrics is not None:
            _metrics["scraped"] = len(posts)
            _metrics["errors"] += len(dlq)
        bound_logger.info(
            "Parsed {ok} posts, {drift} dropped to DLQ (schema drift / parse error)",
            ok=len(posts),
            drift=len(dlq),
        )
        if not posts:
            real_drift = [e for e in dlq if e["reason"] == "schema_drift"]
            non_post = [
                e for e in dlq if e["reason"] in ("non_post_item", "page_unavailable")
            ]
            if real_drift:
                bound_logger.error(
                    "All {n} items failed to parse — likely schema drift. "
                    "First-item keys (pre-normalization): {keys}. "
                    "Inspect DLQ before rerunning.",
                    n=len(items),
                    keys=first_keys,
                )
                for dlq_entry in dlq:
                    bound_logger.bind(
                        dlq=True,
                        reason=dlq_entry["reason"],
                        raw_keys=dlq_entry.get("raw_keys"),
                    ).error("DLQ entry: {err}", err=dlq_entry["error"])
                raise SystemExit(4)
            else:
                bound_logger.info(
                    "Actor returned {n} item(s) but none were parseable posts "
                    "({non_post} meta/status items) — no posts this run.",
                    n=len(items),
                    non_post=len(non_post),
                )
                for dlq_entry in dlq:
                    bound_logger.bind(
                        dlq=True,
                        reason=dlq_entry["reason"],
                        raw_keys=dlq_entry.get("raw_keys"),
                    ).warning("Non-post item skipped: {err}", err=dlq_entry["error"])
                _save_wm()
                return

    # Temporal governance summary — parsed by log-based metrics
    _min_pub = min((p.published_at for p in posts), default=None)
    _max_pub = max((p.published_at for p in posts), default=None)
    bound_logger.info(
        "temporal_governance"
        " mode={mode} effective_cutoff={cutoff}"
        " min_published_at={min_pub} max_published_at={max_pub}"
        " posts_accepted={accepted}",
        mode="backfill" if is_backfill else "incremental",
        cutoff=effective_cutoff.isoformat(),
        min_pub=_min_pub.isoformat() if _min_pub else None,
        max_pub=_max_pub.isoformat() if _max_pub else None,
        accepted=len(posts),
    )

    writer = get_storage_writer()
    warehouse = get_warehouse_loader() if not settings.is_local else None
    failed_loads: list[str] = []

    def _load_to_bq(uri: str, target_table: str, *, merge_key: str | None) -> None:
        if warehouse is None:
            return
        try:
            warehouse.load(uri, target_table, merge_key=merge_key)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — mirror YT pattern so one bad table doesn't kill the run
            failed_loads.append(target_table)
            bq_load_failures.labels(target_table=target_table).inc()
            bound_logger.error(
                "BQ load failed for {table}: {err}",
                table=target_table,
                err=str(exc),
            )

    # --- Stage 3: Raw persist (MERGE on post_id) ---
    bound_logger.info("=== Stage 3: Raw persist ===")
    raw_df = records_to_dataframe(posts)
    raw_df = _stamp_tenant_id(raw_df, settings.mapear_tenant_id)
    raw_uri = write_dataframe_as_parquet(
        writer,
        raw_df,
        SOCIAL_RAW_SCHEMA,
        "raw/social",
        f"platform={platform}/batch={batch_id}",
    )
    raw_table = f"mapear_raw.raw_social_posts_{platform}"
    _load_to_bq(raw_uri, raw_table, merge_key="post_id")
    bound_logger.info(
        "RAW written: {count} records → {uri}",
        count=len(posts),
        uri=raw_uri,
    )

    for dlq_entry in dlq:
        # Structured log line — the log-based metric ``mapear_social_dlq_total``
        # filters on ``dlq=true`` and labels by ``platform`` + ``reason``.
        bound_logger.bind(dlq=True, reason=dlq_entry["reason"]).error(
            "DLQ entry: {err}", err=dlq_entry["error"]
        )

    if dlq:
        dlq_records = _dlq_entries_to_records(dlq, adapter.actor_id)
        dlq_df = records_to_dataframe(dlq_records)
        dlq_df = _stamp_tenant_id(dlq_df, settings.mapear_tenant_id)
        dlq_uri = write_dataframe_as_parquet(
            writer,
            dlq_df,
            SOCIAL_DLQ_SCHEMA,
            "raw/social_dlq",
            f"platform={platform}/batch={batch_id}",
        )
        _load_to_bq(dlq_uri, "mapear_raw.raw_social_posts_dlq", merge_key=None)
        bound_logger.info(
            "DLQ persisted: {count} entries → mapear_raw.raw_social_posts_dlq",
            count=len(dlq_records),
        )
        # Prometheus: emit per-error-type counters from DLQ
        _err_counts: dict[str, int] = {}
        for entry in dlq:
            _err_counts[entry["reason"]] = _err_counts.get(entry["reason"], 0) + 1
        for err_type, count in _err_counts.items():
            social_errors_total.labels(platform=platform, error_type=err_type).inc(
                count
            )

    # --- Stage 4: Silver enrichment ---
    bound_logger.info(
        "=== Stage 4: Silver enrichment (NER + RegionMatcher + sentiment) ==="
    )
    # NER and RegionMatcher share the same Region instance loaded at startup
    # so DI is consistent end-to-end (region comes from settings.mapear_region).
    ner = NERExtractor(region=region)
    region_matcher = RegionMatcher(region)
    sentiment = SentimentAnalyzer()
    classifier = PoliticalSentimentClassifier()
    auditor = IdentityAuditor(resolver.list_targets())
    review_queue = IdentityReviewQueue()

    # Batch-level enrichment contribution counters (logged once at end of Stage 4)
    _batch_ner_contrib = 0
    _batch_matcher_contrib = 0
    _batch_handle_contrib = 0

    # Run static target validation once per batch
    target_violations = auditor.validate_targets()
    if target_violations:
        bound_logger.warning(
            "Identity audit found {n} violations in rn_targets.csv "
            "(errors={errors}, warnings={warnings})",
            n=len(target_violations),
            errors=sum(1 for v in target_violations if v.severity == "error"),
            warnings=sum(1 for v in target_violations if v.severity == "warning"),
        )

    silver_rows: list[dict] = []
    for post in posts:
        text = post.text
        page_name = post.author_display_name or post.account.display_name or None
        lang_result = detect_and_normalize(text, post.language)
        ner_result = ner.extract_from_text(text)

        noise_pct = ner_result.get("entities_person_removed_as_noise_pct", 0.0)
        if noise_pct:
            bound_logger.debug(
                "NER noise rate: {rate:.2f}%",
                rate=noise_pct,
            )
        ner_person_noise_rate.labels(platform=platform).set(noise_pct)

        ner_result, _contrib = _enrich_with_region_matcher(
            ner_result=ner_result,
            text=text,
            platform=post.platform,
            author_handle=post.account.handle,
            region=region,
            matcher=region_matcher,
        )
        _batch_ner_contrib += _contrib["ner"]
        _batch_matcher_contrib += _contrib["matcher"]
        _batch_handle_contrib += _contrib["handle"]

        sent = sentiment.analyze_text(text, entities=_build_entity_list(ner_result))

        # Resolver: handle-based lookup first (strongest signal), then NER fallback.
        # page_name passed so confidence_scorer can compute name_sim per-observation.
        resolution = resolver.resolve_best(
            mentions=ner_result.get("mentioned_persons", []),
            context=text,
            platform=post.platform,
            handle=post.account.handle,
            page_name=page_name,
        )

        # Enqueue suspicious resolutions for human review
        should_review, reasons = auditor.should_enqueue(
            result=resolution,
            post_id=post.post_id,
            platform=post.platform,
            handle=post.account.handle,
            page_name=page_name,
        )
        if should_review:
            review_queue.push(
                ReviewItem(
                    post_id=post.post_id,
                    platform=post.platform,
                    handle=post.account.handle,
                    page_name=page_name,
                    person_id=resolution.person_id,
                    confidence=resolution.confidence,
                    scope_status=resolution.scope_status.value,
                    reasons=reasons,
                    candidates=resolution.candidates,
                )
            )

        author_base_city: str | None = None
        if resolution.person_id and resolution.person_id.startswith("mayor_"):
            author_base_city = region.get_city_for_person_id(resolution.person_id)

        silver_rows.append(
            _build_silver_row(
                post=post,
                ner_result=ner_result,
                sentiment=sent,
                resolution=resolution,
                classification=None,  # filled in second pass once volumes are known
                batch_id=batch_id,
                lang_detection=lang_result,
                author_base_city=author_base_city,
                is_backfill=is_backfill,
                effective_cutoff_date=effective_cutoff,
            )
        )

    bound_logger.info(
        "Enrichment sources — NER: {ner} entity-mentions, "
        "RegionMatcher: +{matcher} new, handle-resolution: +{handle} new "
        "({total} posts processed)",
        ner=_batch_ner_contrib,
        matcher=_batch_matcher_contrib,
        handle=_batch_handle_contrib,
        total=len(posts),
    )

    # --- Stage 5: classification (needs batch-wide volume per person) ---
    volume_map = _volume_by_person(silver_rows)
    for row in silver_rows:
        polarity, volume_24h, velocity, engagement = _classifier_inputs(row, volume_map)

        classification = classifier.classify(
            polarity=polarity,
            volume_24h=volume_24h,
            velocity=velocity,
            engagement=engagement,
        )
        row["sentiment_label"] = classification.label
        row["confidence_score"] = classification.confidence
        row["risk_score"] = classification.risk_score
        row["decision_factors"] = classification.factors_as_dicts()
        row["rule_version"] = classification.rule_version
        row["model_version"] = classification.model_version

    # Issue 7: detect degenerate sentiment model output
    if len(silver_rows) > 100:
        unique_confidences = len(
            {
                r["confidence_score"]
                for r in silver_rows
                if r.get("confidence_score") is not None
            }
        )
        if unique_confidences < 10:
            bound_logger.warning(
                "Sentiment confidence has only {n} unique values in {total} posts "
                "— political sentiment model may be broken",
                n=unique_confidences,
                total=len(silver_rows),
            )

    # --- Stage 5.5b: shadow A/B scoring (Stage 1E v2) ---
    # Opt-in via MAPEAR_SHADOW_RULE_VERSION_YAML. A misconfigured YAML
    # raises here, after Stage 5 — the primary classification already
    # landed on every row, so a shadow config error never blocks silver.
    # The shadow rows themselves are computed from in_scope_rows in
    # Stage 6 so the shadow population matches what silver_social_posts
    # persists.
    shadow_scorer = build_shadow_scorer(
        yaml_path=settings.shadow.rule_version_yaml,
        enabled=settings.shadow.enabled,
        region=settings.mapear_region,
        tenant_id=settings.mapear_tenant_id,
        pipeline_version=PIPELINE_VERSION,
        source_type="social",
    )
    bound_logger.info(
        "=== Stage 5.5b: shadow A/B scoring — enabled={enabled} ===",
        enabled=shadow_scorer is not None,
    )

    # --- Stage 5.6: LLM narrative summary on ALERT rows (Eixo 2 v1) ---
    _social_narrative_explainer = _build_social_narrative_explainer(settings)
    bound_logger.info(
        "=== Stage 5.6: Social narrative explainer (Eixo 2 v1) — "
        "enabled={enabled} ===",
        enabled=_social_narrative_explainer is not None,
    )
    _apply_social_narrative_explainer(
        silver_rows,
        _social_narrative_explainer,
        tenant_id=settings.mapear_tenant_id,
        region_id=settings.mapear_region,
        provider=settings.llm.provider,
        model=settings.llm.model,
        coverage=settings.llm.explainer_coverage,
    )

    # --- Stage 6: IN_SCOPE gate + Silver persist ---
    bound_logger.info("=== Stage 6: IN_SCOPE gate + Silver persist ===")
    in_scope_rows = [
        r for r in silver_rows if r["scope_status"] == ScopeStatus.IN_SCOPE.value
    ]
    ambiguous_rows = [
        r for r in silver_rows if r["scope_status"] == ScopeStatus.AMBIGUOUS.value
    ]
    out_of_scope_rows = [
        r for r in silver_rows if r["scope_status"] == ScopeStatus.OUT_OF_SCOPE.value
    ]

    bound_logger.info(
        "Scope gate: IN_SCOPE={in_scope} / AMBIGUOUS={ambiguous} / "
        "OUT_OF_SCOPE={out_of_scope}",
        in_scope=len(in_scope_rows),
        ambiguous=len(ambiguous_rows),
        out_of_scope=len(out_of_scope_rows),
    )

    if in_scope_rows:
        silver_df = records_to_dataframe(in_scope_rows)
        silver_df = _stamp_tenant_id(silver_df, settings.mapear_tenant_id)
        _silver_partition = (
            f"platform={platform}/data_type=backfill/batch={batch_id}"
            if is_backfill
            else f"platform={platform}/batch={batch_id}"
        )
        silver_uri = write_dataframe_as_parquet(
            writer,
            silver_df,
            SOCIAL_SILVER_SCHEMA,
            "silver/social",
            _silver_partition,
        )
        silver_table = "mapear_silver.silver_social_posts"
        _load_to_bq(silver_uri, silver_table, merge_key="post_id")
        bound_logger.info(
            "SILVER written: {count} records → {uri}",
            count=len(in_scope_rows),
            uri=silver_uri,
        )

        # --- Stage 5.5b write — shadow A/B rows (Stage 1E v2) ---
        # Population matches silver_social_posts (in_scope only). Loaded
        # additively — grain (content_hash, shadow_rule_version) is a
        # composite resolved in the staging view, mirroring activations.
        shadow_rows = _apply_shadow_to_silver_rows(
            in_scope_rows, volume_map, shadow_scorer
        )
        if shadow_rows:
            shadow_df = records_to_dataframe(shadow_rows)
            shadow_uri = write_dataframe_as_parquet(
                writer,
                shadow_df,
                EVENT_SHADOW_SCHEMA,
                "silver/event_shadow",
                _silver_partition,
            )
            _load_to_bq(
                shadow_uri,
                "mapear_silver.silver_event_shadow",
                merge_key=None,
            )
            bound_logger.info(
                "SHADOW written: {count} records → {uri}",
                count=len(shadow_rows),
                uri=shadow_uri,
            )

        # --- Eixo 3 v1 — silver_author_activations fan-out ---
        # Lineage-only write: feeds the dbt mart fct_author_coactivation_daily
        # and the eval harness. No graph scoring on the hot path. Disabled
        # via MAPEAR_CIB_ENABLED=false when an operator needs to mute the
        # write path without redeploying.
        if settings.cib.enabled:
            activation_records = build_activation_records(
                in_scope_rows,
                region=settings.mapear_region,
                pipeline_version=PIPELINE_VERSION,
            )
            if activation_records:
                activations_df = records_to_dataframe(activation_records)
                activations_df = _stamp_tenant_id(
                    activations_df, settings.mapear_tenant_id
                )
                activations_uri = write_dataframe_as_parquet(
                    writer,
                    activations_df,
                    SOCIAL_AUTHOR_ACTIVATIONS_SCHEMA,
                    "silver/social_author_activations",
                    _silver_partition,
                )
                _load_to_bq(
                    activations_uri,
                    "mapear_silver.silver_author_activations",
                    # Activation grain is (author_id, platform, content_hash,
                    # person_target, published_at) — too composite for a single
                    # merge key. We use the deterministic post_id+person_target
                    # join in dbt instead and let BQ append duplicates here.
                    merge_key=None,
                )
                bound_logger.info(
                    "ACTIVATIONS written: {count} records → {uri}",
                    count=len(activation_records),
                    uri=activations_uri,
                )
    if _metrics is not None:
        _metrics["stored"] = len(in_scope_rows)
    social_stored_total.labels(platform=platform).inc(len(in_scope_rows))
    social_scraped_total.labels(platform=platform).inc(
        _metrics["scraped"] if _metrics else len(posts)
    )
    social_filtered_total.labels(platform=platform).inc(
        _metrics["filtered"] if _metrics else 0
    )

    # --- Entity fill rate metrics (todos os silver_rows processados) ---
    total_silver = len(silver_rows)
    if total_silver > 0:
        cities_pct = (
            sum(1 for r in silver_rows if r.get("mentioned_cities"))
            / total_silver
            * 100
        )
        mayors_pct = (
            sum(1 for r in silver_rows if r.get("mentioned_mayors"))
            / total_silver
            * 100
        )
        govs_pct = (
            sum(1 for r in silver_rows if r.get("mentioned_governors"))
            / total_silver
            * 100
        )
        bound_logger.info(
            "Entity fill rate ({platform}): "
            "cities={cities_pct:.1f}%"
            " mayors={mayors_pct:.1f}%"
            " governors={govs_pct:.1f}% "
            "(de {total} posts processados)",
            platform=platform,
            cities_pct=cities_pct,
            mayors_pct=mayors_pct,
            govs_pct=govs_pct,
            total=total_silver,
        )
        entity_fill_cities.labels(platform=platform).set(cities_pct)
        entity_fill_politicians.labels(platform=platform, role="mayor").set(mayors_pct)
        entity_fill_politicians.labels(platform=platform, role="governor").set(govs_pct)

    # Language fill rate + distribution metrics
    if total_silver > 0:
        lang_filled = sum(1 for r in silver_rows if r.get("language"))
        lang_pct = lang_filled / total_silver * 100
        language_fill_pct.labels(platform=platform).set(lang_pct)

        lang_counts: dict[str, int] = {}
        for r in silver_rows:
            lang = r.get("language") or "null"
            lang_counts[lang] = lang_counts.get(lang, 0) + 1

        bound_logger.info(
            "Language fill ({platform}): {filled}/{total} posts preenchidos "
            "({pct:.1f}%) — distribuição={dist}",
            platform=platform,
            filled=lang_filled,
            total=total_silver,
            pct=lang_pct,
            dist=lang_counts,
        )
        for lang, count in lang_counts.items():
            language_detected_total.labels(platform=platform, language=lang).inc(count)

    mayor_rows = [
        r for r in silver_rows if (r.get("person_id") or "").startswith("mayor_")
    ]
    if mayor_rows:
        filled = sum(1 for r in mayor_rows if r.get("author_base_city"))
        fill_pct = filled / len(mayor_rows) * 100
        mayor_author_base_city_fill_pct.labels(platform=platform).set(fill_pct)
        bound_logger.info(
            "author_base_city fill ({platform}): {filled}/{total} posts de prefeito "
            "({pct:.1f}%) — esperado ~100%",
            platform=platform,
            filled=filled,
            total=len(mayor_rows),
            pct=fill_pct,
        )

    # AMBIGUOUS rows are surfaced via structured logs (not persisted to Gold).
    # They still go through Raw (MERGE above) — analysts can join raw_social_posts
    # with the log-based AMBIGUOUS counter to audit borderline matches.
    for row in ambiguous_rows:
        bound_logger.bind(
            ambiguous=True,
            post_id=row["post_id"],
            matched_candidates=len(row.get("mentioned_persons", [])),
        ).info("AMBIGUOUS scope — excluded from Silver")

    # Log identity review queue summary
    queued = review_queue.snapshot()
    if queued:
        bound_logger.warning(
            "identity_review_queue: {n} posts enfileirados para revisão "
            "({platform}) — ver logs com reason breakdown",
            n=len(queued),
            platform=platform,
        )
        for item in queued:
            bound_logger.bind(
                review_post_id=item.post_id,
                review_handle=item.handle,
                review_page_name=item.page_name,
                review_person_id=item.person_id,
                review_confidence=item.confidence,
                review_reasons=item.reasons,
                review_candidates=item.candidates,
            ).warning("identity_review_queue item")

    # --- Summary ---
    bound_logger.info(
        "Pipeline complete — parsed={parsed}, dlq={dlq}, "
        "in_scope={in_scope}, ambiguous={ambiguous}, out_of_scope={out_of_scope}",
        parsed=len(posts),
        dlq=len(dlq),
        in_scope=len(in_scope_rows),
        ambiguous=len(ambiguous_rows),
        out_of_scope=len(out_of_scope_rows),
    )

    if not is_backfill:
        if failed_loads:
            bound_logger.warning(
                "Watermark NOT saved for {platform} — next run will re-ingest.",
                platform=platform,
            )
        else:
            watermark_manager.save_watermark(run_started_at)

    if failed_loads:
        bound_logger.error(
            "Pipeline finished with {n} BQ load failures: {tables}",
            n=len(failed_loads),
            tables=failed_loads,
        )
        sys.exit(2)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mapear_social")
    parser.add_argument(
        "--platform",
        choices=("facebook", "instagram", "x", "tiktok", "all"),
        default=None,
        help=(
            "Which platform to scrape. Use 'all' to run all 4 platforms in parallel. "
            "Defaults to SOCIAL_PLATFORM env / settings.platform."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("incremental", "backfill"),
        default="incremental",
        help=(
            "incremental: collect posts since watermark (floored at cutoff-date). "
            "backfill: collect from cutoff-date or backfill-since; writes to "
            "data_type=backfill partition and does not update the watermark."
        ),
    )
    parser.add_argument(
        "--cutoff-date",
        dest="cutoff_date",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            f"Hard minimum post date inclusive. "
            f"Defaults to {ELECTORAL_CUTOFF_DATE.isoformat()} "
            "(electoral context 2026). "
            "Incremental: effective_cutoff = max(watermark, cutoff-date). "
            "Backfill: start of the collection window."
        ),
    )
    parser.add_argument(
        "--lookback-days",
        dest="lookback_days",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Incremental only. Override watermark and collect from run_time minus N "
            "days, clamped to --cutoff-date."
        ),
    )
    parser.add_argument(
        "--backfill-since",
        dest="backfill_since",
        default=None,
        metavar="ISO_TIMESTAMP",
        help=(
            "Ingest posts published after this timestamp (implies --mode=backfill). "
            "Watermark is NOT updated. "
            "Example: --backfill-since=2026-04-01T00:00:00Z"
        ),
    )
    return parser.parse_args(argv)


def main() -> None:
    """Entry point for ``python -m mapear_social``."""
    args = _parse_args()

    if args.platform == "all":
        from mapear_social.parallel import run_all_platforms

        setup_logging()
        start_metrics_server()
        _point_seeds_at_dbt()
        backfill_since: datetime | None = None
        if args.backfill_since:
            backfill_since = datetime.fromisoformat(
                args.backfill_since.replace("Z", "+00:00")
            )
        cutoff_date: date | None = None
        if args.cutoff_date:
            cutoff_date = date.fromisoformat(args.cutoff_date)
        ok = run_all_platforms(
            backfill_since=backfill_since,
            mode=args.mode,
            cutoff_date=cutoff_date,
            lookback_days=args.lookback_days,
        )
        sys.exit(0 if ok else 2)

    backfill_since = None
    if args.backfill_since:
        backfill_since = datetime.fromisoformat(
            args.backfill_since.replace("Z", "+00:00")
        )
    cutoff_date = None
    if args.cutoff_date:
        cutoff_date = date.fromisoformat(args.cutoff_date)
    started = time.monotonic()
    platform = args.platform
    status = "completed"
    try:
        run_pipeline(
            cli_platform=platform,
            backfill_since=backfill_since,
            mode=args.mode,
            cutoff_date=cutoff_date,
            lookback_days=args.lookback_days,
        )
    except SystemExit as exc:
        status = "failed" if (exc.code or 0) != 0 else "completed"
        raise
    except Exception:
        status = "failed"
        logger.exception("Pipeline failed with unhandled exception")
        sys.exit(1)
    finally:
        elapsed = time.monotonic() - started
        if platform and platform != "all":
            social_pipeline_latency.labels(platform=platform).observe(elapsed)
        logger.info(
            "Pipeline runtime seconds={seconds:.3f} status={status}",
            seconds=elapsed,
            status=status,
        )


if __name__ == "__main__":
    main()
