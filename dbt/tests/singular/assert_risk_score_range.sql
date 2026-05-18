-- Row 32 — Range [0, 1] de risk_score em stg_social__silver_posts.
--
-- risk_score = 0.5*max(-polarity,0) + 0.3*velocity + 0.2*log_engagement
-- (political_sentiment.py:313-326). Cada componente está em [0,1] e os
-- pesos somam 1.0 — matematicamente bounded. Teste detecta bug de
-- implementação que produza componente fora de range ou peso incorreto.
--
-- Testado em stg_social__silver_posts (coluna top-level, sem overhead de
-- JSON parsing). Em mapear_events, risk_score vive em metadata_json —
-- GAP_ACEITO por ausência de consumer downstream que justifique o custo
-- de JSON_VALUE por run dbt (Row 32 do framework).
--
-- Social-only por design: RSS não executa PoliticalSentimentClassifier
-- e não possui os campos de engajamento necessários.
--
-- Severidade M (warn): violação indica bug de pipeline real. Não escala
-- para 'error' — risk_score não é campo de bloqueio de qualidade atual.
{{ config(severity='warn') }}

SELECT
    post_id,
    source_type,
    platform,
    risk_score
FROM {{ ref('stg_social__silver_posts') }}
WHERE
    risk_score IS NOT NULL
    AND (risk_score < 0 OR risk_score > 1)
