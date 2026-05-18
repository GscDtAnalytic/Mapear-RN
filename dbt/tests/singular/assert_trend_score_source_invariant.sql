-- Row 15 — Invariante de fonte: trend_score DEVE ser NULL para RSS.
--
-- trend_score = LOG(1 + likes + comments*2 + shares*3): depende de campos de
-- engajamento que RSS não possui. mapear_events.sql propaga NULL para RSS por
-- design. Um valor não-NULL em source_type='rss' indica vazamento do pipeline
-- Social para RSS (ex: JOIN errado ou UNION com mismatch de colunas).
--
-- O range (>= 0) já é coberto por assert_semantic_score_ranges.sql:28-29.
-- Este arquivo cobre apenas a invariante de fonte (NULL-RSS), que estava ausente.
--
-- Severidade M (warn): violação indica bug de pipeline real, não condição
-- esperada. Não escala para 'error' pois o range complementar em
-- assert_semantic_score_ranges.sql já é warn — manter consistência de severity
-- no mesmo campo.
{{ config(severity='warn') }}

SELECT
    event_id,
    source_type,
    platform,
    trend_score
FROM {{ ref('mapear_events') }}
WHERE
    platform = 'rss'
    AND trend_score IS NOT NULL
