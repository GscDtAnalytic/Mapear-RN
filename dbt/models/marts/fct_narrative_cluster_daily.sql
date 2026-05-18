{{
    config(
        materialized='incremental',
        unique_key=['cluster_run_date', 'region', 'algorithm', 'cluster_id'],
        incremental_strategy='merge',
        partition_by={
            'field': 'cluster_run_date',
            'data_type': 'date',
            'granularity': 'day'
        },
        cluster_by=['region', 'algorithm'],
        tags=['mart', 'aggregated', 'narrative', 'eixo-2', 'cluster']
    )
}}

/*
    fct_narrative_cluster_daily — clusters narrativos diários sobre embeddings.

    Grain : (cluster_run_date, region, algorithm, cluster_id)
    Source: stg_rss__narrative_clusters (silver_narrative_clusters)
    Cobre : Eixo 2 v2a — agrega membros + métricas por dia/região/algoritmo.

    Cada row representa UM cluster narrativo detectado em UM dia + região +
    algoritmo. Os membros são agregados em ARRAY<STRUCT<...>> para permitir
    queries do tipo "quais foram as narrativas dominantes hoje?" sem
    expandir N rows por membro.

    Outliers (cluster_id = -1) NÃO produzem row no mart — eles existem
    em silver para auditoria mas não são uma narrativa coordenada por
    definição. Filtre na staging via cluster_id >= 0.

    Notas:
      - cluster_id é estável dentro de (date, region, algorithm) por
        construção do compute_narrative_clusters (sorted-membership IDs)
        mas NÃO é estável cross-day. Cross-day cluster persistence é v3.
      - Quando o mesmo cluster é detectado por hdbscan + cosine_threshold,
        aparecem 2 rows distintas (uma por algoritmo).
      - cluster_label vem da silver row (ainda NULL na v2a — top-terms
        labelling vai entrar no v2b se justificar).
*/

WITH source AS (

    SELECT * FROM {{ ref('stg_rss__narrative_clusters') }}
    WHERE cluster_id >= 0

    {% if is_incremental() %}
      AND CAST(cluster_run_date AS DATE) >= (
          SELECT COALESCE(MAX(cluster_run_date), DATE '1970-01-01')
          FROM {{ this }}
      )
    {% endif %}

),

aggregated AS (

    SELECT
        CAST(cluster_run_date AS DATE)                                      AS cluster_run_date,
        region,
        algorithm,
        cluster_id,
        embedding_model,
        COUNT(DISTINCT content_hash)                                        AS cluster_size,
        MAX(avg_intra_cluster_distance)                                     AS avg_intra_cluster_distance,
        MAX(cluster_label)                                                  AS cluster_label,
        MAX(CASE WHEN member_role = 'centroid' THEN content_hash ELSE NULL END) AS centroid_content_hash,
        {% if target.type == 'bigquery' %}
        ARRAY_AGG(
            STRUCT(content_hash, member_role, distance_to_centroid)
            ORDER BY content_hash
        )
        {% else %}
        array_agg({'content_hash': content_hash, 'member_role': member_role, 'distance_to_centroid': distance_to_centroid} ORDER BY content_hash)
        {% endif %}                                                          AS members,
        MAX(job_run_id)                                                     AS job_run_id,
        MAX(run_at)                                                         AS last_clustered_at,
        MAX(pipeline_version)                                               AS pipeline_version,
        MAX(tenant_id)                                                      AS tenant_id,
        CURRENT_TIMESTAMP                                                   AS last_refreshed_at
    FROM source
    GROUP BY
        cluster_run_date,
        region,
        algorithm,
        cluster_id,
        embedding_model

)

SELECT * FROM aggregated
