-- Rows 29 + 31 — Invariantes NULL-RSS para sentiment_overall e sentiment_label.
--
-- Dois invariantes de fonte testados em conjunto sobre mapear_events:
--
-- (A) sentiment_overall NULL para RSS (Row 29):
--     mapear_events.sql:58 CAST NULL explícito para RSS.
--     Um valor não-NULL em source_type='rss' indica vazamento do pipeline
--     Social para RSS — ex: UNION com mismatch de colunas ou JOIN errado.
--
-- (B) sentiment_label NULL para RSS (Row 31):
--     mapear_events.sql:59 CAST NULL explícito para RSS.
--     Análogo ao (A). accepted_values + not_null existem em
--     stg_social__silver_posts/schema.yml:52-57 para Social; a invariante
--     inversa (RSS deve ser NULL) estava ausente em mapear_events.
--
-- Universo: mapear_events completo — a invariante vale para TODOS os
-- eventos RSS, independente de escopo ou person_id.
--
-- Não inclui entities: TDT-RSS-ENTITIES-01 fechado em 2026-05-07
-- (entities deixou de ser NULL para RSS); o regression test
-- assert_entities_null_for_rss.sql foi removido junto.
--
-- Severidade M (warn): violação indica bug de pipeline real. Não escala
-- para 'error' enquanto BL-F2-05 (sentimento RSS) está em backlog —
-- política de não-bloqueio para tech debts conhecidos.
{{ config(severity='warn') }}

SELECT
    event_id,
    source_type,
    platform,
    sentiment_overall,
    sentiment_label,
    CASE
        WHEN source_type = 'rss' AND sentiment_overall IS NOT NULL
            THEN 'sentiment_overall_not_null_for_rss'
        WHEN source_type = 'rss' AND sentiment_label IS NOT NULL
            THEN 'sentiment_label_not_null_for_rss'
    END AS violation_type
FROM {{ ref('mapear_events') }}
WHERE
    source_type = 'rss'
    AND (
        sentiment_overall IS NOT NULL
        OR sentiment_label IS NOT NULL
    )
