{{
    config(
        materialized='table'
    )
}}

/*
    Dimensão de fontes de dados — cada feed RSS monitorado.
    Gerado a partir das tabelas de conteúdo processado.
*/

SELECT DISTINCT
    source_feed AS source_id,
    source_feed AS source_name,
    'rss' AS source_type,
    CAST(NULL AS {{ dbt.type_string() }}) AS channel_id
FROM {{ ref('stg_rss__silver_articles') }}
WHERE source_feed IS NOT NULL
ORDER BY source_name
