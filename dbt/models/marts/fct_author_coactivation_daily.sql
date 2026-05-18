{{
    config(
        materialized='incremental',
        unique_key=['activation_date', 'region', 'author_a_key', 'author_b_key'],
        incremental_strategy='merge',
        partition_by={
            'field': 'activation_date',
            'data_type': 'date',
            'granularity': 'day'
        },
        cluster_by=['region', 'author_a_key'],
        tags=['mart', 'aggregated', 'cib', 'eixo-3']
    )
}}

/*
    fct_author_coactivation_daily — co-activation diária entre pares de autores.

    Grain : (activation_date, region, author_a_key, author_b_key)
    Source: stg_social__author_activations (silver_author_activations)
    Cobre : Eixo 3 v1 — foundation para CIB detection.

    O par é simétrico mas canonicaliza ordenando (platform, author_id):
    (author_a_key, author_b_key) com author_a_key < author_b_key. Nunca
    emite a tupla inversa.

    Métricas por dia:
      - co_post_count       : nº de targets distintos que ambos ativaram no mesmo dia
      - shared_targets      : lista dos targets compartilhados (ARRAY<STRING>)
      - first_co_activation : timestamp do primeiro co-fire no dia (qualquer target)
      - last_co_activation  : timestamp do último co-fire no dia

    Nota cross-dialect:
      - ARRAY_AGG é portátil entre DuckDB e BigQuery.
      - DISTINCT é necessário porque, dentro de um dia, dois autores podem
        ativar o mesmo person_target em múltiplos posts; queremos targets
        distintos, não posts.
      - tenant_id/region são preservados; o join garante que só conta
        co-activations dentro do mesmo (region, tenant_id).
*/

WITH activations AS (

    SELECT
        author_id,
        platform,
        author_id || '|' || platform                                        AS author_key,
        post_id,
        person_target,
        published_at,
        CAST(published_at AS DATE)                                          AS activation_date,
        region,
        tenant_id
    FROM {{ ref('stg_social__author_activations') }}
    WHERE author_in_scope = TRUE

    {% if is_incremental() %}
      AND CAST(published_at AS DATE) >= (
          SELECT COALESCE(MAX(activation_date), DATE '1970-01-01')
          FROM {{ this }}
      )
    {% endif %}

),

-- One row per author × person_target × day. Collapses multiple posts
-- by the same author against the same target on the same day.
distinct_author_target_day AS (

    SELECT DISTINCT
        author_key,
        author_id,
        platform,
        person_target,
        activation_date,
        region,
        COALESCE(tenant_id, '__default__')                                  AS tenant_bucket
    FROM activations

),

-- Self-join on (activation_date, person_target, region, tenant_bucket).
-- The strict < ordering on author_key keeps each pair once and excludes
-- self-pairs.
pairs_raw AS (

    SELECT
        a.activation_date,
        a.region,
        a.tenant_bucket,
        a.author_key                                                        AS author_a_key,
        a.author_id                                                         AS author_a_id,
        a.platform                                                          AS author_a_platform,
        b.author_key                                                        AS author_b_key,
        b.author_id                                                         AS author_b_id,
        b.platform                                                          AS author_b_platform,
        a.person_target
    FROM distinct_author_target_day AS a
    INNER JOIN distinct_author_target_day AS b
        ON a.activation_date = b.activation_date
       AND a.region          = b.region
       AND a.tenant_bucket   = b.tenant_bucket
       AND a.person_target   = b.person_target
       AND a.author_key      < b.author_key

),

-- Per-pair, per-day rollup with shared_targets array and co-fire counts.
aggregated AS (

    SELECT
        activation_date,
        region,
        author_a_key,
        author_a_id,
        author_a_platform,
        author_b_key,
        author_b_id,
        author_b_platform,
        NULLIF(tenant_bucket, '__default__')                                AS tenant_id,
        COUNT(DISTINCT person_target)                                       AS co_post_count,
        ARRAY_AGG(DISTINCT person_target)                                   AS shared_targets
    FROM pairs_raw
    GROUP BY
        activation_date,
        region,
        author_a_key,
        author_a_id,
        author_a_platform,
        author_b_key,
        author_b_id,
        author_b_platform,
        tenant_bucket

),

with_timestamps AS (

    SELECT
        agg.activation_date,
        agg.region,
        agg.tenant_id,
        agg.author_a_key,
        agg.author_a_id,
        agg.author_a_platform,
        agg.author_b_key,
        agg.author_b_id,
        agg.author_b_platform,
        agg.co_post_count,
        agg.shared_targets,
        MIN(act.published_at)                                               AS first_co_activation,
        MAX(act.published_at)                                               AS last_co_activation,
        CURRENT_TIMESTAMP                                                   AS last_refreshed_at
    FROM aggregated AS agg
    -- Bring back the timestamps from the underlying activations to
    -- record the actual first/last co-fire moment within the day.
    INNER JOIN activations AS act
        ON act.activation_date = agg.activation_date
       AND act.region          = agg.region
       AND (
           act.author_id || '|' || act.platform = agg.author_a_key
           OR act.author_id || '|' || act.platform = agg.author_b_key
       )
    GROUP BY
        agg.activation_date,
        agg.region,
        agg.tenant_id,
        agg.author_a_key,
        agg.author_a_id,
        agg.author_a_platform,
        agg.author_b_key,
        agg.author_b_id,
        agg.author_b_platform,
        agg.co_post_count,
        agg.shared_targets

)

SELECT * FROM with_timestamps
