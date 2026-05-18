-- Validade semântica: event_type deve corresponder ao source_type.
-- rss → article | social → post
-- Divergência indica bug de mapeamento na construção de mapear_events.
SELECT
    event_id,
    source_type,
    event_type
FROM {{ ref('mapear_events') }}
WHERE
    (source_type = 'rss'    AND event_type != 'article')
    OR (source_type = 'social' AND event_type != 'post')
