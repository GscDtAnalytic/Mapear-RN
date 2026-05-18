{#-
    fct_content_gold must be a strict filter of silver: every row in Gold v2
    must correspond to a silver row with (is_rn_relevant = TRUE,
    scope_status = 'IN_SCOPE', person_id IS NOT NULL). Any row that landed
    in Gold v2 while its silver source says otherwise is a contract
    violation — either the gold materialization skipped the filter or
    silver was mutated after gold built.

    Covers BL-RESTRUCT Fase 1 — electoral scope gate. Fails (returns rows)
    if a content_id in fct_content_gold cannot be reconciled with
    IN_SCOPE silver for the same source_type.
-#}

WITH gold AS (

    SELECT
        content_id,
        source_type,
        person_id,
        scope_status AS gold_scope_status
    FROM {{ ref('fct_content_gold') }}

),

rss_silver AS (

    SELECT
        content_hash AS content_id,
        'rss' AS source_type,
        is_rn_relevant,
        scope_status,
        person_id
    FROM {{ ref('stg_rss__silver_articles') }}

),

social_silver AS (

    SELECT
        post_id AS content_id,
        'social' AS source_type,
        is_rn_relevant,
        scope_status,
        person_id
    FROM {{ ref('stg_social__silver_posts') }}

),

silver AS (

    SELECT * FROM rss_silver
    UNION ALL
    SELECT * FROM social_silver

),

violations AS (

    SELECT
        g.content_id,
        g.source_type,
        g.person_id AS gold_person_id,
        g.gold_scope_status,
        s.scope_status AS silver_scope_status,
        s.is_rn_relevant AS silver_is_rn_relevant,
        s.person_id AS silver_person_id,
        CASE
            WHEN s.content_id IS NULL THEN 'silver_row_missing'
            WHEN s.is_rn_relevant IS NOT TRUE THEN 'silver_not_rn_relevant'
            WHEN s.scope_status IS DISTINCT FROM 'IN_SCOPE' THEN 'silver_not_in_scope'
            WHEN s.person_id IS NULL THEN 'silver_person_id_null'
            ELSE 'unknown_mismatch'
        END AS failure_reason
    FROM gold AS g
    LEFT JOIN silver AS s
        ON g.content_id = s.content_id
        AND g.source_type = s.source_type
    WHERE
        s.content_id IS NULL
        OR s.is_rn_relevant IS NOT TRUE
        OR s.scope_status IS DISTINCT FROM 'IN_SCOPE'
        OR s.person_id IS NULL

)

SELECT * FROM violations
