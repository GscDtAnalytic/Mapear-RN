{{
    config(
        materialized='table',
        tags=['mart', 'eixo-2', 'endorsement']
    )
}}

/*
    fct_mayor_endorsements — apoio de cada prefeito monitorado na corrida
    ao governo do RN 2026.

    Grain : 1 linha por prefeito monitorado (cidade corrente).
    Fontes: stg_rss__mayor_endorsements (veredito da LLM — Eixo 2 v2d)
            dim_rn_cities_mayors        (override manual via seed)

    Precedência (decisão de produto "LLM investiga, seed sobrescreve"):
      - A investigação da LLM (Sonnet) é a fonte primária.
      - supports_candidate do seed é um OVERRIDE manual, aplicado apenas
        quando preenchido com algo diferente de 'Indefinido'/vazio.
      - endorsement_source registra qual venceu ('manual' | 'llm').

    Rows com error IS NOT NULL na silver são descartadas — só o último
    veredito válido por prefeito entra aqui.
*/

WITH llm_latest AS (

    SELECT *
    FROM {{ ref('stg_rss__mayor_endorsements') }}
    WHERE error IS NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY mayor_id ORDER BY investigated_at DESC
    ) = 1

),

mayors AS (

    SELECT
        city,
        mayor,
        party,
        population,
        monitored,
        supports_candidate
    FROM {{ ref('dim_rn_cities_mayors') }}
    WHERE is_current = TRUE AND state = 'RN'

),

joined AS (

    SELECT
        m.city,
        m.mayor                          AS mayor_name,
        m.party                          AS mayor_party,
        m.population,
        m.monitored,
        m.supports_candidate             AS manual_override,
        l.detected_candidate             AS llm_candidate,
        l.confidence                     AS llm_confidence,
        l.rationale                      AS llm_rationale,
        l.evidence_ids,
        l.article_count,
        l.endorsement_model,
        l.endorsement_prompt_version,
        l.investigated_at,
        (m.supports_candidate IS NOT NULL
         AND m.supports_candidate NOT IN ('Indefinido', '')) AS has_manual_override
    FROM mayors AS m
    LEFT JOIN llm_latest AS l
        ON l.mayor_name = m.mayor

)

SELECT
    city,
    mayor_name,
    mayor_party,
    population,
    monitored,
    CASE WHEN has_manual_override THEN manual_override
         ELSE COALESCE(llm_candidate, 'Indefinido') END  AS endorsed_candidate,
    CASE WHEN has_manual_override THEN 'manual'
         ELSE 'llm' END                                  AS endorsement_source,
    manual_override,
    llm_candidate,
    llm_confidence,
    llm_rationale,
    evidence_ids,
    article_count,
    endorsement_model,
    endorsement_prompt_version,
    investigated_at
FROM joined
ORDER BY population DESC
