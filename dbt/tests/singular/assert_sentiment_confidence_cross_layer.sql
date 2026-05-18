{{ config(severity='error') }}
-- Regressão: alias confidence_score AS sentiment_confidence em mapear_events.
-- B1_VALIDATED 2026-05-04 (N=362, 0 divergentes, 0 perdidos em trânsito).
-- Se falhar: alias foi quebrado em mapear_events.sql — investigar imediatamente.

SELECT
    me.event_id,
    me.sentiment_confidence,
    ssp.confidence_score
FROM {{ ref('mapear_events') }} me
JOIN {{ ref('stg_social__silver_posts') }} ssp
    ON me.event_id = ssp.post_id
WHERE me.source_type != 'rss'
  AND ssp.confidence_score IS NOT NULL
  AND (
    ABS(me.sentiment_confidence - ssp.confidence_score) > 0.0001
    OR (me.sentiment_confidence IS NULL AND ssp.confidence_score IS NOT NULL)
  )
