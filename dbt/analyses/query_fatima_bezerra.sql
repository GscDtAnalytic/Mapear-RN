/*
    Consulta de exemplo: menções a Fátima Bezerra na última semana (RSS + Social).

    Estratégia dupla de detecção:
      1. person_id resolvido pelo PersonResolver → cobertura de conteúdo IN_SCOPE
      2. mentioned_persons array → cobre menções em conteúdo sem resolução de autoria

    Para executar em BigQuery (prod):
        -- Remova o UNNEST se preferir ver eventos sem desdobrar menções.
        -- `published_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)`
        -- é a forma padrão em BigQuery; ajuste para DuckDB:
        -- `published_at >= NOW() - INTERVAL '7 days'`
*/

-- Variante 1: via person_id (conteúdo com autoria IN_SCOPE)
SELECT
    event_id,
    event_type,
    platform,
    published_at,
    author_display_name,
    author_handle,
    author_base_city,
    LEFT(text, 280)             AS text_preview,
    url,
    sentiment_label,
    sentiment_confidence,
    trend_score,
    person_name,
    person_role
FROM {{ ref('mapear_events') }}
WHERE
    person_id = 'governor_fatima_bezerra'
    AND published_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
ORDER BY published_at DESC

UNION ALL

-- Variante 2: via mentioned_persons (menções em conteúdo sem resolução de autoria)
SELECT
    e.event_id,
    e.event_type,
    e.platform,
    e.published_at,
    e.author_display_name,
    e.author_handle,
    e.author_base_city,
    LEFT(e.text, 280)           AS text_preview,
    e.url,
    e.sentiment_label,
    e.sentiment_confidence,
    e.trend_score,
    e.person_name,
    e.person_role
FROM {{ ref('mapear_events') }} AS e,
    UNNEST(e.mentioned_persons) AS mp
WHERE
    -- Normaliza espaços e case para absorver variações tipográficas
    LOWER(TRIM(mp)) IN (
        'fátima bezerra',
        'fatima bezerra',
        'fátima',
        'governadora fátima'
    )
    AND e.person_id IS DISTINCT FROM 'governor_fatima_bezerra'  -- evita duplicata com variante 1 (cobre NULLs)
    AND e.published_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
ORDER BY published_at DESC
