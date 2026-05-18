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
        cluster_by=['region', 'algorithm', 'series_id'],
        tags=['mart', 'aggregated', 'cib', 'eixo-3', 'series']
    )
}}

/*
    fct_cluster_series — persistência cross-day de identidade de clusters.

    Grain : (activation_date, region, algorithm, community_id)
    Source: stg_social__cluster_series (silver_cluster_series)
    Cobre : Eixo 3 v3 — conecta community_ids efêmeros a um series_id estável.

    Cada row associa uma comunidade detectada em um dia ao seu series_id —
    identificador estável derivado dos membros na primeira aparição do cluster.
    Analistas filtram por series_id para acompanhar a evolução de um squad
    suspeito ao longo de múltiplos dias.

    jaccard_to_previous — sobreposição de membros entre hoje e ontem.
                          NULL para o primeiro dia de uma série.
    is_new_series       — TRUE quando o cluster não tem continuação do dia anterior.
    series_age_days     — quantos dias a série está ativa até este ponto.
*/

WITH source AS (

    SELECT * FROM {{ ref('stg_social__cluster_series') }}

    {% if is_incremental() %}
      WHERE CAST(activation_date AS DATE) >= (
          SELECT COALESCE(MAX(activation_date), DATE '1970-01-01')
          FROM {{ this }}
      )
    {% endif %}

),

with_age AS (

    SELECT
        CAST(activation_date AS DATE)           AS activation_date,
        region,
        algorithm,
        community_id,
        series_id,
        CAST(series_start_date AS DATE)         AS series_start_date,
        jaccard_to_previous,
        is_new_series,
        {% if target.type == 'bigquery' %}
        DATE_DIFF(CAST(activation_date AS DATE), CAST(series_start_date AS DATE), DAY)
        {% else %}
        datediff('day', CAST(series_start_date AS DATE), CAST(activation_date AS DATE))
        {% endif %}                             AS series_age_days,
        job_run_id,
        run_at                                  AS assigned_at,
        pipeline_version,
        tenant_id,
        CURRENT_TIMESTAMP                       AS last_refreshed_at
    FROM source

)

SELECT * FROM with_age
