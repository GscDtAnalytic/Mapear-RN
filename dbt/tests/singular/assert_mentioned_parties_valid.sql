{{ config(severity='warn') }}
-- Row 3 — Partidos mencionados devem existir no seed canônico (dim_rn_cities_mayors).
-- Singular test cobrindo RSS + Social via mapear_events.
-- severity='warn' porque NER pode capturar variações ortográficas legítimas
-- (ex: "UB" vs "União Brasil") que não estão no seed — não são bug crítico.

WITH party_mentions AS (
    SELECT
        event_id,
        source_type,
        platform,
        party
    FROM {{ ref('mapear_events') }},
    UNNEST(mentioned_parties) AS party
    WHERE ARRAY_LENGTH(mentioned_parties) > 0
),
canonical_parties AS (
    SELECT DISTINCT party FROM {{ ref('dim_rn_cities_mayors') }}
    WHERE party IS NOT NULL
)
SELECT pm.*
FROM party_mentions pm
LEFT JOIN canonical_parties cp USING (party)
WHERE cp.party IS NULL
