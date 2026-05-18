/*
    stg_rss__narrative_embeddings — staging para silver_narrative_embeddings.

    Grain : (content_hash, embedding_model)
    Source: silver_narrative_embeddings (Eixo 2 v2a)

    Re-embedding com novo modelo cria nova row sem invalidar a anterior;
    consumidores downstream filtram por embedding_model quando precisam
    pinar uma versão.
*/

{{ config(materialized='view') }}

WITH source AS (

    SELECT * FROM {{ source('rss_silver', 'silver_narrative_embeddings') }}

),

renamed AS (

    SELECT
        content_hash,
        embedding_model,
        embedding_dim,
        embedding,
        narrative_prompt_version,
        rule_version,
        job_run_id,
        run_at,
        pipeline_version,
        schema_version,
        source_type,
        region,
        tenant_id
    FROM source

)

SELECT * FROM renamed
