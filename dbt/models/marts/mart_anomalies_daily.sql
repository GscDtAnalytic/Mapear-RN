{{
    config(
        materialized='incremental',
        unique_key=['person_id', 'day'],
        incremental_strategy='merge',
        partition_by={
            'field': 'day',
            'data_type': 'date',
            'granularity': 'day'
        },
        cluster_by=['person_id'],
        tags=['mart', 'aggregated', 'anomaly']
    )
}}

/*
    mart_anomalies_daily — spike detection diário por person_id.

    Grain   : (person_id, day) — apenas dias com mentions >= 1
    Source  : mapear_events (rn_relevant=TRUE, person_role IN target roles)
    Cobre   : PQ-011

    Lógica:
      - Cross join `dim_persons (target roles, is_current) × dim_dates (lookback)`
      - LEFT JOIN com agregados daily de mapear_events
      - Janela rolling (30d preceding, exclui dia atual) para mean / stddev
      - is_anomaly = (zscore >= var('anomaly_zscore_threshold')) AND (mentions >= var('anomaly_min_mentions'))

    Refresh incremental: rolling stats exigem reabsorver últimos 31 dias para
    contexto da janela. O merge por (person_id, day) garante idempotência.
*/

{% set lookback_days     = var('anomaly_lookback_days', 90) %}
{% set zscore_threshold  = var('anomaly_zscore_threshold', 2.0) %}
{% set min_mentions      = var('anomaly_min_mentions', 3) %}

WITH target_persons AS (

    SELECT
        person_id,
        name        AS person_name,
        role        AS person_role
    FROM {{ ref('dim_persons') }}
    WHERE is_current = TRUE
      AND role IN ('governor', 'governor_candidate', 'mayor')

),

calendar AS (

    SELECT date_day AS day
    FROM {{ ref('dim_dates') }}
    WHERE date_day >= {{ dbt.dateadd('day', -1 * (lookback_days + 30), 'CURRENT_DATE') }}
      AND date_day <= CURRENT_DATE

),

daily_counts AS (

    SELECT
        person_id,
        CAST(published_at AS DATE)  AS day,
        COUNT(*)                    AS mentions
    FROM {{ ref('mapear_events') }}
    WHERE rn_relevant = TRUE
      AND person_id IS NOT NULL
      AND CAST(published_at AS DATE) >= {{ dbt.dateadd('day', -1 * (lookback_days + 30), 'CURRENT_DATE') }}
    GROUP BY person_id, CAST(published_at AS DATE)

),

grid AS (

    SELECT
        p.person_id,
        p.person_name,
        p.person_role,
        c.day,
        COALESCE(dc.mentions, 0)    AS mentions
    FROM target_persons    AS p
    CROSS JOIN calendar    AS c
    LEFT JOIN daily_counts AS dc
        ON dc.person_id = p.person_id AND dc.day = c.day

),

windowed AS (

    SELECT
        person_id,
        person_name,
        person_role,
        day,
        mentions,
        AVG(mentions) OVER (
            PARTITION BY person_id
            ORDER BY day
            ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING
        )                           AS rolling_mean_30d,
        STDDEV(mentions) OVER (
            PARTITION BY person_id
            ORDER BY day
            ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING
        )                           AS rolling_sd_30d
    FROM grid

),

scored AS (

    SELECT
        person_id,
        person_name,
        person_role,
        day,
        mentions,
        rolling_mean_30d,
        rolling_sd_30d,
        CASE
            WHEN rolling_sd_30d IS NULL OR rolling_sd_30d = 0 THEN NULL
            ELSE (mentions - rolling_mean_30d) / rolling_sd_30d
        END                         AS zscore
    FROM windowed
    -- Drop dias sem menção (grain útil = dias que o person apareceu)
    WHERE mentions >= 1
      -- Dropar janela inicial sem 30d de contexto
      AND day >= {{ dbt.dateadd('day', -1 * lookback_days, 'CURRENT_DATE') }}

),

flagged AS (

    SELECT
        person_id,
        person_name,
        person_role,
        day,
        mentions,
        rolling_mean_30d,
        rolling_sd_30d,
        zscore,
        CASE
            WHEN zscore IS NULL                   THEN FALSE
            WHEN mentions < {{ min_mentions }}    THEN FALSE
            WHEN zscore >= {{ zscore_threshold }} THEN TRUE
            ELSE FALSE
        END                         AS is_anomaly,
        CURRENT_TIMESTAMP           AS last_refreshed_at
    FROM scored

    {% if is_incremental() %}
    WHERE day >= {{ dbt.dateadd('day', -31, 'CURRENT_DATE') }}
    {% endif %}

)

SELECT * FROM flagged
