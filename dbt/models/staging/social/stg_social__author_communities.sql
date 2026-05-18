/*
    stg_social__author_communities — staging para silver_author_communities.

    Grain : (activation_date, region, author_id, platform, algorithm)
    Source: silver_author_communities (Eixo 3 v2a)

    Normaliza author_id lowercase, mantém o original em author_id_raw.
    Comunidade IDs NÃO são estáveis cross-day (Louvain remunera quando
    o grafo muda); a chave canônica downstream é
    (activation_date, region, algorithm, community_id).
*/

{{ config(materialized='view') }}

WITH source AS (

    SELECT * FROM {{ source('social_silver', 'silver_author_communities') }}

),

renamed AS (

    SELECT
        activation_date,
        region,
        author_id                                AS author_id_raw,
        LOWER(TRIM(author_id))                   AS author_id,
        platform,
        algorithm,
        community_id,
        community_size,
        edge_count,
        edge_density,
        avg_co_post_count,
        avg_jaccard,
        job_run_id,
        run_at,
        pipeline_version,
        schema_version,
        source_type,
        tenant_id
    FROM source

)

SELECT * FROM renamed
