/*
    stg_rss__narrative_clusters — staging para silver_narrative_clusters.

    Grain : (cluster_run_date, region, algorithm, content_hash)
    Source: silver_narrative_clusters (Eixo 2 v2a)

    cluster_id NÃO é estável cross-day. A chave canônica downstream é
    (cluster_run_date, region, algorithm, cluster_id). cluster_id = -1
    marca outlier.
*/

{{ config(materialized='view') }}

WITH source AS (

    SELECT * FROM {{ source('rss_silver', 'silver_narrative_clusters') }}

),

renamed AS (

    SELECT
        cluster_run_date,
        region,
        algorithm,
        content_hash,
        embedding_model,
        cluster_id,
        member_role,
        cluster_size,
        distance_to_centroid,
        avg_intra_cluster_distance,
        cluster_label,
        job_run_id,
        run_at,
        pipeline_version,
        schema_version,
        source_type,
        tenant_id
    FROM source

)

SELECT * FROM renamed
