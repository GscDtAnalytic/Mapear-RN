WITH source AS (

    SELECT * FROM {{ source('social_silver', 'silver_social_posts') }}

),

-- Exclude historical posts: only content published on or after the electoral
-- cutoff 2025-01-01 appears here. Query the silver table directly for older data.
incremental_only AS (

    SELECT * FROM source
    WHERE published_at >= CAST('2025-01-01' AS TIMESTAMP)

),

renamed AS (

    SELECT
        post_id,
        platform,
        url,
        author_handle,
        author_display_name,
        author_verified,
        text,
        language,
        language_confidence,
        language_reason,
        published_at,
        extracted_at,
        likes,
        comments,
        shares,
        views,
        is_repost,
        is_reply,
        parent_post_id,
        entities,
        mentioned_cities,
        mentioned_mayors,
        mentioned_governors,
        mentioned_parties,
        mentioned_candidates,
        mentioned_politicians,
        mentioned_persons,
        is_rn_relevant,
        sentiment_overall,
        sentiment_by_entity,
        person_id,
        scope_status,
        resolution_confidence,
        sentiment_label,
        confidence_score,
        risk_score,
        decision_factors,
        content_hash,
        actor_run_id,
        ingestion_run_id,
        rule_version,
        model_version,
        pipeline_version,
        source_type,
        batch_id,
        author_base_city,
        effective_cutoff_date,
        identity_resolution_version
    FROM incremental_only
    -- silver_social_posts uses MERGE on post_id so duplicates should not
    -- occur, but QUALIFY defensively covers any edge case from intra-batch
    -- races or replayed loads.
    QUALIFY ROW_NUMBER() OVER (PARTITION BY post_id ORDER BY extracted_at DESC) = 1

),

-- V1: expose canonical field names alongside legacy ones.
-- V2 (breaking): remove is_rn_relevant and scope_status, keep only canonical.
enriched AS (

    SELECT
        *,
        is_rn_relevant                                                          AS content_rn_relevant,
        CASE WHEN scope_status = 'IN_SCOPE' THEN TRUE ELSE FALSE END            AS author_in_scope
    FROM renamed

)

SELECT * FROM enriched
