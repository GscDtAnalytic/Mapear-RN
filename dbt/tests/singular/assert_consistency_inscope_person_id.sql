-- Consistência: author_in_scope=TRUE exige person_id preenchido.
-- IN_SCOPE é o resultado do PersonResolver identificar e resolver o autor como
-- um alvo monitorado — portanto person_id nunca pode ser NULL neste estado.
-- Checa RSS e Social para capturar a falha próxima à origem.
SELECT
    record_id,
    source_type,
    author_in_scope,
    person_id
FROM (
    SELECT
        content_hash AS record_id,
        'rss' AS source_type,
        author_in_scope,
        person_id
    FROM {{ ref('stg_rss__silver_articles') }}

    UNION ALL

    SELECT
        post_id AS record_id,
        'social' AS source_type,
        author_in_scope,
        person_id
    FROM {{ ref('stg_social__silver_posts') }}
) combined
WHERE author_in_scope = TRUE
  AND person_id IS NULL
