-- Row 19 — Invariante composta: person_id NULL ↔ campos denorm NULL + FK orphan check.
--
-- Dois invariantes distintos testados em conjunto:
--
-- (A) Defensivo (impossível por LEFT JOIN, mas explícito):
--     person_id IS NULL → person_role/name/party/city/is_incumbent IS NULL.
--     mapear_events.sql:234 faz LEFT JOIN persons_current (WHERE is_current=TRUE).
--     Um LEFT JOIN nunca produz colunas não-NULL do lado direito sem chave correspondente.
--
-- (B) FK orphan check (bug real detectável):
--     person_id IS NOT NULL AND person_id não existe em NENHUMA versão de dim_persons.
--     Distingue SCD2 staleness (is_current=FALSE, OK) de FK órfã genuína (bug).
--     O schema.yml FK test usa WHERE is_current=TRUE e pode perder FKs de registros fechados;
--     este teste verifica existência em qualquer versão do SCD2.
--
-- Severidade M (warn): FK orphan é bug de pipeline; o (A) defensivo nunca deve disparar.
{{ config(severity='warn') }}

SELECT
    me.event_id,
    me.source_type,
    me.person_id,
    me.person_name,
    me.person_role,
    CASE
        WHEN me.person_id IS NULL
             AND (
                 me.person_name IS NOT NULL
                 OR me.person_role IS NOT NULL
                 OR me.person_party IS NOT NULL
                 OR me.person_city IS NOT NULL
                 OR me.person_is_incumbent IS NOT NULL
             )
             THEN 'defensivo_violated'
        WHEN me.person_id IS NOT NULL AND dp.person_id IS NULL
             THEN 'orphan_fk'
    END AS violation_type
FROM {{ ref('mapear_events') }} AS me
LEFT JOIN (
    SELECT DISTINCT person_id
    FROM {{ ref('dim_persons') }}
) AS dp
    ON me.person_id = dp.person_id
WHERE
    (
        me.person_id IS NULL
        AND (
            me.person_name IS NOT NULL
            OR me.person_role IS NOT NULL
            OR me.person_party IS NOT NULL
            OR me.person_city IS NOT NULL
            OR me.person_is_incumbent IS NOT NULL
        )
    )
    OR (me.person_id IS NOT NULL AND dp.person_id IS NULL)
