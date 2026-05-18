{{ config(severity='warn') }}
-- Distribuição: alerta quando resolution_confidence tem variância próxima de zero.
-- stddev quase nulo indica modelo de resolução degenerado (sempre retorna o mesmo score).
-- Requer amostra mínima de dq_min_rows_distribution_check linhas antes de checar.
{% set t = quality_thresholds() %}

SELECT
    'resolution_confidence' AS metric,
    ROUND(STDDEV(resolution_confidence), 6) AS actual_stddev,
    {{ t.min_resolution_confidence_stddev }} AS min_stddev,
    COUNT(*) AS sample_size
FROM {{ ref('mapear_events') }}
WHERE resolution_confidence IS NOT NULL
HAVING COUNT(*) >= {{ t.min_rows_distribution_check }}
   AND STDDEV(resolution_confidence) < {{ t.min_resolution_confidence_stddev }}
