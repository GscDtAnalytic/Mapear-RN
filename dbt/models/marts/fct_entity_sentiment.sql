{{
    config(
        materialized='incremental',
        unique_key=['content_id', 'entity'],
        on_schema_change='append_new_columns'
    )
}}

/*
    Sentimento granular por entidade (prefeito, cidade, partido).
    Fonte: gold_articles com campo sentiment_by_entity (JSON array).
    Em DuckDB, usa unnest; em BigQuery, usa UNNEST com CROSS JOIN.
*/

WITH gold AS (

    SELECT
        content_hash AS content_id,
        url,
        source_feed,
        CAST(NULL AS {{ dbt.type_string() }}) AS channel_name,
        title,
        published_at,
        sentiment_by_entity,
        'rss' AS source_type
    FROM {{ source('rss_gold', 'gold_articles') }}
    WHERE is_rn_relevant = TRUE

    {% if is_incremental() %}
        AND published_at > (SELECT MAX(published_at) FROM {{ this }} WHERE source_type = 'rss')
    {% endif %}

),

-- Extrair entidades do array JSON de sentimento
entities AS (

    {{ unnest_json_array('gold', 'sentiment_by_entity', ['entity', 'entity_type', 'sentiment', 'mention_count', 'sentiment_source'], key_column='content_id') }}

)

SELECT
    g.content_id,
    g.url,
    g.source_feed,
    g.channel_name,
    g.title,
    g.published_at,
    g.source_type,
    e.entity,
    e.entity_type,
    CAST(e.sentiment AS {{ dbt.type_float() }}) AS sentiment,
    CAST(e.mention_count AS {{ dbt.type_int() }}) AS mention_count,
    e.sentiment_source
FROM gold AS g
INNER JOIN entities AS e
    ON g.content_id = e.content_id
