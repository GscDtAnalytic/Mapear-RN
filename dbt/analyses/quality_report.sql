-- Relatório de qualidade por batch — status PASS / WARN / FAIL / SKIP por check.
-- Execute com:  dbt compile --select quality_report
-- Rode o SQL compilado (target/compiled/...) via DuckDB CLI ou BigQuery console.
--
-- Todas as categorias:
--   completeness · semantic · enrichment · temporal · distribution · consistency
{% set t = quality_thresholds() %}

WITH

-- 1. Completude -----------------------------------------------------------
chk_completeness_ids AS (
    SELECT
        'completeness'          AS category,
        'ids_urls_dates'        AS check_name,
        COUNT(*)                AS failing_rows,
        (SELECT COUNT(*) FROM {{ ref('mapear_events') }}) AS total_rows,
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS status,
        'Campos obrigatórios nulos: event_id, published_at, extracted_at, url(rss/social)' AS description
    FROM {{ ref('mapear_events') }}
    WHERE event_id IS NULL
       OR published_at IS NULL
       OR extracted_at IS NULL
       OR (source_type IN ('rss', 'social') AND url IS NULL)
),

-- 2a. Semântica — alinhamento event_type ↔ source_type ---------------------
chk_event_source_alignment AS (
    SELECT
        'semantic'                          AS category,
        'event_type_source_type_alignment'  AS check_name,
        COUNT(*)                            AS failing_rows,
        (SELECT COUNT(*) FROM {{ ref('mapear_events') }}) AS total_rows,
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS status,
        'event_type não corresponde ao source_type esperado (rss→article, social→post)' AS description
    FROM {{ ref('mapear_events') }}
    WHERE (source_type = 'rss'     AND event_type != 'article')
       OR (source_type = 'social'  AND event_type != 'post')
),

-- 2b. Semântica — intervalos de scores numéricos ---------------------------
chk_score_ranges AS (
    SELECT
        'semantic'      AS category,
        'score_ranges'  AS check_name,
        COUNT(*)        AS failing_rows,
        (SELECT COUNT(*) FROM {{ ref('mapear_events') }}) AS total_rows,
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS status,
        'Score fora do intervalo: resolution_confidence/sentiment_confidence ∈ [0,1]; sentiment_overall ∈ [-1,1]' AS description
    FROM {{ ref('mapear_events') }}
    WHERE (resolution_confidence IS NOT NULL AND (resolution_confidence < 0 OR resolution_confidence > 1))
       OR (sentiment_confidence  IS NOT NULL AND (sentiment_confidence  < 0 OR sentiment_confidence  > 1))
       OR (sentiment_overall     IS NOT NULL AND (sentiment_overall     < -1 OR sentiment_overall     > 1))
),

-- 3a. Enriquecimento — entidades excessivas (proxy de stoplist leak) --------
chk_stoplist_entities AS (
    SELECT
        'enrichment'            AS category,
        'stoplist_entity_leak'  AS check_name,
        COUNT(*)                AS failing_rows,
        (SELECT COUNT(*) FROM {{ ref('mapear_events') }}) AS total_rows,
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'WARN' END AS status,
        CONCAT(
            'Documentos com >',
            CAST({{ t.max_entity_mentions_per_doc }} AS VARCHAR),
            ' menções de entidades — possível vazamento de stoplist'
        ) AS description
    FROM {{ ref('mapear_events') }}
    WHERE (
        COALESCE(ARRAY_LENGTH(mentioned_persons), 0)
        + COALESCE(ARRAY_LENGTH(mentioned_cities), 0)
        + COALESCE(ARRAY_LENGTH(mentioned_parties), 0)
    ) > {{ t.max_entity_mentions_per_doc }}
),

-- 3b. Enriquecimento — cobertura de cidades por source_type ----------------
chk_city_coverage AS (
    SELECT
        'enrichment'                    AS category,
        CONCAT('city_coverage_', source_type) AS check_name,
        SUM(CASE WHEN COALESCE(ARRAY_LENGTH(mentioned_cities), 0) = 0 THEN 1 ELSE 0 END) AS failing_rows,
        COUNT(*)                        AS total_rows,
        CASE
            WHEN COUNT(*) < {{ t.min_rows_coverage_check }} THEN 'SKIP'
            WHEN SUM(CASE WHEN COALESCE(ARRAY_LENGTH(mentioned_cities), 0) = 0 THEN 1 ELSE 0 END) * 1.0
                 / COUNT(*) >
                 CASE source_type
                     WHEN 'rss'     THEN 1.0 - {{ t.min_city_coverage_pct_rss }}
                     WHEN 'social'  THEN 1.0 - {{ t.min_city_coverage_pct_social }}
                     ELSE 1.0
                 END
            THEN 'WARN'
            ELSE 'PASS'
        END AS status,
        'Taxa de documentos sem cidades mencionadas acima do limite (regressão NER)' AS description
    FROM {{ ref('mapear_events') }}
    GROUP BY source_type
),

-- 3c. Enriquecimento — cobertura de pessoas em conteúdo in-scope -----------
chk_mayor_coverage AS (
    SELECT
        'enrichment'                AS category,
        'person_coverage_inscope'   AS check_name,
        SUM(CASE WHEN COALESCE(ARRAY_LENGTH(mentioned_persons), 0) = 0 THEN 1 ELSE 0 END) AS failing_rows,
        COUNT(*)                    AS total_rows,
        CASE
            WHEN COUNT(*) < {{ t.min_rows_coverage_check }} THEN 'SKIP'
            WHEN SUM(CASE WHEN COALESCE(ARRAY_LENGTH(mentioned_persons), 0) = 0 THEN 1 ELSE 0 END)
                 * 1.0 / COUNT(*) > (1.0 - {{ t.min_mayor_coverage_pct }})
            THEN 'WARN'
            ELSE 'PASS'
        END AS status,
        CONCAT(
            'Conteúdo in-scope sem pessoas mencionadas (mín ',
            CAST(CAST(ROUND({{ t.min_mayor_coverage_pct }} * 100) AS INTEGER) AS VARCHAR),
            '% de cobertura esperada)'
        ) AS description
    FROM {{ ref('mapear_events') }}
    WHERE author_in_scope = TRUE
),

-- 4. Temporal — violação de cutoff incremental ------------------------------
chk_temporal_cutoff AS (
    SELECT
        'temporal'              AS category,
        'incremental_cutoff'    AS check_name,
        COUNT(*)                AS failing_rows,
        (SELECT COUNT(*) FROM {{ ref('mapear_events') }}) AS total_rows,
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS status,
        CONCAT('Eventos com published_at < ', '{{ t.temporal_cutoff }}', ' violam filtro de watermark') AS description
    FROM {{ ref('mapear_events') }}
    WHERE published_at < CAST('{{ t.temporal_cutoff }}' AS TIMESTAMP)
),

-- 5a. Distribuição — variância de resolution_confidence --------------------
chk_dist_resolution AS (
    SELECT
        'distribution'                      AS category,
        'resolution_confidence_variance'    AS check_name,
        0                                   AS failing_rows,
        COUNT(*)                            AS total_rows,
        CASE
            WHEN COUNT(*) < {{ t.min_rows_distribution_check }} THEN 'SKIP'
            WHEN STDDEV(resolution_confidence) < {{ t.min_resolution_confidence_stddev }} THEN 'WARN'
            ELSE 'PASS'
        END AS status,
        CONCAT(
            'STDDEV(resolution_confidence)=',
            CAST(ROUND(COALESCE(STDDEV(resolution_confidence), 0), 4) AS VARCHAR),
            ' (mínimo esperado: {{ t.min_resolution_confidence_stddev }})'
        ) AS description
    FROM {{ ref('mapear_events') }}
    WHERE resolution_confidence IS NOT NULL
),

-- 5b. Distribuição — variância de sentiment_confidence ---------------------
chk_dist_sentiment AS (
    SELECT
        'distribution'                      AS category,
        'sentiment_confidence_variance'     AS check_name,
        0                                   AS failing_rows,
        COUNT(*)                            AS total_rows,
        CASE
            WHEN COUNT(*) < {{ t.min_rows_distribution_check }} THEN 'SKIP'
            WHEN STDDEV(sentiment_confidence) < {{ t.min_sentiment_confidence_stddev }} THEN 'WARN'
            ELSE 'PASS'
        END AS status,
        CONCAT(
            'STDDEV(sentiment_confidence)=',
            CAST(ROUND(COALESCE(STDDEV(sentiment_confidence), 0), 4) AS VARCHAR),
            ' (mínimo esperado: {{ t.min_sentiment_confidence_stddev }})'
        ) AS description
    FROM {{ ref('mapear_events') }}
    WHERE sentiment_confidence IS NOT NULL
),

-- 6a. Consistência — author_in_scope=TRUE → person_id IS NOT NULL ----------
chk_inscope_person AS (
    SELECT
        'consistency'               AS category,
        'inscope_requires_person_id' AS check_name,
        COUNT(*)                    AS failing_rows,
        (SELECT COUNT(*) FROM {{ ref('mapear_events') }}) AS total_rows,
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS status,
        'author_in_scope=TRUE sem person_id — IN_SCOPE implica resolução de identidade bem-sucedida' AS description
    FROM {{ ref('mapear_events') }}
    WHERE author_in_scope = TRUE
      AND person_id IS NULL
),

-- 6b. Consistência — sentiment_label e sentiment_confidence co-existência --
chk_sentiment_pairing AS (
    SELECT
        'consistency'                           AS category,
        'sentiment_label_confidence_coexist'    AS check_name,
        COUNT(*)                                AS failing_rows,
        (SELECT COUNT(*) FROM {{ ref('mapear_events') }}) AS total_rows,
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS status,
        'sentiment_label e sentiment_confidence devem ser ambos NULL ou ambos preenchidos' AS description
    FROM {{ ref('mapear_events') }}
    WHERE (sentiment_label IS NULL) != (sentiment_confidence IS NULL)
),

-- 6c. Consistência — schema_version deve ser 1 (invariante atual) ----------
chk_schema_version AS (
    SELECT
        'consistency'           AS category,
        'schema_version_value'  AS check_name,
        COUNT(*)                AS failing_rows,
        (SELECT COUNT(*) FROM {{ ref('mapear_events') }}) AS total_rows,
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'WARN' END AS status,
        'schema_version != 1 — verificar se houve migração de schema sem atualizar o relatório' AS description
    FROM {{ ref('mapear_events') }}
    WHERE schema_version != 1
)

-- -------------------------------------------------------------------------
-- Resultado final ordenado por severidade, depois por categoria e check
-- -------------------------------------------------------------------------
SELECT
    category,
    check_name,
    status,
    failing_rows,
    total_rows,
    CASE
        WHEN total_rows = 0 THEN NULL
        ELSE ROUND((total_rows - failing_rows) * 100.0 / total_rows, 1)
    END AS pass_rate_pct,
    description,
    CURRENT_TIMESTAMP AS report_generated_at
FROM (
    SELECT * FROM chk_completeness_ids
    UNION ALL SELECT * FROM chk_event_source_alignment
    UNION ALL SELECT * FROM chk_score_ranges
    UNION ALL SELECT * FROM chk_stoplist_entities
    UNION ALL SELECT * FROM chk_city_coverage
    UNION ALL SELECT * FROM chk_mayor_coverage
    UNION ALL SELECT * FROM chk_temporal_cutoff
    UNION ALL SELECT * FROM chk_dist_resolution
    UNION ALL SELECT * FROM chk_dist_sentiment
    UNION ALL SELECT * FROM chk_inscope_person
    UNION ALL SELECT * FROM chk_sentiment_pairing
    UNION ALL SELECT * FROM chk_schema_version
) all_checks
ORDER BY
    CASE status
        WHEN 'FAIL' THEN 1
        WHEN 'WARN' THEN 2
        WHEN 'PASS' THEN 3
        ELSE 4
    END,
    category,
    check_name
