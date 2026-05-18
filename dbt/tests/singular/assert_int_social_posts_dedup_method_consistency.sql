-- dedup_method = 'singleton' ⟺ group_size = 1 (bidirecional).
-- Violação: singleton com group_size > 1, ou prefix_hash_match com group_size = 1.
SELECT
    post_id,
    dedup_method,
    group_size
FROM {{ ref('int_social_posts__deduped') }}
WHERE (dedup_method = 'singleton'         AND group_size != 1)
   OR (dedup_method = 'prefix_hash_match' AND group_size  = 1)
