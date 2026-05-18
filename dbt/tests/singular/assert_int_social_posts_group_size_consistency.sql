-- group_size deve igualar COUNT(*) real dentro de cada post_group_id.
-- Retorna grupos onde o valor declarado diverge da contagem real.
SELECT
    post_group_id,
    group_size                        AS declared_size,
    COUNT(*)                          AS real_size
FROM {{ ref('int_social_posts__deduped') }}
GROUP BY post_group_id, group_size
HAVING group_size != COUNT(*)
