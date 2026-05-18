{{
    config(
        materialized='table'
    )
}}

/*
    Trend scores por entidade. Agrega volume, sentimento médio e
    score de tendência para cada prefeito/cidade/partido.
    Recalculado a cada run do pipeline.
*/

WITH entity_sentiment AS (

    SELECT * FROM {{ ref('fct_entity_sentiment') }}

),

aggregated AS (

    SELECT
        entity,
        entity_type,
        COUNT(*) AS total_mentions,
        COUNT(DISTINCT content_id) AS content_count,
        ROUND(AVG(sentiment), 4) AS avg_sentiment,
        ROUND(MIN(sentiment), 4) AS min_sentiment,
        ROUND(MAX(sentiment), 4) AS max_sentiment,
        MIN(published_at) AS first_mention,
        MAX(published_at) AS last_mention,
        COUNT(DISTINCT source_type) AS source_count
    FROM entity_sentiment
    WHERE published_at IS NOT NULL
    GROUP BY entity, entity_type

)

SELECT
    entity,
    entity_type,
    total_mentions,
    content_count,
    avg_sentiment,
    min_sentiment,
    max_sentiment,
    first_mention,
    last_mention,
    source_count
FROM aggregated
ORDER BY content_count DESC
