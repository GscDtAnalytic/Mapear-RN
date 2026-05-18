{#-
    Denormalized person attributes in fct_content_gold (person_name,
    person_role, person_party, person_city, person_is_incumbent) must match
    the current record in dim_persons for that person_id.

    Mismatch signals a stale gold materialization: dim_persons SCD2 closed
    an old record and opened a new one (e.g. party change), but the gold
    fact still carries the old attributes. After the next dbt run the gold
    should refresh; if it doesn't, this test catches it.

    Notes:
      * Skips rows where dim_persons has no is_current=TRUE record for the
        person_id (closed/retired target). Those are historical facts and
        legitimately diverge from the current dim state.
      * Comparison is NULL-safe via IS DISTINCT FROM so empty strings vs
        NULL don't trigger false positives (canonical attrs can legitimately
        be empty — e.g. governor's city is "").
-#}

WITH gold AS (

    SELECT
        content_id,
        source_type,
        person_id,
        person_name,
        person_role,
        person_party,
        person_city,
        person_is_incumbent
    FROM {{ ref('fct_content_gold') }}

),

dim_current AS (

    SELECT
        person_id,
        name         AS dim_name,
        role         AS dim_role,
        party        AS dim_party,
        city         AS dim_city,
        is_incumbent AS dim_is_incumbent
    FROM {{ ref('dim_persons') }}
    WHERE is_current = TRUE

),

violations AS (

    SELECT
        g.content_id,
        g.source_type,
        g.person_id,
        g.person_name,
        d.dim_name,
        g.person_role,
        d.dim_role,
        g.person_party,
        d.dim_party,
        g.person_city,
        d.dim_city,
        g.person_is_incumbent,
        d.dim_is_incumbent
    FROM gold AS g
    INNER JOIN dim_current AS d
        ON g.person_id = d.person_id
    WHERE
        g.person_name         IS DISTINCT FROM d.dim_name
        OR g.person_role      IS DISTINCT FROM d.dim_role
        OR g.person_party     IS DISTINCT FROM d.dim_party
        OR COALESCE(g.person_city, '') != COALESCE(d.dim_city, '')
        OR g.person_is_incumbent IS DISTINCT FROM d.dim_is_incumbent

)

SELECT * FROM violations
