/*
    assert_scope_fields_semantics
    ─────────────────────────────────────────────────────────────────────────────
    Valida que author_in_scope e content_rn_relevant são derivados corretamente
    das suas respectivas fontes, de forma INDEPENDENTE entre si.

    Invariante crítico:
        author_in_scope   = (scope_status = 'IN_SCOPE')   — sobre o AUTOR
        content_rn_relevant = is_rn_relevant              — sobre o CONTEÚDO

    As duas dimensões são ortogonais:
        • author_in_scope=TRUE  não implica  content_rn_relevant=TRUE
        • content_rn_relevant=TRUE  não implica  author_in_scope=TRUE

    Este teste falha se qualquer registro tiver derivação incorreta.
    Retorna linhas apenas em caso de inconsistência (dbt espera zero linhas).
*/

-- Social: author_in_scope deve derivar de scope_status, não de is_rn_relevant
SELECT
    'social'                    AS source,
    post_id                     AS id,
    'author_in_scope_mismatch'  AS reason
FROM {{ ref('stg_social__silver_posts') }}
WHERE author_in_scope != (scope_status = 'IN_SCOPE')

UNION ALL

-- Social: content_rn_relevant deve espelhar is_rn_relevant exatamente
SELECT
    'social'                        AS source,
    post_id                         AS id,
    'content_rn_relevant_mismatch'  AS reason
FROM {{ ref('stg_social__silver_posts') }}
WHERE content_rn_relevant != is_rn_relevant

UNION ALL

-- RSS: author_in_scope deve derivar de scope_status (NULL → FALSE)
SELECT
    'rss'                       AS source,
    content_hash                AS id,
    'author_in_scope_mismatch'  AS reason
FROM {{ ref('stg_rss__silver_articles') }}
WHERE author_in_scope != (COALESCE(scope_status, '') = 'IN_SCOPE')

UNION ALL

-- RSS: content_rn_relevant deve espelhar is_rn_relevant exatamente
SELECT
    'rss'                           AS source,
    content_hash                    AS id,
    'content_rn_relevant_mismatch'  AS reason
FROM {{ ref('stg_rss__silver_articles') }}
WHERE content_rn_relevant != is_rn_relevant
