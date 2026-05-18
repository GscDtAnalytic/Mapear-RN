{{
    config(
        materialized='incremental',
        unique_key='event_id',
        incremental_strategy='merge',
        on_schema_change='append_new_columns',
        partition_by={
            'field': 'published_at',
            'data_type': 'timestamp',
            'granularity': 'day'
        },
        cluster_by=['platform', 'person_id'],
        tags=['events', 'gold', 'canonical']
    )
}}

/*
    mapear_events — camada gold canônica multiplataforma.

    Grain    : 1 linha por conteúdo (event_id é globalmente único por prefixo de fonte).
    Escopo   : Todo conteúdo rn_relevant=TRUE, independentemente de scope_status.
               Use author_in_scope para filtrar somente conteúdo IN_SCOPE.
    Fontes   : RSS → stg_rss__silver_articles
               Social → stg_social__silver_posts

    Prefixos de event_id:
      rss      rss:<content_hash>
      social   <platform>:<id>  (ex.: fb:<id>, ig:<id>, x:<id>, tt:<id> para tiktok — prefixo vem do Apify post_id)

    Campos NULL por fonte — regras detalhadas em schema.yml:
      RSS     : author_handle, author_base_city, trend_score
                (sentiment_*, topic_id, topics, topic_id_source disponíveis após C3.1)
      Social  : topic_id, topics, topic_id_source (sentiment e trend_score disponíveis)
*/

WITH rss AS (

    SELECT
        CONCAT('rss:', content_hash)                                    AS event_id,
        'article'                                                       AS event_type,
        'rss'                                                           AS platform,
        source_feed                                                     AS source_pipeline,
        CAST(NULL AS {{ dbt.type_string() }})                           AS author_handle,
        author                                                          AS author_display_name,
        person_id,
        published_at,
        content_clean                                                   AS text,
        url,
        entities,
        {{ array_concat_strings('mentioned_mayors', 'mentioned_governors') }}
                                                                        AS mentioned_persons,
        mentioned_cities,
        mentioned_parties,
        CAST(NULL AS {{ dbt.type_string() }})                           AS author_base_city,
        resolution_confidence,
        sentiment_overall,
        sentiment_label,
        sentiment_confidence,
        is_rn_relevant                                                  AS content_rn_relevant,
        is_rn_relevant                                                  AS rn_relevant,
        CASE WHEN scope_status = 'IN_SCOPE' THEN TRUE ELSE FALSE END    AS author_in_scope,
        topic_id,
        topics,
        topic_id_source,
        CAST(NULL AS {{ dbt.type_float() }})                            AS trend_score,
        {% if target.type == 'bigquery' -%}
        TO_JSON_STRING(STRUCT(
            source_feed     AS source_feed,
            content_hash    AS content_hash,
            schema_version  AS source_schema_version
        ))
        {%- else -%}
        CAST(NULL AS {{ dbt.type_string() }})
        {%- endif %}                                                    AS metadata_json,
        1                                                               AS schema_version,
        'rss'                                                           AS source_type,
        extracted_at
    FROM {{ ref('int_rss_articles__gold_enriched') }}
    WHERE is_rn_relevant = TRUE

    {% if is_incremental() %}
    AND extracted_at > (
        SELECT COALESCE(MAX(extracted_at), CAST('1970-01-01' AS TIMESTAMP))
        FROM {{ this }}
        WHERE source_type = 'rss'
    )
    {% endif %}

),

social AS (

    SELECT
        post_id                                                         AS event_id,
        'post'                                                          AS event_type,
        platform,
        CONCAT('apify/', platform)                                      AS source_pipeline,
        author_handle,
        author_display_name,
        person_id,
        published_at,
        text,
        url,
        entities,
        mentioned_persons,
        mentioned_cities,
        mentioned_parties,
        author_base_city,
        resolution_confidence,
        sentiment_overall,
        sentiment_label,
        confidence_score                                                AS sentiment_confidence,
        is_rn_relevant                                                  AS content_rn_relevant,
        is_rn_relevant                                                  AS rn_relevant,
        CASE WHEN scope_status = 'IN_SCOPE' THEN TRUE ELSE FALSE END    AS author_in_scope,
        CAST(NULL AS {{ dbt.type_int() }})                              AS topic_id,
        {% if target.type == 'bigquery' -%}
        CAST(NULL AS ARRAY<STRING>)
        {%- else -%}
        CAST(NULL AS TEXT[])
        {%- endif %}                                                    AS topics,
        CAST(NULL AS {{ dbt.type_string() }})                           AS topic_id_source,
        {% if target.type == 'bigquery' -%}
        LOG(
            1.0
            + COALESCE(likes, 0)
            + COALESCE(comments, 0) * 2.0
            + COALESCE(shares, 0) * 3.0
        )
        {%- else -%}
        LN(
            1.0
            + COALESCE(likes, 0)
            + COALESCE(comments, 0) * 2.0
            + COALESCE(shares, 0) * 3.0
        )
        {%- endif %}                                                    AS trend_score,
        {% if target.type == 'bigquery' -%}
        TO_JSON_STRING(STRUCT(
            likes                       AS likes,
            comments                    AS comments,
            shares                      AS shares,
            views                       AS views,
            is_repost                   AS is_repost,
            is_reply                    AS is_reply,
            parent_post_id              AS parent_post_id,
            actor_run_id                AS actor_run_id,
            ingestion_run_id            AS ingestion_run_id,
            batch_id                    AS batch_id,
            identity_resolution_version AS identity_resolution_version,
            risk_score                  AS risk_score
        ))
        {%- else -%}
        CAST(NULL AS {{ dbt.type_string() }})
        {%- endif %}                                                    AS metadata_json,
        1                                                               AS schema_version,
        'social'                                                        AS source_type,
        extracted_at
    FROM {{ ref('int_social_posts__deduped') }}
    WHERE is_rn_relevant = TRUE
      AND is_canonical = TRUE

    {% if is_incremental() %}
    AND extracted_at > (
        SELECT COALESCE(MAX(extracted_at), CAST('1970-01-01' AS TIMESTAMP))
        FROM {{ this }}
        WHERE source_type = 'social'
    )
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
        name            AS person_name,
        role            AS person_role,
        party           AS person_party,
        city            AS person_city,
        is_incumbent    AS person_is_incumbent
    FROM {{ ref('dim_persons') }}
    WHERE is_current = TRUE

)

SELECT
    c.event_id,
    c.event_type,
    c.platform,
    c.source_pipeline,
    c.author_handle,
    c.author_display_name,
    c.person_id,
    p.person_name,
    p.person_role,
    p.person_party,
    p.person_city,
    p.person_is_incumbent,
    c.published_at,
    c.text,
    c.url,
    c.entities,
    c.mentioned_persons,
    c.mentioned_cities,
    c.mentioned_parties,
    c.author_base_city,
    c.resolution_confidence,
    c.sentiment_overall,
    c.sentiment_label,
    c.sentiment_confidence,
    c.content_rn_relevant,
    c.rn_relevant,
    c.author_in_scope,
    c.topic_id,
    c.topics,
    c.topic_id_source,
    c.trend_score,
    c.metadata_json,
    c.schema_version,
    c.source_type,
    c.extracted_at
FROM combined AS c
LEFT JOIN persons_current AS p
    ON c.person_id = p.person_id
