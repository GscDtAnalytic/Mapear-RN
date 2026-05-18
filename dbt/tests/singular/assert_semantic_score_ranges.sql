-- Rows 8, 10 — Validade semântica: scores numéricos top-level em mapear_events.
-- resolution_confidence ∈ [0, 1]
-- sentiment_confidence ∈ [0, 1]
-- sentiment_overall ∈ [-1, 1]
--
-- Escopo: campos top-level em mapear_events apenas. Não inclui risk_score.
-- risk_score em mapear_events vive em metadata_json — JSON_VALUE por run dbt
-- sem consumer downstream que justifique o overhead (GAP_ACEITO, Row 32).
-- Cobertura de risk_score: assert_risk_score_range.sql sobre
-- stg_social__silver_posts (coluna top-level, sem overhead de parsing).
SELECT
    event_id,
    source_type,
    resolution_confidence,
    sentiment_confidence,
    sentiment_overall,
    CASE
        WHEN resolution_confidence IS NOT NULL
             AND (resolution_confidence < 0 OR resolution_confidence > 1)
             THEN 'resolution_confidence'
        WHEN sentiment_confidence IS NOT NULL
             AND (sentiment_confidence < 0 OR sentiment_confidence > 1)
             THEN 'sentiment_confidence'
        WHEN sentiment_overall IS NOT NULL
             AND (sentiment_overall < -1 OR sentiment_overall > 1)
             THEN 'sentiment_overall'
    END AS failed_column
FROM {{ ref('mapear_events') }}
WHERE
    (resolution_confidence IS NOT NULL AND (resolution_confidence < 0 OR resolution_confidence > 1))
    OR (sentiment_confidence IS NOT NULL AND (sentiment_confidence < 0 OR sentiment_confidence > 1))
    OR (sentiment_overall IS NOT NULL AND (sentiment_overall < -1 OR sentiment_overall > 1))
