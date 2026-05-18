-- INV-002: published_at deve preceder ou ser igual a extracted_at.
-- Conteúdo não pode ser coletado antes de ser publicado. Violação indica clock
-- skew no pipeline, dado corrompido, ou bug de parsing de data.
-- Cobre RSS e Social na camada de staging, próximo à origem.
SELECT
    record_id,
    source_type,
    published_at,
    extracted_at
FROM (
    SELECT
        content_hash                        AS record_id,
        'rss'                               AS source_type,
        CAST(published_at AS TIMESTAMP)     AS published_at,
        CAST(extracted_at AS TIMESTAMP)     AS extracted_at
    FROM {{ ref('stg_rss__silver_articles') }}

    UNION ALL

    SELECT
        post_id                             AS record_id,
        'social'                            AS source_type,
        CAST(published_at AS TIMESTAMP)     AS published_at,
        CAST(extracted_at AS TIMESTAMP)     AS extracted_at
    FROM {{ ref('stg_social__silver_posts') }}
) combined
WHERE published_at > extracted_at
