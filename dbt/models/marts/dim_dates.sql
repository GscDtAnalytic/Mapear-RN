{{
    config(
        materialized='table',
        tags=['dimension', 'calendar']
    )
}}

/*
    Calendar dimension — janela 2024-01-01 a 2027-12-31 (cobre coleta histórica
    e ciclo eleitoral 2026). Geração via dbt_utils.date_spine (cross-dialect).

    Usado por marts agregados temporais (mart_mentions_by_person_weekly,
    mart_anomalies_daily) e PQs com filtros calendar-aware (PQ-009, PQ-010, PQ-012).

    Feriados e fase eleitoral em macros/holiday_flags.sql.
*/

WITH spine AS (

    {{ dbt_utils.date_spine(
        datepart="day",
        start_date="cast('2024-01-01' as date)",
        end_date="cast('2028-01-01' as date)"
    ) }}

),

calendar AS (

    SELECT
        CAST(date_day AS DATE)                                          AS date_day,
        EXTRACT(YEAR    FROM date_day)                                  AS year,
        EXTRACT(MONTH   FROM date_day)                                  AS month,
        EXTRACT(QUARTER FROM date_day)                                  AS quarter,
        EXTRACT(WEEK    FROM date_day)                                  AS week_iso,
        {% if target.type == 'bigquery' -%}
        DATE_TRUNC(CAST(date_day AS DATE), ISOWEEK)
        {%- else -%}
        DATE_TRUNC('week', CAST(date_day AS DATE))
        {%- endif %}                                                    AS week_start_monday,
        {% if target.type == 'bigquery' -%}
        FORMAT_DATE('%B', CAST(date_day AS DATE))
        {%- else -%}
        STRFTIME(CAST(date_day AS DATE), '%B')
        {%- endif %}                                                    AS month_name,
        {% if target.type == 'bigquery' -%}
        MOD(EXTRACT(DAYOFWEEK FROM date_day) + 5, 7)
        {%- else -%}
        ((EXTRACT(DOW FROM date_day) + 6) % 7)
        {%- endif %}                                                    AS dow_num,
        {% if target.type == 'bigquery' -%}
        FORMAT_DATE('%A', CAST(date_day AS DATE))
        {%- else -%}
        STRFTIME(CAST(date_day AS DATE), '%A')
        {%- endif %}                                                    AS dow_name,
        {% if target.type == 'bigquery' -%}
        EXTRACT(DAYOFWEEK FROM date_day) IN (1, 7)
        {%- else -%}
        EXTRACT(DOW FROM date_day) IN (0, 6)
        {%- endif %}                                                    AS is_weekend,
        {{ is_holiday_br('date_day') }}                                 AS is_holiday_br,
        {{ is_holiday_rn('date_day') }}                                 AS is_holiday_rn,
        {{ electoral_phase('date_day') }}                               AS electoral_phase
    FROM spine

)

SELECT * FROM calendar
ORDER BY date_day
