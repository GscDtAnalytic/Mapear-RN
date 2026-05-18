WITH source AS (

    SELECT * FROM {{ source('rss_raw', 'raw_articles') }}

),

renamed AS (

    SELECT
        url,
        source_feed,
        title,
        content,
        author,
        published_at,
        extracted_at,
        content_hash,
        html_lang,
        schema_version,
        'rss' AS source_type
    FROM source

)

SELECT * FROM renamed
