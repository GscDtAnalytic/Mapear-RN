{{
    config(
        materialized='incremental',
        unique_key=['activation_date', 'region', 'algorithm', 'community_id'],
        incremental_strategy='merge',
        partition_by={
            'field': 'activation_date',
            'data_type': 'date',
            'granularity': 'day'
        },
        cluster_by=['region', 'algorithm'],
        tags=['mart', 'aggregated', 'cib', 'eixo-3', 'scoring']
    )
}}

/*
    fct_community_score_daily — inauthenticity scores por comunidade detectada.

    Grain : (activation_date, region, algorithm, community_id)
    Source: stg_social__community_scores (silver_community_scores)
    Cobre : Eixo 3 v3 — score composto de inautenticidade por cluster.

    Cada row é o score de UMA comunidade em UM dia.
    composite_score ∈ [0, 1] — valores próximos de 1.0 indicam alta
    probabilidade de comportamento coordenado inautêntico.

    Campos:
      avg_synchrony_score     — normalização de co_post_count (co-fires no tempo)
      avg_alignment_score     — Jaccard sobre targets políticos (mesmas pessoas)
      avg_content_similarity  — cosine entre embeddings dos posts (copiam conteúdo)
      composite_score         — média ponderada dos três componentes acima
*/

WITH source AS (

    SELECT * FROM {{ ref('stg_social__community_scores') }}

    {% if is_incremental() %}
      WHERE CAST(activation_date AS DATE) >= (
          SELECT COALESCE(MAX(activation_date), DATE '1970-01-01')
          FROM {{ this }}
      )
    {% endif %}

)

SELECT
    CAST(activation_date AS DATE)       AS activation_date,
    region,
    algorithm,
    community_id,
    community_size,
    pair_count,
    avg_synchrony_score,
    avg_alignment_score,
    avg_content_similarity_score,
    composite_score,
    score_version,
    score_weights_json,
    job_run_id,
    run_at                              AS scored_at,
    pipeline_version,
    tenant_id,
    CURRENT_TIMESTAMP                   AS last_refreshed_at
FROM source
