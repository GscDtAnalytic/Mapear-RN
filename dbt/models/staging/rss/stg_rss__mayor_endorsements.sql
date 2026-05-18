/*
    stg_rss__mayor_endorsements — staging para silver_mayor_endorsements.

    Grain : (mayor_id, endorsement_prompt_version)
    Source: silver_mayor_endorsements (Eixo 2 v2d)

    detected_candidate é o candidato a governador que a LLM concluiu que o
    prefeito apoia, ou 'Indefinido' quando não há sinal claro. Rows com
    error IS NOT NULL têm detected_candidate NULL — a chamada LLM falhou ou
    o JSON era inválido; devem ser excluídas de análises (ver fct).
*/

{{ config(materialized='view') }}

WITH source AS (

    SELECT * FROM {{ source('rss_silver', 'silver_mayor_endorsements') }}

),

renamed AS (

    SELECT
        mayor_id,
        mayor_name,
        mayor_party,
        endorsement_prompt_version,
        detected_candidate,
        confidence,
        rationale,
        evidence_ids,
        endorsement_model,
        article_count,
        cache_hit,
        error,
        redaction_level,
        investigated_at,
        job_run_id,
        pipeline_version,
        schema_version,
        region,
        tenant_id
    FROM source

)

SELECT * FROM renamed
