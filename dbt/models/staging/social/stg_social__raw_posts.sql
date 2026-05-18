WITH facebook AS (
    SELECT * FROM {{ source('social_raw', 'raw_social_posts_facebook') }}
),

instagram AS (
    SELECT * FROM {{ source('social_raw', 'raw_social_posts_instagram') }}
),

x AS (
    SELECT * FROM {{ source('social_raw', 'raw_social_posts_x') }}
),

tiktok AS (
    SELECT * FROM {{ source('social_raw', 'raw_social_posts_tiktok') }}
),

unioned AS (
    SELECT * FROM facebook
    UNION ALL
    SELECT * FROM instagram
    UNION ALL
    SELECT * FROM x
    UNION ALL
    SELECT * FROM tiktok
)

SELECT
    post_id,
    platform,
    url,
    account,
    text,
    language,
    published_at,
    extracted_at,
    engagement,
    is_repost,
    is_reply,
    parent_post_id,
    content_hash,
    actor_run_id,
    ingestion_run_id,
    schema_version,
    source_type
FROM unioned
QUALIFY ROW_NUMBER() OVER (PARTITION BY post_id ORDER BY extracted_at DESC) = 1
