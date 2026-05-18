{{
    config(
        materialized='view',
        tags=['intermediate', 'rss']
    )
}}

/*
    int_rss_articles__gold_enriched — junta RSS silver com gold para
    expor sentiment_label, sentiment_overall, sentiment_confidence,
    topic_id, topic_id_source e topics em mapear_events (C3.1).

    G-09 / TDT-TOPIC-01: topic_id é propagado APENAS quando
    topic_id_source = 'keyword_map' AND topic_id BETWEEN 1 AND 10.
    gcp_ordinal/legacy_unknown/unclassified viram NULL para evitar
    joins enganosos com dim_topics. topic_id_source é sempre propagado
    para o consumidor poder distinguir "sem tópico" de "tópico descartado".

    Grain : 1 linha por content_hash (LEFT JOIN preserva grain do silver).
    Source: stg_rss__silver_articles (silver) + rss_gold.gold_articles.
*/

WITH silver AS (

    SELECT * FROM {{ ref('stg_rss__silver_articles') }}

),

gold AS (

    SELECT
        content_hash,
        sentiment_overall,
        sentiment_label,
        confidence_score                AS sentiment_confidence,
        topic_id                        AS topic_id_raw,
        topic_id_source,
        topics
    FROM {{ source('rss_gold', 'gold_articles') }}

),

joined AS (

    SELECT
        s.*,
        g.sentiment_overall,
        g.sentiment_label,
        g.sentiment_confidence,
        CASE
            WHEN g.topic_id_source = 'keyword_map'
                 AND g.topic_id_raw BETWEEN 1 AND 10
            THEN g.topic_id_raw
            ELSE NULL
        END                             AS topic_id,
        g.topic_id_source,
        g.topics
    FROM silver AS s
    LEFT JOIN gold AS g
        ON s.content_hash = g.content_hash

)

SELECT * FROM joined
