/*
    stg_social__author_personas — staging para silver_author_personas.

    Grain : (persona_id, platform, author_id)
    Source: silver_author_personas (Eixo 3 v2b)

    Normaliza author_id e canonical_handle lowercase; mantém os
    originais (sufixo _raw) para auditoria. persona_id é content-
    addressed (sha1[:16] dos members ordenados), portanto estável
    sob mesma entrada — mas o mesmo grupo de contas re-medido com um
    novo membro chega com novo persona_id (membership mudou).
*/

{{ config(materialized='view') }}

WITH source AS (

    SELECT * FROM {{ source('social_silver', 'silver_author_personas') }}

),

renamed AS (

    SELECT
        persona_id,
        platform,
        author_id                                AS author_id_raw,
        LOWER(TRIM(author_id))                   AS author_id,
        member_count,
        canonical_handle                         AS canonical_handle_raw,
        LOWER(TRIM(canonical_handle))            AS canonical_handle,
        canonical_display_name,
        confidence,
        resolution_version,
        evidence_json,
        activation_date,
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
