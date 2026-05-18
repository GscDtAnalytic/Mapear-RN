/*
    stg_social__cluster_series — staging para silver_cluster_series.

    Grain : (activation_date, region, algorithm, community_id)
    Source: silver_cluster_series (Eixo 3 v3)

    Persistência de identidade cross-day: series_id estável derivado dos
    membros iniciais do cluster. Analistas podem filtrar por series_id
    para acompanhar a evolução de um squad suspeito ao longo de dias.

    Deduplication: silver_cluster_series é escrito por append; múltiplas
    execuções no mesmo dia geram duplicatas pelo mesmo grain. Mantemos
    apenas a linha mais recente (run_at DESC) para evitar MERGE failures
    nos marts downstream (fct_cluster_series).
*/

{{ config(materialized='view') }}

WITH source AS (

    SELECT * FROM {{ source('social_silver', 'silver_cluster_series') }}

),

renamed AS (

    SELECT
        activation_date,
        region,
        algorithm,
        community_id,
        series_id,
        series_start_date,
        jaccard_to_previous,
        is_new_series,
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
    series_id,
    series_start_date,
    jaccard_to_previous,
    is_new_series,
    job_run_id,
    run_at,
    pipeline_version,
    schema_version,
    source_type,
    tenant_id
FROM deduped
WHERE _row_num = 1
