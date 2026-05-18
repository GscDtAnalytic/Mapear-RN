{{
    config(
        materialized='incremental',
        unique_key=['content_id', 'source_type'],
        incremental_strategy='merge'
    )
}}

/*
    Unified fact table for RSS content. Uses content_hash as content_id.

    Only RN-relevant content lands here (is_rn_relevant = TRUE). National noise
    (Folha/Estadão articles without RN match) stays in silver but never inflates
    gold analytics. Incident 2026-04-18: without this filter, fct_content ran
    at 1,85x duplication and top trends were polluted by SP religious YT channels.
*/

WITH rss AS (

    SELECT
        content_hash AS content_id,
        url,
        source_feed,
        CAST(NULL AS {{ dbt.type_string() }}) AS channel_name,
        title,
        content_clean AS content_text,
        author,
        published_at,
        extracted_at,
        is_rn_relevant,
        mentioned_cities,
        mentioned_mayors,
        mentioned_governors,
        mentioned_parties,
        source_type
    FROM {{ ref('stg_rss__silver_articles') }}
    WHERE is_rn_relevant = TRUE

    {% if is_incremental() %}
        AND extracted_at > (SELECT MAX(extracted_at) FROM {{ this }} WHERE source_type = 'rss')
    {% endif %}

)

SELECT * FROM rss
