/*
    stg_social__author_activations — staging para silver_author_activations.

    Grain : (author_id, platform, content_hash, person_target, published_at)
    Source: silver_author_activations (Eixo 3 v1)

    Faz cast leve + canonicaliza author_id/person_target lowercase para
    pareamento entre handles em case diferente. Mantém os valores
    originais em author_id_raw / person_target_raw para auditoria.

    Não filtra por electoral cutoff aqui — silver_author_activations já
    é só fan-out de in_scope_rows do pipeline social.
*/

{{ config(materialized='view') }}

WITH source AS (

    SELECT * FROM {{ source('social_silver', 'silver_author_activations') }}

),

renamed AS (

    SELECT
        author_id                                AS author_id_raw,
        LOWER(TRIM(author_id))                   AS author_id,
        platform,
        post_id,
        content_hash,
        person_target                            AS person_target_raw,
        LOWER(TRIM(person_target))               AS person_target,
        target_kind,
        target_person_id,
        author_in_scope,
        published_at,
        extracted_at,
        batch_id,
        actor_run_id,
        ingestion_run_id,
        pipeline_version,
        schema_version,
        source_type,
        region,
        tenant_id
    FROM source

)

SELECT * FROM renamed
