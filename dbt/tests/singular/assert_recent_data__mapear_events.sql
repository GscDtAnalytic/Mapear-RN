{{ config(tags=['presence']) }}
{%- set window_24h = "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)" if target.type == 'bigquery' else "NOW() - INTERVAL 24 HOURS" -%}
{%- set window_7d  = "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)"   if target.type == 'bigquery' else "NOW() - INTERVAL 7 DAYS"  -%}
-- Presence check: cada source_type ativo nos últimos 7d deve ter pelo menos 1 evento
-- nas últimas 24h. Falha se um source_type ativo sumir (zero linhas em 24h)
-- OU se a linha mais recente passar de 24h. Cobre o mart canônico (model,
-- não source) — fora do escopo de dbt source freshness.
WITH active_last_7d AS (
    SELECT DISTINCT source_type
    FROM {{ ref('mapear_events') }}
    WHERE extracted_at >= {{ window_7d }}
),
recent_last_24h AS (
    SELECT
        source_type,
        MAX(extracted_at) AS latest_extracted_at
    FROM {{ ref('mapear_events') }}
    WHERE extracted_at >= {{ window_24h }}
    GROUP BY source_type
)
SELECT
    a.source_type,
    r.latest_extracted_at
FROM active_last_7d a
LEFT JOIN recent_last_24h r USING (source_type)
WHERE r.source_type IS NULL
