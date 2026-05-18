-- Para cada post_group_id, exatamente 1 linha deve ter is_canonical = TRUE.
-- Retorna grupos com 0 ou 2+ canônicos (ambos são invariante violada).
SELECT
    post_group_id,
    SUM(CASE WHEN is_canonical THEN 1 ELSE 0 END) AS canonical_count
FROM {{ ref('int_social_posts__deduped') }}
GROUP BY post_group_id
HAVING SUM(CASE WHEN is_canonical THEN 1 ELSE 0 END) != 1
