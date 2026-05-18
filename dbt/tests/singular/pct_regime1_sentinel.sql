-- TDT-TOPIC-01: Monitor proportion of gcp_ordinal vs keyword_map classifications.
--
-- Fires WARN if yesterday's classified articles deviate from expected range [0.60, 0.80].
-- Excludes legacy_unknown (pre-cutover records) from the calculation.
-- Only gains signal after the pipeline new producer runs post-2026-05-07 cutover.
{{ config(severity='warn') }}

WITH today AS (
    SELECT
        CASE
            WHEN COUNTIF(topic_id_source IN ('gcp_ordinal', 'keyword_map')) = 0 THEN NULL
            ELSE 1.0 * COUNTIF(topic_id_source = 'gcp_ordinal')
                     / COUNTIF(topic_id_source IN ('gcp_ordinal', 'keyword_map'))
        END AS pct
    FROM {{ source('rss_gold', 'gold_articles') }}
    WHERE DATE(published_at) = CURRENT_DATE() - 1
      AND topic_id_source != 'legacy_unknown'
)
SELECT pct FROM today
WHERE pct IS NOT NULL AND (pct < 0.60 OR pct > 0.80)
