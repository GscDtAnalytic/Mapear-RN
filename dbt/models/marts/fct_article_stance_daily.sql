{{
    config(
        materialized='incremental',
        unique_key=['stance_date', 'region', 'stance_prompt_version', 'content_hash'],
        incremental_strategy='merge',
        partition_by={
            'field': 'stance_date',
            'data_type': 'date',
            'granularity': 'day'
        },
        cluster_by=['region', 'stance_label'],
        tags=['mart', 'aggregated', 'narrative', 'eixo-2', 'stance']
    )
}}

/*
    fct_article_stance_daily — stance label por narrativa por dia.

    Grain : (stance_date, region, stance_prompt_version, content_hash)
    Source: stg_rss__article_stances (silver_article_stances)
    Cobre : Eixo 2 v2b — persiste stance por narrativa para análise
            operacional de posicionamento da mídia.

    Notas:
      - Rows com error IS NOT NULL têm stance_label = NULL e são
        excluídas do mart (preservadas na silver para diagnóstico).
      - Um content_hash pode aparecer com múltiplas stance_prompt_version
        quando o job reprocessa com um prompt novo — cada versão gera uma
        row independente. O operador filtra pela versão corrente.
      - stance_date é a data UTC derivada de classified_at.
*/

WITH source AS (

    SELECT * FROM {{ ref('stg_rss__article_stances') }}
    WHERE error IS NULL

    {% if is_incremental() %}
      AND CAST(classified_at AS DATE) >= (
          SELECT COALESCE(MAX(stance_date), DATE '1970-01-01')
          FROM {{ this }}
      )
    {% endif %}

)

SELECT
    CAST(classified_at AS DATE)                         AS stance_date,
    region,
    stance_prompt_version,
    content_hash,
    stance_label,
    confidence,
    stance_model,
    cache_hit,
    person_id,
    person_name,
    person_role,
    classified_at,
    job_run_id,
    pipeline_version,
    source_type,
    tenant_id
FROM source
