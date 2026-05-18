-- INV-004: Toda linha em fct_content_gold deve ter correspondência na silver de origem.
-- Garante que o filtro de scope não descarta registros silenciosamente entre staging
-- e gold — toda linha gold deve ser rastreável a um post/artigo silver IN_SCOPE.
-- Falha indica bug de filtro no modelo ou ID reutilizado entre fontes distintas.

-- RSS: cada content_id gold deve existir em stg_rss__silver_articles como content_hash
SELECT g.content_id, g.source_type
FROM {{ ref('fct_content_gold') }} AS g
LEFT JOIN {{ ref('stg_rss__silver_articles') }} AS rss
    ON g.content_id = rss.content_hash
WHERE g.source_type = 'rss'
  AND rss.content_hash IS NULL

UNION ALL

-- Social: cada content_id gold deve existir em stg_social__silver_posts como post_id
SELECT g.content_id, g.source_type
FROM {{ ref('fct_content_gold') }} AS g
LEFT JOIN {{ ref('stg_social__silver_posts') }} AS soc
    ON g.content_id = soc.post_id
WHERE g.source_type = 'social'
  AND soc.post_id IS NULL
