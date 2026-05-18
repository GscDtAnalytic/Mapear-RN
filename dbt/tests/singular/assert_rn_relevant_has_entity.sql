-- INV-005: Todo evento rn_relevant deve mencionar ao menos uma entidade RN.
-- mapear_events filtra WHERE is_rn_relevant=TRUE, portanto todas as linhas têm
-- content_rn_relevant=TRUE. Se nenhum array de entidades (pessoas, cidades, partidos)
-- for preenchido, a flag rn_relevant foi atribuída sem evidência estruturada —
-- indica regressão no NER ou desacoplamento entre o classificador de relevância
-- e o extrator de entidades.
SELECT
    event_id,
    source_type,
    platform,
    content_rn_relevant
FROM {{ ref('mapear_events') }}
WHERE
    (mentioned_persons IS NULL OR ARRAY_LENGTH(mentioned_persons) = 0)
    AND (mentioned_cities  IS NULL OR ARRAY_LENGTH(mentioned_cities)  = 0)
    AND (mentioned_parties IS NULL OR ARRAY_LENGTH(mentioned_parties) = 0)
