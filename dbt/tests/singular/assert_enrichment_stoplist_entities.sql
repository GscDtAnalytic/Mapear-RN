{{ config(severity='warn') }}
-- Qualidade de enriquecimento: documentos com excesso de menções de entidades
-- indicam vazamento de stoplist no NER (entidades genéricas não filtradas).
-- Threshold: dq_max_entity_mentions_per_doc (default: 50 menções por documento).
{% set t = quality_thresholds() %}

SELECT
    event_id,
    source_type,
    platform,
    (
        COALESCE(ARRAY_LENGTH(mentioned_persons), 0)
        + COALESCE(ARRAY_LENGTH(mentioned_cities), 0)
        + COALESCE(ARRAY_LENGTH(mentioned_parties), 0)
    ) AS total_entity_mentions
FROM {{ ref('mapear_events') }}
WHERE (
    COALESCE(ARRAY_LENGTH(mentioned_persons), 0)
    + COALESCE(ARRAY_LENGTH(mentioned_cities), 0)
    + COALESCE(ARRAY_LENGTH(mentioned_parties), 0)
) > {{ t.max_entity_mentions_per_doc }}
