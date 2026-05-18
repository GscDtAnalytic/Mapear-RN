/*
    stg_social__community_scores — staging para silver_community_scores.

    Grain : (activation_date, region, algorithm, community_id)
    Source: silver_community_scores (Eixo 3 v3)

    Inauthenticity composite score por comunidade detectada.
    Combina synchrony (co_post_count normalizado), alignment (Jaccard)
    e content similarity (cosine entre embeddings dos posts).

    Deduplication: silver_community_scores é escrito por append; múltiplas
    execuções no mesmo dia geram duplicatas pelo mesmo grain. Mantemos
    apenas a linha mais recente (run_at DESC) para evitar MERGE failures
    nos marts downstream (fct_community_score_daily).
*/

{{ config(materialized='view') }}

WITH source AS (

    SELECT * FROM {{ source('social_silver', 'silver_community_scores') }}

),

renamed AS (

    SELECT
        activation_date,
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
        run_at,
        pipeline_version,
        schema_version,
        source_type,
        tenant_id
    FROM source

),

deduped AS (

    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY activation_date, region, algorithm, community_id
            ORDER BY run_at DESC
        ) AS _row_num
    FROM renamed

)

SELECT
    activation_date,
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
    run_at,
    pipeline_version,
    schema_version,
    source_type,
    tenant_id
FROM deduped
WHERE _row_num = 1
