/*
    Identifica grupos de cross-posts FB+IG e elege o canônico (FB-first).
    Grain      : 1 row per post_id (preservado de silver_social_posts).
    Design doc : docs/sprint3_b5_dedup_design.md
    Tech debt  : docs/tech_debt_crossplatform_dedup.md (TD-DEDUP-01)
    Colunas adicionadas em relação ao silver:
      post_group_id  STRING  — ID estável do grupo (SHA256 do par LEAST(fb_id, ig_id))
      is_canonical   BOOL    — TRUE para o representante FB-first do grupo
      group_size     INT64   — número de posts no grupo (1 para singletons)
      dedup_method   STRING  — 'singleton' | 'prefix_hash_match'

    Nota de compatibilidade cross-target:
      post_group_id usa macro stable_hash() — SHA256 hex idêntico em BQ e DuckDB.
      text_prefix_100 usa normalize_nfkc() — BQ usa NFKC, DuckDB usa NFC.
      Para português brasileiro padrão, NFC≈NFKC (diferença em CJK/ligaduras tipográficas
      que não ocorrem no corpus). Ver macro social_text_normalize.sql para critério de revisão.
*/

{{ config(materialized='table') }}

WITH source AS (

    SELECT * FROM {{ ref('stg_social__silver_posts') }}

),

normalized AS (

    -- Adds text_prefix_100 for pairing logic. Not propagated to final output.
    SELECT
        *,
        SUBSTR(
            LOWER(TRIM({{ normalize_nfkc("COALESCE(text, '')") }})),
            1, 100
        ) AS text_prefix_100
    FROM source

),

eligible_for_pairing AS (

    -- FB and IG posts with sufficient text and known person.
    -- All others are unconditional singletons.
    SELECT * FROM normalized
    WHERE platform IN ('facebook', 'instagram')
      AND LENGTH(TRIM(COALESCE(text, ''))) > 10
      AND person_id IS NOT NULL

),

pair_candidates AS (

    -- Cross-join FB × IG within same person and ±60 min window.
    SELECT
        fb.post_id AS post_id_fb,
        ig.post_id AS post_id_ig,
        fb.person_id,
        {% if target.type == 'bigquery' -%}
        ABS(TIMESTAMP_DIFF(fb.published_at, ig.published_at, MINUTE))
        {%- else -%}
        CAST(ABS(EXTRACT(EPOCH FROM (fb.published_at - ig.published_at)) / 60) AS INT)
        {%- endif %} AS delta_min
    FROM eligible_for_pairing AS fb
    JOIN eligible_for_pairing AS ig
        ON  fb.person_id       = ig.person_id
        AND fb.text_prefix_100 = ig.text_prefix_100
        AND fb.platform        = 'facebook'
        AND ig.platform        = 'instagram'
        AND {% if target.type == 'bigquery' -%}
            ABS(TIMESTAMP_DIFF(fb.published_at, ig.published_at, MINUTE))
            {%- else -%}
            ABS(EXTRACT(EPOCH FROM (fb.published_at - ig.published_at)) / 60)
            {%- endif %} <= 60

),

groups_assigned AS (

    -- One IG per FB: keep closest match, tiebreaker post_id_ig lexicographic.
    -- A second IG matching the same FB within the window becomes a singleton.
    SELECT
        post_id_fb,
        post_id_ig,
        person_id,
        delta_min,
        {{ stable_hash("CONCAT(person_id, ':', LEAST(post_id_fb, post_id_ig))") }} AS post_group_id
    FROM pair_candidates
    -- Bijection: 1 IG per FB AND 1 FB per IG.
    -- PR1 sanity (real data) revelou duplicatas quando 2 FB com prefix100 idêntico do
    -- mesmo person (reposts do mesmo conteúdo) ambos matchavam o mesmo IG.
    -- QUALIFY unidirecional (só post_id_fb) não prevenia isso. A condição AND abaixo
    -- implementa mutual best-match: o par vence só se cada lado é o melhor match do outro.
    QUALIFY
        ROW_NUMBER() OVER (PARTITION BY post_id_fb ORDER BY delta_min ASC, post_id_ig ASC) = 1
        AND ROW_NUMBER() OVER (PARTITION BY post_id_ig ORDER BY delta_min ASC, post_id_fb ASC) = 1

),

canonical_election AS (

    -- Expand each pair into 2 rows: FB (canonical) and IG (non-canonical).
    SELECT
        post_id_fb          AS post_id,
        post_group_id,
        TRUE                AS is_canonical,
        2                   AS group_size,
        'prefix_hash_match' AS dedup_method
    FROM groups_assigned

    UNION ALL

    SELECT
        post_id_ig          AS post_id,
        post_group_id,
        FALSE               AS is_canonical,
        2                   AS group_size,
        'prefix_hash_match' AS dedup_method
    FROM groups_assigned

),

paired_post_ids AS (

    SELECT post_id FROM canonical_election

),

singletons AS (

    SELECT
        s.post_id,
        {{ stable_hash("CONCAT(COALESCE(s.person_id, 'NULL'), ':', s.post_id)") }} AS post_group_id,
        TRUE         AS is_canonical,
        1            AS group_size,
        'singleton'  AS dedup_method
    FROM source AS s
    WHERE NOT EXISTS (
        SELECT 1 FROM paired_post_ids p WHERE p.post_id = s.post_id
    )

),

all_dedup_flags AS (

    SELECT * FROM canonical_election
    UNION ALL
    SELECT * FROM singletons

),

final AS (

    SELECT
        s.*,
        f.post_group_id,
        f.is_canonical,
        f.group_size,
        f.dedup_method
    FROM source AS s
    INNER JOIN all_dedup_flags AS f ON s.post_id = f.post_id

)

SELECT * FROM final
