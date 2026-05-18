{{
    config(
        materialized='table'
    )
}}

/*
    Dimensão de tópicos — resume tópicos com mapping semântico estável.

    Grain: topic_id × topic_id_source (composto).

    Filtro: apenas topic_id_source = 'keyword_map' (IDs 1-10 estáveis do TOPIC_ID_MAP).
    Registros com topic_id_source = 'gcp_ordinal' são excluídos porque o índice ordinal
    da GCP API não tem mapping semântico fixo entre chamadas. Consulte
    vw_topic_id_gcp_ordinal para inspeção desses registros.

    TDT-TOPIC-01: topic_id_source adicionado em 2026-05-07 para eliminar ambiguidade
    semântica. Histórico backfillado deterministicamente via
    infra/migrations/tdt_topic_01_backfill.py.
*/

WITH rss_gold AS (

    SELECT
        topic_id,
        topic_id_source,
        topics,
        sentiment_overall,
        published_at,
        'rss' AS source_type
    FROM {{ source('rss_gold', 'gold_articles') }}
    WHERE topic_id IS NOT NULL
      AND topic_id != -1
      AND topic_id_source = 'keyword_map'

),

all_gold AS (

    SELECT * FROM rss_gold

),

aggregated AS (

    SELECT
        topic_id,
        topic_id_source,
        ANY_VALUE(topics) AS topics,
        COUNT(*) AS content_count,
        ROUND(AVG(CAST(sentiment_overall AS {{ dbt.type_float() }})), 4) AS avg_sentiment,
        MIN(published_at) AS first_content,
        MAX(published_at) AS last_content
    FROM all_gold
    GROUP BY topic_id, topic_id_source

),

labeled AS (

    SELECT
        a.*,
        CASE a.topic_id
            WHEN 1  THEN 'Eleições'
            WHEN 2  THEN 'Governo estadual'
            WHEN 3  THEN 'Municipal'
            WHEN 4  THEN 'Políticas sociais'
            WHEN 5  THEN 'Segurança pública'
            WHEN 6  THEN 'Economia'
            WHEN 7  THEN 'Segurança'
            WHEN 8  THEN 'Saúde'
            WHEN 9  THEN 'Educação'
            WHEN 10 THEN 'Meio ambiente'
            ELSE CAST(a.topic_id AS {{ dbt.type_string() }})
        END AS topic_label
    FROM aggregated AS a

)

SELECT * FROM labeled
ORDER BY content_count DESC
