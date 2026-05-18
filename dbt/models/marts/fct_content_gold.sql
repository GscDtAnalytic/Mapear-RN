{{
    config(
        materialized='incremental',
        unique_key=['content_id', 'source_type'],
        incremental_strategy='merge'
    )
}}

/*
    Electoral-scope fact table — Gold v2 (BL-RESTRUCT / Fase 1 + Fase 2).

    Only content that resolves to a canonical person_id with
    scope_status = 'IN_SCOPE' lands here. OUT_OF_SCOPE and AMBIGUOUS
    mentions are excluded at this gate.

    Social columns (Fase 2 — BL-F2-08):
      * platform       — 'facebook'/'instagram'/'x'/'tiktok' for social;
                         mirrors source_type for rss.
      * sentiment_label — FAVORABLE/WARNING/ALERT (PoliticalSentimentClassifier).
                         NULL for rss until BL-F2-05 backfill.
      * confidence_score, risk_score — classifier outputs.
      * likes, comments, shares, views — engagement (social only; NULL elsewhere).

    Coexists with fct_content (legacy) during cutover window (Fase 3).
*/

WITH rss AS (

    SELECT
        content_hash                                    AS content_id,
        url,
        source_feed,
        CAST(NULL AS {{ dbt.type_string() }})          AS channel_name,
        title,
        content_clean                                   AS content_text,
        author,
        published_at,
        extracted_at,
        person_id,
        scope_status,
        resolution_confidence,
        is_rn_relevant,
        mentioned_cities,
        mentioned_mayors,
        mentioned_governors,
        mentioned_parties,
        'rss'                                           AS platform,
        CAST(NULL AS {{ dbt.type_string() }})          AS sentiment_label,
        CAST(NULL AS {{ dbt.type_float() }})           AS confidence_score,
        CAST(NULL AS {{ dbt.type_float() }})           AS risk_score,
        CAST(NULL AS {{ dbt.type_int() }})             AS likes,
        CAST(NULL AS {{ dbt.type_int() }})             AS comments,
        CAST(NULL AS {{ dbt.type_int() }})             AS shares,
        CAST(NULL AS {{ dbt.type_int() }})             AS views,
        source_type
    FROM {{ ref('stg_rss__silver_articles') }}
    WHERE is_rn_relevant = TRUE
      AND scope_status = 'IN_SCOPE'
      AND person_id IS NOT NULL

    {% if is_incremental() %}
        AND extracted_at > (SELECT MAX(extracted_at) FROM {{ this }} WHERE source_type = 'rss')
    {% endif %}

),

social AS (

    SELECT
        post_id                                         AS content_id,
        url,
        CAST(NULL AS {{ dbt.type_string() }})          AS source_feed,
        CAST(NULL AS {{ dbt.type_string() }})          AS channel_name,
        CAST(NULL AS {{ dbt.type_string() }})          AS title,
        text                                            AS content_text,
        author_handle                                   AS author,
        published_at,
        extracted_at,
        person_id,
        scope_status,
        resolution_confidence,
        is_rn_relevant,
        mentioned_cities,
        mentioned_mayors,
        mentioned_governors,
        mentioned_parties,
        platform,
        sentiment_label,
        confidence_score,
        risk_score,
        likes,
        comments,
        shares,
        views,
        source_type
    FROM {{ ref('int_social_posts__deduped') }}
    WHERE is_rn_relevant = TRUE
      AND scope_status = 'IN_SCOPE'
      AND person_id IS NOT NULL
      AND is_canonical = TRUE

    {% if is_incremental() %}
        AND extracted_at > (SELECT MAX(extracted_at) FROM {{ this }} WHERE source_type = 'social')
    {% endif %}

),

combined AS (

    SELECT * FROM rss
    UNION ALL
    SELECT * FROM social

),

persons_current AS (

    SELECT
        person_id,
        name          AS person_name,
        role          AS person_role,
        party         AS person_party,
        city          AS person_city,
        is_incumbent  AS person_is_incumbent
    FROM {{ ref('dim_persons') }}
    WHERE is_current = TRUE

)

SELECT
    c.content_id,
    c.source_type,
    c.platform,
    c.url,
    c.source_feed,
    c.channel_name,
    c.title,
    c.content_text,
    c.author,
    c.published_at,
    c.extracted_at,
    c.person_id,
    p.person_name,
    p.person_role,
    p.person_party,
    p.person_city,
    p.person_is_incumbent,
    c.scope_status,
    c.resolution_confidence,
    c.is_rn_relevant,
    -- V1 canonical fields. V2: keep only these and remove scope_status/is_rn_relevant.
    TRUE                AS author_in_scope,
    c.is_rn_relevant    AS content_rn_relevant,
    c.mentioned_cities,
    c.mentioned_mayors,
    c.mentioned_governors,
    c.mentioned_parties,
    c.sentiment_label,
    c.confidence_score,
    c.risk_score,
    c.likes,
    c.comments,
    c.shares,
    c.views
FROM combined AS c
LEFT JOIN persons_current AS p
    ON c.person_id = p.person_id
