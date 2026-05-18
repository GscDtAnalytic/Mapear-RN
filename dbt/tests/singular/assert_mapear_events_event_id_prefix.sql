-- event_id deve seguir o prefixo correto para cada source_type.
-- RSS → rss:, Social → fb:/ig:/x:/tt: (prefixo TikTok é tt:, vem do Apify post_id)
-- Retorna linhas com prefixo incorreto (falha quando count > 0).
SELECT
    event_id,
    source_type,
    platform
FROM {{ ref('mapear_events') }}
WHERE
    (source_type = 'rss'    AND event_id NOT LIKE 'rss:%')
    OR (source_type = 'social' AND platform = 'facebook'  AND event_id NOT LIKE 'fb:%')
    OR (source_type = 'social' AND platform = 'instagram' AND event_id NOT LIKE 'ig:%')
    OR (source_type = 'social' AND platform = 'x'         AND event_id NOT LIKE 'x:%')
    OR (source_type = 'social' AND platform = 'tiktok'    AND event_id NOT LIKE 'tt:%')
