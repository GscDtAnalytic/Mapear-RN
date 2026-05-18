-- Em pares (group_size = 2), cada post_id deve aparecer em exatamente 1 grupo.
-- Violação: post_id em múltiplos grupos indica pareamento ambíguo — o mesmo bug
-- encontrado no PR1 quando 2 FB com prefix100 idêntico do mesmo person matchavam
-- o mesmo IG. O QUALIFY bidirecional em groups_assigned resolve isso, mas se a
-- CTE for refatorada sem preservar a invariante bidirecional o bug volta silenciosamente.
SELECT
    post_id,
    COUNT(DISTINCT post_group_id) AS distinct_groups
FROM {{ ref('int_social_posts__deduped') }}
WHERE group_size > 1
GROUP BY post_id
HAVING COUNT(DISTINCT post_group_id) > 1
