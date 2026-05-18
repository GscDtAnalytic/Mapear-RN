{{ config(severity='warn') }}
-- Qualidade de enriquecimento: taxa mínima de documentos com cidades mencionadas.
-- Uma cobertura muito baixa indica regressão no extrator de entidades geográficas.
-- Thresholds por source_type configuráveis via dbt vars.
{% set t = quality_thresholds() %}

WITH coverage AS (
    SELECT
        source_type,
        COUNT(*) AS total,
        SUM(CASE
            WHEN mentioned_cities IS NULL OR ARRAY_LENGTH(mentioned_cities) = 0
            THEN 1 ELSE 0
        END) AS zero_city_count
    FROM {{ ref('mapear_events') }}
    GROUP BY source_type
),
evaluated AS (
    SELECT
        source_type,
        total,
        zero_city_count,
        zero_city_count * 1.0 / total AS zero_city_pct,
        CASE source_type
            WHEN 'rss'     THEN 1.0 - {{ t.min_city_coverage_pct_rss }}
            WHEN 'social'  THEN 1.0 - {{ t.min_city_coverage_pct_social }}
            ELSE 1.0
        END AS max_zero_city_pct
    FROM coverage
    WHERE total >= {{ t.min_rows_coverage_check }}
)
SELECT
    source_type,
    total AS total_rows,
    zero_city_count AS rows_without_cities,
    ROUND(zero_city_pct * 100, 1) AS zero_city_pct,
    ROUND(max_zero_city_pct * 100, 1) AS threshold_max_zero_pct
FROM evaluated
WHERE zero_city_pct > max_zero_city_pct
