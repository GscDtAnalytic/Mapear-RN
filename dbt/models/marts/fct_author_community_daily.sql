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
        tags=['mart', 'aggregated', 'cib', 'eixo-3', 'community']
    )
}}

/*
    fct_author_community_daily — comunidades diárias detectadas no grafo de co-activation.

    Grain : (activation_date, region, algorithm, community_id)
    Source: stg_social__author_communities (silver_author_communities)
    Cobre : Eixo 3 v2a — agrega membros + métricas de cluster por dia.

    Cada row representa UMA comunidade detectada em UM dia + região +
    algoritmo. Os membros são agregados em ARRAY<STRUCT<...>> para
    permitir queries do tipo "quais foram as comunidades ativas no
    dia X?" sem expandir N rows por membro.

    Notas:
      - community_id é estável dentro de (date, region, algorithm) por
        construção do detect_communities (sorted-membership IDs), mas
        NÃO é estável cross-day. Cross-day cluster tracking é v3.
      - Quando o mesmo cluster é detectado pelos dois algoritmos
        (louvain + label_propagation), aparecem 2 rows distintas.
      - Métricas (edge_density, avg_co_post_count, avg_jaccard) são
        denormalizadas em silver — aqui só fazemos MAX/ANY_VALUE para
        ressaltar (idempotente: todos os membros da mesma comunidade
        carregam o mesmo valor).
*/

WITH source AS (

    SELECT * FROM {{ ref('stg_social__author_communities') }}

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
        algorithm,
        community_id,
        COUNT(DISTINCT author_id || '|' || platform)                        AS community_size,
        MAX(edge_count)                                                     AS edge_count,
        MAX(edge_density)                                                   AS edge_density,
        MAX(avg_co_post_count)                                              AS avg_co_post_count,
        MAX(avg_jaccard)                                                    AS avg_jaccard,
        {% if target.type == 'bigquery' %}
        ARRAY_AGG(
            STRUCT(author_id, platform)
            ORDER BY author_id, platform
        )
        {% else %}
        array_agg({'author_id': author_id, 'platform': platform} ORDER BY author_id, platform)
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
        algorithm,
        community_id

)

SELECT * FROM aggregated
