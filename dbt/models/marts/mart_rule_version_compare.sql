{{
    config(
        materialized='incremental',
        unique_key=['shadow_date', 'region', 'source_type', 'primary_rule_version', 'shadow_rule_version'],
        incremental_strategy='merge',
        partition_by={
            'field': 'shadow_date',
            'data_type': 'date',
            'granularity': 'day'
        },
        cluster_by=['source_type', 'shadow_rule_version'],
        tags=['mart', 'aggregated', 'shadow', 'stage-1e', 'mlops']
    )
}}

/*
    mart_rule_version_compare — comparação A/B diária primary vs shadow.

    Grain : (shadow_date, region, source_type, primary_rule_version,
             shadow_rule_version)
    Source: stg_shadow__event_shadow (silver_event_shadow)
    Cobre : Stage 1E v2 — permite ao operador navegar o shadow contínuo
            em SQL/Looker sem rodar o comparador Python pontual.

    Métricas por partição:
      - n_events        — eventos classificados sob os dois regimes
      - agreed          — regimes concordam (mesmo label)
      - escalated       — shadow mais severo que primary (FAV<WARN<ALERT)
      - demoted         — shadow menos severo
      - {label}_primary / {label}_shadow — distribuição de label por regime
      - mean_confidence_shift / mean_risk_shift — deslocamento médio

    Cross-dialect: só agregações simples + CASE + AVG — roda em DuckDB
    (dev) e BigQuery (prod) sem rewrite.
*/

WITH source AS (

    SELECT * FROM {{ ref('stg_shadow__event_shadow') }}

    {% if is_incremental() %}
      WHERE shadow_date >= (
          SELECT COALESCE(MAX(shadow_date), DATE '1970-01-01')
          FROM {{ this }}
      )
    {% endif %}

)

SELECT
    shadow_date,
    region,
    source_type,
    primary_rule_version,
    shadow_rule_version,
    COUNT(*)                                                  AS n_events,
    SUM(CASE WHEN regimes_agree THEN 1 ELSE 0 END)             AS agreed,
    SUM(CASE WHEN severity_delta > 0 THEN 1 ELSE 0 END)        AS escalated,
    SUM(CASE WHEN severity_delta < 0 THEN 1 ELSE 0 END)        AS demoted,
    SUM(CASE WHEN primary_label = 'FAVORABLE' THEN 1 ELSE 0 END) AS favorable_primary,
    SUM(CASE WHEN primary_label = 'WARNING'   THEN 1 ELSE 0 END) AS warning_primary,
    SUM(CASE WHEN primary_label = 'ALERT'     THEN 1 ELSE 0 END) AS alert_primary,
    SUM(CASE WHEN shadow_label = 'FAVORABLE'  THEN 1 ELSE 0 END) AS favorable_shadow,
    SUM(CASE WHEN shadow_label = 'WARNING'    THEN 1 ELSE 0 END) AS warning_shadow,
    SUM(CASE WHEN shadow_label = 'ALERT'      THEN 1 ELSE 0 END) AS alert_shadow,
    AVG(confidence_shift)                                     AS mean_confidence_shift,
    AVG(risk_shift)                                           AS mean_risk_shift
FROM source
GROUP BY
    shadow_date,
    region,
    source_type,
    primary_rule_version,
    shadow_rule_version
