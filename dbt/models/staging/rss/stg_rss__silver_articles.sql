WITH source AS (

    SELECT * FROM {{ source('rss_silver', 'silver_articles') }}

),

renamed AS (

    SELECT
        url,
        source_feed,
        title,
        content_clean,
        author,
        published_at,
        extracted_at,
        content_hash,
        entities,
        is_rn_relevant,
        mentioned_cities,
        mentioned_mayors,
        mentioned_governors,
        mentioned_parties,
        schema_version,
        -- Electoral-pivot overlay (Fase 1): projeta coluna se existir na
        -- silver física, senão NULL tipado para manter o contrato do model.
        {{ silver_column_or_null('rss_silver', 'silver_articles', 'person_id', dbt.type_string()) }},
        {{ silver_column_or_null('rss_silver', 'silver_articles', 'scope_status', dbt.type_string()) }},
        {{ silver_column_or_null('rss_silver', 'silver_articles', 'resolution_confidence', dbt.type_float()) }},
        'rss' AS source_type
    FROM source
    -- Silver physical table is WRITE_APPEND without dedup. Keep the most
    -- recent extraction per content_hash until the loader switches to MERGE.
    QUALIFY ROW_NUMBER() OVER (PARTITION BY content_hash ORDER BY extracted_at DESC) = 1

),

-- V1: expose canonical field names alongside legacy ones.
-- V2 (breaking): remove is_rn_relevant and scope_status, keep only canonical.
enriched AS (

    SELECT
        *,
        is_rn_relevant                                                          AS content_rn_relevant,
        CASE WHEN scope_status = 'IN_SCOPE' THEN TRUE ELSE FALSE END            AS author_in_scope
    FROM renamed

)

SELECT * FROM enriched
