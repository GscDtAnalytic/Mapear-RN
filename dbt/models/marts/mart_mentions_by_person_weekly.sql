{{
    config(
        materialized='incremental',
        unique_key=['person_id', 'week_start'],
        incremental_strategy='merge',
        partition_by={
            'field': 'week_start',
            'data_type': 'date',
            'granularity': 'day'
        },
        cluster_by=['person_id'],
        tags=['mart', 'aggregated', 'temporal']
    )
}}

/*
    mart_mentions_by_person_weekly — agregação semanal por pessoa.

    Grain : (person_id, week_start)
    Source: mapear_events (rn_relevant=TRUE, person_id IS NOT NULL)
    Cobre : PQ-009 (tendência semanal de menções por candidato/prefeito)

    Caveat herdado de C3.1: sentiment_label em RSS é polarity-only (FAVORABLE/WARNING),
    nunca ALERT. n_alert é dominado por social.
*/

WITH events_window AS (

    SELECT
        e.person_id,
        e.published_at,
        e.source_type,
        e.sentiment_overall,
        e.sentiment_label
    FROM {{ ref('mapear_events') }} AS e
    WHERE e.rn_relevant = TRUE
      AND e.person_id IS NOT NULL

    {% if is_incremental() %}
      AND CAST(e.published_at AS DATE) >= (
          SELECT {{ dbt.dateadd('day', -7, 'COALESCE(MAX(week_start), DATE \'1970-01-01\')') }}
          FROM {{ this }}
      )
    {% endif %}

),

aggregated AS (

    SELECT
        person_id,
        {% if target.type == 'bigquery' -%}
        DATE_TRUNC(DATE(published_at), ISOWEEK)
        {%- else -%}
        DATE_TRUNC('week', CAST(published_at AS DATE))
        {%- endif %}                                                        AS week_start,
        COUNT(*)                                                            AS mentions_total,
        SUM(CASE WHEN source_type = 'rss'    THEN 1 ELSE 0 END)             AS mentions_rss,
        SUM(CASE WHEN source_type = 'social' THEN 1 ELSE 0 END)             AS mentions_social,
        AVG(sentiment_overall)                                              AS avg_sentiment_overall,
        SUM(CASE WHEN sentiment_label = 'FAVORABLE' THEN 1 ELSE 0 END)      AS n_favorable,
        SUM(CASE WHEN sentiment_label = 'WARNING'   THEN 1 ELSE 0 END)      AS n_warning,
        SUM(CASE WHEN sentiment_label = 'ALERT'     THEN 1 ELSE 0 END)      AS n_alert
    FROM events_window
    GROUP BY person_id, week_start

),

with_dims AS (

    SELECT
        a.person_id,
        p.name              AS person_name,
        p.role              AS person_role,
        a.week_start,
        d.electoral_phase,
        a.mentions_total,
        a.mentions_rss,
        a.mentions_social,
        a.avg_sentiment_overall,
        a.n_favorable,
        a.n_warning,
        a.n_alert,
        CURRENT_TIMESTAMP   AS last_refreshed_at
    FROM aggregated AS a
    LEFT JOIN {{ ref('dim_persons') }} AS p
        ON a.person_id = p.person_id AND p.is_current = TRUE
    LEFT JOIN {{ ref('dim_dates') }} AS d
        ON a.week_start = d.date_day

)

SELECT * FROM with_dims
