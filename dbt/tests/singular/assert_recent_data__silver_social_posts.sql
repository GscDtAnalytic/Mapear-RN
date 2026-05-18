{{ config(tags=['presence']) }}
{%- if target.type == 'bigquery' %}
  {%- set window_7d  = "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)" %}
  {%- set hours_diff = "TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), l.latest_extracted_at, HOUR)" %}
{%- else %}
  {%- set window_7d  = "NOW() - INTERVAL 7 DAYS" %}
  {%- set hours_diff = "DATEDIFF('hour', l.latest_extracted_at, CURRENT_TIMESTAMP)" %}
{%- endif %}
-- Verifica que cada plataforma social ativa nos últimos 7d produziu dado dentro
-- do seu próprio threshold de freshness (alinhado com src_social.yml error_after).
-- Falha também se uma plataforma nova aparecer sem threshold definido aqui.
-- Origem: issue #38 (calibração 24h vs 72h) + gap 9 do B1 audit (granularidade per-platform).
WITH active_last_7d AS (
    SELECT DISTINCT platform
    FROM {{ source('social_silver', 'silver_social_posts') }}
    WHERE extracted_at >= {{ window_7d }}
),
latest_per_platform AS (
    SELECT
        platform,
        MAX(extracted_at) AS latest_extracted_at
    FROM {{ source('social_silver', 'silver_social_posts') }}
    GROUP BY platform
),
thresholds AS (
    SELECT 'facebook'  AS platform, 24 AS hours_threshold UNION ALL
    SELECT 'instagram',             24                    UNION ALL
    SELECT 'tiktok',                24                    UNION ALL
    SELECT 'x',                     72
),
violations AS (
    SELECT
        a.platform,
        l.latest_extracted_at,
        t.hours_threshold,
        {{ hours_diff }} AS hours_since_latest,
        'STALE' AS failure_reason
    FROM active_last_7d a
    JOIN latest_per_platform l USING (platform)
    JOIN thresholds t          USING (platform)
    WHERE {{ hours_diff }} > t.hours_threshold
),
unknown_platforms AS (
    -- Plataforma ativa sem threshold definido — requer atualização da CTE thresholds
    SELECT
        a.platform,
        l.latest_extracted_at,
        NULL AS hours_threshold,
        {{ hours_diff }} AS hours_since_latest,
        'NO_THRESHOLD_DEFINED' AS failure_reason
    FROM active_last_7d a
    JOIN latest_per_platform l USING (platform)
    LEFT JOIN thresholds t     USING (platform)
    WHERE t.platform IS NULL
)

SELECT * FROM violations
UNION ALL
SELECT * FROM unknown_platforms
