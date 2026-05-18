{{
    config(
        materialized='table',
        tags=['dimension']
    )
}}

/*
    Dimension de plataformas de coleta. Estática (seed-based) — atualizar via
    seeds/dim_platforms_seed.csv quando uma plataforma nova é adicionada ou
    descontinuada.

    Categorias:
      news   — fontes editoriais (RSS).
      social — redes sociais coletadas via Apify actors.

    Cobre PQ-004 (distribuição por plataforma com hierarquia rede/notícia).
*/

SELECT
    platform_id,
    platform_name,
    platform_category,
    platform_owner,
    is_active,
    collection_method
FROM {{ ref('dim_platforms_seed') }}
