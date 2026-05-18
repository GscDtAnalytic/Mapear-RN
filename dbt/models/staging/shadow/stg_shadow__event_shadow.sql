/*
    stg_shadow__event_shadow — staging para silver_event_shadow.

    Grain : (content_hash, shadow_rule_version)
    Source: silver_event_shadow (Stage 1E v2)

    A silver física é WRITE_APPEND sem dedup — o pipeline pode reescrever
    o mesmo (content_hash, shadow_rule_version) em runs sucessivos. O
    QUALIFY mantém a classificação mais recente por par.

    severity_delta deriva da ordem FAVORABLE < WARNING < ALERT
    (a mesma do comparador Stage 1E v1, eval/shadow.py):
      > 0  shadow escalou (mais severo que o primário)
      < 0  shadow rebaixou
      = 0  os dois regimes concordam
*/

{{ config(materialized='view') }}

WITH source AS (

    SELECT * FROM {{ source('shadow_silver', 'silver_event_shadow') }}

),

deduped AS (

    SELECT *
    FROM source
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY content_hash, shadow_rule_version
        ORDER BY processed_at_utc DESC
    ) = 1

),

severity AS (

    SELECT
        content_hash,
        shadow_rule_version,
        primary_rule_version,
        source_type,
        region,
        tenant_id,
        person_id,
        polarity,
        volume_24h,
        velocity,
        engagement,
        recurrence,
        primary_label,
        primary_confidence,
        primary_risk_score,
        shadow_label,
        shadow_confidence,
        shadow_risk_score,
        model_version,
        pipeline_version,
        processed_at_utc,
        CAST(processed_at_utc AS DATE) AS shadow_date,
        CASE primary_label
            WHEN 'FAVORABLE' THEN 0
            WHEN 'WARNING'   THEN 1
            WHEN 'ALERT'     THEN 2
        END AS primary_severity,
        CASE shadow_label
            WHEN 'FAVORABLE' THEN 0
            WHEN 'WARNING'   THEN 1
            WHEN 'ALERT'     THEN 2
        END AS shadow_severity
    FROM deduped

)

SELECT
    *,
    shadow_severity - primary_severity            AS severity_delta,
    (primary_label = shadow_label)                AS regimes_agree,
    shadow_confidence - primary_confidence         AS confidence_shift,
    shadow_risk_score - primary_risk_score         AS risk_shift
FROM severity
