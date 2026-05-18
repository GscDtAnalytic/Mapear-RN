/*
    stg_rss__article_stances — staging para silver_article_stances.

    Grain : (content_hash, stance_prompt_version)
    Source: silver_article_stances (Eixo 2 v2b)

    stance_label ∈ {favor, contra, neutro, NULL}. NULL significa que a
    chamada LLM falhou ou o JSON retornado era inválido — ver coluna
    error. Rows com error IS NOT NULL devem ser excluídas de análises.
*/

{{ config(materialized='view') }}

WITH source AS (

    SELECT * FROM {{ source('rss_silver', 'silver_article_stances') }}

),

renamed AS (

    SELECT
        content_hash,
        stance_prompt_version,
        stance_label,
        confidence,
        stance_model,
        cache_hit,
        error,
        redaction_level,
        person_id,
        person_name,
        person_role,
        classified_at,
        job_run_id,
        pipeline_version,
        schema_version,
        source_type,
        region,
        tenant_id
    FROM source

)

SELECT * FROM renamed
