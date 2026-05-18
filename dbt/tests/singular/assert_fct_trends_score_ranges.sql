-- Rows 17 + 37 — Ranges e ordenação composta de sentimento em fct_trends.
--
-- Dois invariantes testados em conjunto sobre fct_trends:
--
-- (A) Range avg_sentiment ∈ [-1, 1] (Row 17):
--     avg_sentiment = ROUND(AVG(sentiment), 4) sobre fct_entity_sentiment.
--     Como sentiment ∈ [-1, 1] (garantido por assert_semantic_score_ranges.sql),
--     a média é matematicamente bounded. Teste preventivo: detecta refatoração
--     que troque a fonte de avg_sentiment por campo sem range garantido.
--
-- (B) Ordenação composta min ≤ avg ≤ max (Row 37):
--     min_sentiment = ROUND(MIN(sentiment), 4), max_sentiment = ROUND(MAX(sentiment), 4)
--     (fct_trends.sql:27-28). Matematicamente garantido pelo SQL de origem,
--     mas um teste de baixo custo que detecta troca acidental de MIN/MAX na
--     agregação ou refatoração que quebre a semântica dos campos.
--
-- Universo: fct_trends WHERE avg/min/max IS NOT NULL (excluir trends sem
-- sentimento calculado — condição normal quando nenhum evento do trend
-- passou pelo PoliticalSentimentClassifier).
--
-- Severidade B (warn): ambos são preventivos; violação indica bug de
-- refatoração, não corrupção de dados de produção atual.
{{ config(severity='warn') }}

SELECT
    entity,
    entity_type,
    avg_sentiment,
    min_sentiment,
    max_sentiment,
    CASE
        WHEN avg_sentiment < -1 OR avg_sentiment > 1
            THEN 'avg_out_of_range'
        WHEN min_sentiment > avg_sentiment OR max_sentiment < avg_sentiment
            THEN 'ordering_violated'
    END AS violation_type
FROM {{ ref('fct_trends') }}
WHERE
    avg_sentiment IS NOT NULL
    AND min_sentiment IS NOT NULL
    AND max_sentiment IS NOT NULL
    AND (
        avg_sentiment < -1
        OR avg_sentiment > 1
        OR min_sentiment > avg_sentiment
        OR max_sentiment < avg_sentiment
    )
