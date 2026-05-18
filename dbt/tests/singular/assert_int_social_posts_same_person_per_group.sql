-- Dentro de grupos com group_size > 1, todos os posts devem ter o mesmo person_id.
-- Grupos cross-person indicam falso positivo no match de prefixo.
SELECT
    post_group_id,
    COUNT(DISTINCT person_id) AS distinct_persons
FROM {{ ref('int_social_posts__deduped') }}
WHERE group_size > 1
GROUP BY post_group_id
HAVING COUNT(DISTINCT person_id) > 1
