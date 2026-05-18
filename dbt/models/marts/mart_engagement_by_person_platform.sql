{{
    config(
        materialized='incremental',
        unique_key=['person_id', 'platform', 'week_start'],
        incremental_strategy='merge',
        partition_by={
            'field': 'week_start',
            'data_type': 'date',
            'granularity': 'day'
        },
        cluster_by=['person_id', 'platform'],
        tags=['mart', 'aggregated', 'engagement']
    )
}}

/*
    mart_engagement_by_person_platform — engajamento social agregado semanal.

    Grain : (person_id, platform, week_start)
    Source: fct_content_gold (source_type='social' apenas — RSS sem engagement)
    Cobre : PQ-007 (engajamento likes+comments+shares por candidato × plataforma)

    Refresh incremental: reabsorver semana parcial (última segunda - 7 dias).
*/

WITH posts AS (

    SELECT
        person_id,
        platform,
        published_at,
        COALESCE(likes,    0)   AS likes,
        COALESCE(comments, 0)   AS comments,
        COALESCE(shares,   0)   AS shares,
        COALESCE(views,    0)   AS views
    FROM {{ ref('fct_content_gold') }}
    WHERE source_type = 'social'
      AND person_id IS NOT NULL

    {% if is_incremental() %}
      AND CAST(published_at AS DATE) >= (
          SELECT {{ dbt.dateadd('day', -7, 'COALESCE(MAX(week_start), DATE \'1970-01-01\')') }}
          FROM {{ this }}
      )
    {% endif %}

),

aggregated AS (

    SELECT
        person_id,
        platform,
        {% if target.type == 'bigquery' -%}
        DATE_TRUNC(DATE(published_at), ISOWEEK)
        {%- else -%}
        DATE_TRUNC('week', CAST(published_at AS DATE))
        {%- endif %}                                                AS week_start,
        COUNT(*)                                                    AS posts,
        SUM(likes)                                                  AS total_likes,
        SUM(comments)                                               AS total_comments,
        SUM(shares)                                                 AS total_shares,
        SUM(views)                                                  AS total_views,
        SUM(likes + comments + shares)                              AS engagement_total
    FROM posts
    GROUP BY person_id, platform, week_start

),

with_dims AS (

    SELECT
        a.person_id,
        p.name              AS person_name,
        p.role              AS person_role,
        a.platform,
        a.week_start,
        a.posts,
        a.total_likes,
        a.total_comments,
        a.total_shares,
        a.total_views,
        a.engagement_total,
        CASE
            WHEN a.posts = 0 THEN NULL
            ELSE CAST(a.engagement_total AS {{ dbt.type_float() }}) / a.posts
        END                 AS avg_engagement_per_post,
        CURRENT_TIMESTAMP   AS last_refreshed_at
    FROM aggregated AS a
    LEFT JOIN {{ ref('dim_persons') }} AS p
        ON a.person_id = p.person_id AND p.is_current = TRUE

)

SELECT * FROM with_dims
