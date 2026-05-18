-- TDT-TOPIC-01: Validate invariants between topic_id, topic_id_source, and topic_label_raw.
--
-- Invariant 1: keyword_map → topic_id in 1..10, topic_label_raw NOT NULL
-- Invariant 2: unclassified → topic_id = -1, topic_label_raw IS NULL
-- Invariant 3: gcp_ordinal → topic_id >= 0, topic_label_raw NOT NULL
-- Invariant 4: legacy_unknown → topic_label_raw IS NULL (topic_id may be any value >= -1)
--
-- Violations indicate producer bug or incorrect backfill execution.
{{ config(severity='error') }}

SELECT
    content_hash,
    topic_id,
    topic_id_source,
    topic_label_raw,
    'keyword_map_out_of_range' AS violation_type
FROM {{ source('rss_gold', 'gold_articles') }}
WHERE topic_id_source = 'keyword_map'
  AND (topic_id < 1 OR topic_id > 10)

UNION ALL

SELECT
    content_hash,
    topic_id,
    topic_id_source,
    topic_label_raw,
    'keyword_map_null_label' AS violation_type
FROM {{ source('rss_gold', 'gold_articles') }}
WHERE topic_id_source = 'keyword_map'
  AND topic_label_raw IS NULL

UNION ALL

SELECT
    content_hash,
    topic_id,
    topic_id_source,
    topic_label_raw,
    'unclassified_not_minus_one' AS violation_type
FROM {{ source('rss_gold', 'gold_articles') }}
WHERE topic_id_source = 'unclassified'
  AND topic_id != -1

UNION ALL

SELECT
    content_hash,
    topic_id,
    topic_id_source,
    topic_label_raw,
    'unclassified_nonnull_label' AS violation_type
FROM {{ source('rss_gold', 'gold_articles') }}
WHERE topic_id_source = 'unclassified'
  AND topic_label_raw IS NOT NULL

UNION ALL

SELECT
    content_hash,
    topic_id,
    topic_id_source,
    topic_label_raw,
    'gcp_ordinal_negative_id' AS violation_type
FROM {{ source('rss_gold', 'gold_articles') }}
WHERE topic_id_source = 'gcp_ordinal'
  AND topic_id < 0

UNION ALL

SELECT
    content_hash,
    topic_id,
    topic_id_source,
    topic_label_raw,
    'gcp_ordinal_null_label' AS violation_type
FROM {{ source('rss_gold', 'gold_articles') }}
WHERE topic_id_source = 'gcp_ordinal'
  AND topic_label_raw IS NULL

UNION ALL

SELECT
    content_hash,
    topic_id,
    topic_id_source,
    topic_label_raw,
    'legacy_unknown_nonnull_label' AS violation_type
FROM {{ source('rss_gold', 'gold_articles') }}
WHERE topic_id_source = 'legacy_unknown'
  AND topic_label_raw IS NOT NULL
