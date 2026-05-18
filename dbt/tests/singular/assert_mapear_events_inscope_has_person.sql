-- Todo evento com author_in_scope=TRUE deve ter person_id e person_name resolvidos.
-- Retorna linhas que violam a invariante (falha quando count > 0).
SELECT
    event_id,
    source_type,
    platform,
    author_in_scope,
    person_id,
    person_name
FROM {{ ref('mapear_events') }}
WHERE
    author_in_scope = TRUE
    AND (person_id IS NULL OR person_name IS NULL)
