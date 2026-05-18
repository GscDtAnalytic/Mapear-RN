-- sentiment_label e sentiment_confidence devem ser preenchidos juntos ou ambos NULL.
-- Retorna linhas com estado inconsistente (falha quando count > 0).
SELECT
    event_id,
    source_type,
    platform,
    sentiment_label,
    sentiment_confidence
FROM {{ ref('mapear_events') }}
WHERE
    (sentiment_label IS NULL) != (sentiment_confidence IS NULL)
