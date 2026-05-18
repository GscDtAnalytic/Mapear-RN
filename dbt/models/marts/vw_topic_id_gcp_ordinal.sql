{{
    config(
        materialized='view'
    )
}}

/*
    View auxiliar — tópicos classificados pelo índice ordinal da GCP API.

    O topic_id aqui é o índice de posição na resposta da GCP classify_text,
    não um ID semântico estável. Não usar para agregação por nome de tópico.
    Use dim_topics (filtrado para keyword_map) para análise de temas.

    TDT-TOPIC-01: criada como parte da remediação de ambiguidade semântica
    de topic_id. Mantida para auditoria e análise exploratória de dados
    pré-2026-05-07.
*/

SELECT *
FROM {{ source('rss_gold', 'gold_articles') }}
WHERE topic_id_source = 'gcp_ordinal'
