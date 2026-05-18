{{
    config(
        materialized='incremental',
        unique_key=['activation_date', 'region', 'persona_id'],
        incremental_strategy='merge',
        partition_by={
            'field': 'activation_date',
            'data_type': 'date',
            'granularity': 'day'
        },
        cluster_by=['region', 'resolution_version'],
        tags=['mart', 'aggregated', 'cib', 'eixo-3', 'persona']
    )
}}

/*
    fct_author_persona_daily — personas cross-platform detectadas no dia.

    Grain : (activation_date, region, persona_id)
    Source: stg_social__author_personas (silver_author_personas)
    Cobre : Eixo 3 v2b — agrega membros + métricas de persona por dia.

    Cada row representa UMA persona detectada em UM dia + região.
    Os membros (platform + author_id) são agregados em ARRAY<STRUCT<...>>
    para permitir queries do tipo "quais foram as personas com 3+
    plataformas hoje?" sem expandir N rows.

    Notas:
      - persona_id é content-addressed: estável sob mesma entrada,
        mas membership change (uma conta nova entra na persona)
        renumera. Cross-day persona stitching é v3.
      - Persona com 1 membro NÃO aparece em silver — a engine só
        emite quando |members| >= 2. O mart preserva essa garantia.
      - evidence_json carrega a quebra de PairScore por edge. Em
        BQ, JSON_EXTRACT_ARRAY(evidence_json) destrincha quando o
        analista precisa entender por que duas contas mergiram.
*/

WITH source AS (

    SELECT * FROM {{ ref('stg_social__author_personas') }}

    {% if is_incremental() %}
      WHERE CAST(activation_date AS DATE) >= (
          SELECT COALESCE(MAX(activation_date), DATE '1970-01-01')
          FROM {{ this }}
      )
    {% endif %}

),

aggregated AS (

    SELECT
        CAST(activation_date AS DATE)                                       AS activation_date,
        region,
        persona_id,
        MAX(member_count)                                                   AS member_count,
        COUNT(DISTINCT platform)                                            AS platform_count,
        MAX(canonical_handle)                                               AS canonical_handle,
        MAX(canonical_display_name)                                         AS canonical_display_name,
        MAX(confidence)                                                     AS confidence,
        MAX(resolution_version)                                             AS resolution_version,
        MAX(evidence_json)                                                  AS evidence_json,
        {% if target.type == 'bigquery' %}
        ARRAY_AGG(
            STRUCT(platform, author_id)
            ORDER BY platform, author_id
        )
        {% else %}
        array_agg({'platform': platform, 'author_id': author_id} ORDER BY platform, author_id)
        {% endif %}                                                          AS members,
        MAX(job_run_id)                                                     AS job_run_id,
        MAX(run_at)                                                         AS last_detected_at,
        MAX(pipeline_version)                                               AS pipeline_version,
        MAX(tenant_id)                                                      AS tenant_id,
        CURRENT_TIMESTAMP                                                   AS last_refreshed_at
    FROM source
    GROUP BY
        activation_date,
        region,
        persona_id

)

SELECT * FROM aggregated
