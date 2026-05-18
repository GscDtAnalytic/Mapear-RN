-- Completude: campos obrigatórios não devem ser NULL.
-- event_id, published_at, extracted_at sempre obrigatórios.
-- url obrigatória para RSS e Social.
SELECT
    event_id,
    source_type,
    platform,
    published_at,
    CASE
        WHEN event_id IS NULL      THEN 'event_id'
        WHEN published_at IS NULL  THEN 'published_at'
        WHEN extracted_at IS NULL  THEN 'extracted_at'
        WHEN source_type IN ('rss', 'social') AND url IS NULL THEN 'url'
    END AS failed_column
FROM {{ ref('mapear_events') }}
WHERE event_id IS NULL
   OR published_at IS NULL
   OR extracted_at IS NULL
   OR (source_type IN ('rss', 'social') AND url IS NULL)
