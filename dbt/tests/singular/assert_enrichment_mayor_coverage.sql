{{ config(severity='warn') }}
-- Qualidade de enriquecimento: para conteúdo de autores in-scope, espera-se que
-- pelo menos min_mayor_coverage_pct_{source_type} do conteúdo mencione pessoas políticas.
-- Cobertura baixa indica falha de resolução de identidade ou NER de pessoas.
-- Usa mentioned_persons (= union(mayors,governors) para RSS; nativo para Social/YT).
-- Threshold por source_type calibrado em B4 Fase 4 (Q3): RSS_SOCIAL_RECALL — fill rate
-- IN_SCOPE diverge estruturalmente entre fontes (Social com handle resolution > RSS).
{% set t = quality_thresholds() %}

WITH inscope_coverage AS (
    SELECT
        source_type,
        COUNT(*) AS total_inscope,
        SUM(CASE
            WHEN mentioned_persons IS NULL OR ARRAY_LENGTH(mentioned_persons) = 0
            THEN 1 ELSE 0
        END) AS zero_person_count
    FROM {{ ref('mapear_events') }}
    WHERE author_in_scope = TRUE
    GROUP BY source_type
),
evaluated AS (
    SELECT
        source_type,
        total_inscope,
        zero_person_count,
        zero_person_count * 1.0 / total_inscope AS zero_person_pct,
        CASE source_type
            WHEN 'rss'    THEN 1.0 - {{ t.min_mayor_coverage_pct_rss }}
            WHEN 'social' THEN 1.0 - {{ t.min_mayor_coverage_pct_social }}
            ELSE 1.0
        END AS max_zero_person_pct
    FROM inscope_coverage
    WHERE total_inscope >= {{ t.min_rows_coverage_check }}
)
SELECT
    source_type,
    total_inscope,
    zero_person_count,
    ROUND(zero_person_pct * 100, 1) AS zero_person_pct,
    ROUND(max_zero_person_pct * 100, 1) AS threshold_max_zero_pct
FROM evaluated
WHERE zero_person_pct > max_zero_person_pct
