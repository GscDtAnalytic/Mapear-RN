/*
    Cobertura sentiment_label em RSS rn_relevant nos últimos 30 dias.
    SLO C3.1 / BL-F2-05: ≥ 95% (miss_rate ≤ 5%).

    Falha (test fail) se mais de 5% dos artigos RSS rn_relevant nos últimos
    30 dias estiverem sem sentiment_label. Pós-backfill 90d, esperado é 0%.
    Após cutover, cobertura próxima de 100% indica pipeline saudável.

    Severity warn: até o pipeline rodar em prod com a Stage 4.5 ativa por
    >24h (mínimo um ciclo de Cloud Scheduler), promover para error.
*/
{{ config(severity='warn') }}

WITH coverage AS (
    SELECT
        COUNT(*) AS total,
        SUM(CASE WHEN sentiment_label IS NULL THEN 1 ELSE 0 END) AS missing
    FROM {{ ref('mapear_events') }}
    WHERE source_type = 'rss'
      AND content_rn_relevant = TRUE
      AND published_at >= CAST({{ dbt.dateadd('day', -30, 'CURRENT_TIMESTAMP') }} AS TIMESTAMP)
)

SELECT
    total,
    missing,
    CAST(missing AS {{ dbt.type_float() }}) / NULLIF(total, 0) AS miss_rate
FROM coverage
WHERE total > 0
  AND CAST(missing AS {{ dbt.type_float() }}) / NULLIF(total, 0) > 0.05
