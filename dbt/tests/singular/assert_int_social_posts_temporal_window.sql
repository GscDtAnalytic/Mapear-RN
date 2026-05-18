-- Dentro de grupos não-singleton, delta entre posts deve ser ≤ 60 min.
-- Grupos que excedem a janela indicam bug na CTE de pair_candidates.
SELECT
    post_group_id,
    MIN(published_at)   AS earliest,
    MAX(published_at)   AS latest,
    {% if target.type == 'bigquery' -%}
    TIMESTAMP_DIFF(MAX(published_at), MIN(published_at), MINUTE)
    {%- else -%}
    CAST(EXTRACT(EPOCH FROM (MAX(published_at) - MIN(published_at))) / 60 AS INT)
    {%- endif %} AS delta_min
FROM {{ ref('int_social_posts__deduped') }}
WHERE group_size > 1
GROUP BY post_group_id
HAVING {% if target.type == 'bigquery' -%}
    TIMESTAMP_DIFF(MAX(published_at), MIN(published_at), MINUTE) > 60
    {%- else -%}
    EXTRACT(EPOCH FROM (MAX(published_at) - MIN(published_at))) / 60 > 60
    {%- endif %}
