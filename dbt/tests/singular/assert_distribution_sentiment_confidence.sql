{{ config(severity='warn') }}
-- Distribuição: alerta quando sentiment_confidence tem variância próxima de zero.
-- stddev quase nulo indica classificador de sentimento político degenerado
-- (retorna sempre o mesmo score de confiança, independente do conteúdo).
{% set t = quality_thresholds() %}

SELECT
    'sentiment_confidence' AS metric,
    ROUND(STDDEV(sentiment_confidence), 6) AS actual_stddev,
    {{ t.min_sentiment_confidence_stddev }} AS min_stddev,
    COUNT(*) AS sample_size
FROM {{ ref('mapear_events') }}
WHERE sentiment_confidence IS NOT NULL
HAVING COUNT(*) >= {{ t.min_rows_distribution_check }}
   AND STDDEV(sentiment_confidence) < {{ t.min_sentiment_confidence_stddev }}
