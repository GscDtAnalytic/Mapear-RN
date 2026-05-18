-- INV-006: mentioned_mayors > 0 implica is_rn_relevant=TRUE — APENAS para RSS.
--
-- Invariante: Se o NER encontrou um prefeito monitorado no texto de um artigo
-- RSS, o classificador de relevância deve ter marcado o conteúdo como rn_relevant.
-- Em RSS, ambos os componentes (NER de entidades + classificador de relevância)
-- operam sobre o mesmo texto de notícia, de modo que a menção de um prefeito RN
-- é condição suficiente para relevância política.
--
-- Por que NÃO vale para Social:
-- Posts de autores IN_SCOPE frequentemente contêm auto-menção do prefeito
-- (assinatura, hashtag, citação própria) sem natureza política. O NER detecta
-- a string do nome; o classificador de relevância corretamente rejeita o
-- conteúdo como não-político. É design, não bug.
--
-- Evidência de validação em prod (2026-05-04):
-- 275 violações em Social, 100% com scope_status=IN_SCOPE, 0 chegando a
-- mapear_events. Padrão consistente com auto-menção em conteúdo pessoal.
-- 0 violações em RSS — invariante válido nesta fonte.
SELECT
    record_id,
    source_type,
    is_rn_relevant
FROM (
    SELECT
        content_hash    AS record_id,
        'rss'           AS source_type,
        is_rn_relevant
    FROM {{ ref('stg_rss__silver_articles') }}
    WHERE ARRAY_LENGTH(mentioned_mayors) > 0
) rss_only
WHERE is_rn_relevant = FALSE
