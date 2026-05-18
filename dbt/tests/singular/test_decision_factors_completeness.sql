{#-
    Every silver social post must carry at least one decision factor
    from the PoliticalSentimentClassifier (DoD-5 / BL-F2-05).

    Runs on silver_social_posts (not gold) because decision_factors is
    a REPEATED STRUCT that lives in silver; gold surfaces only the scalar
    summary (sentiment_label, risk_score, confidence_score).

    Violations indicate a classification bug or a pipeline run where the
    classifier was disabled (political_sentiment_enabled=false).
-#}

SELECT
    post_id,
    platform,
    sentiment_label,
    decision_factors
FROM {{ ref('stg_social__silver_posts') }}
WHERE scope_status = 'IN_SCOPE'
  AND (
    -- NULL decision_factors or empty array (DuckDB: len=0, BQ: ARRAY_LENGTH=0)
    decision_factors IS NULL
    {% if target.type == 'duckdb' %}
        OR len(decision_factors) = 0
    {% else %}
        OR ARRAY_LENGTH(decision_factors) = 0
    {% endif %}
  )
