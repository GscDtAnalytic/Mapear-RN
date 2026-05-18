from fastapi import APIRouter
from api.bq import query, tbl, df_to_records

router = APIRouter()
GOV_ROLES = ["governor", "governor_candidate"]


@router.get("/candidates/ranking")
def ranking(days: int = 30):
    df = query(
        f"""
        SELECT person_name, person_party,
               COUNT(*) AS mentions,
               COUNTIF(sentiment_label = 'FAVORABLE') AS fav,
               COUNTIF(sentiment_label = 'WARNING')   AS warn,
               COUNTIF(sentiment_label = 'ALERT')     AS alert
        FROM {tbl('mapear_events')}
        WHERE rn_relevant = TRUE AND person_role IN UNNEST(@roles)
          AND person_id IS NOT NULL
          AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY person_name, person_party ORDER BY mentions DESC LIMIT 15
        """,
        roles=GOV_ROLES,
        days=days,
    )
    return df_to_records(df)


@router.get("/candidates/source-split")
def source_split(days: int = 30):
    df = query(
        f"""
        SELECT person_name, person_party, source_type, COUNT(*) AS mentions
        FROM {tbl('mapear_events')}
        WHERE rn_relevant = TRUE AND person_role IN UNNEST(@roles)
          AND person_id IS NOT NULL
          AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY person_name, person_party, source_type ORDER BY mentions DESC
        """,
        roles=GOV_ROLES,
        days=days,
    )
    return df_to_records(df)


@router.get("/candidates/group-comparison")
def group_comparison(days: int = 30):
    df = query(
        f"""
        SELECT
          CASE WHEN person_is_incumbent THEN 'Governo atual' ELSE 'Oposição' END AS grupo,
          COUNT(*) AS mentions,
          ROUND(AVG(sentiment_overall), 3) AS avg_sentiment,
          COUNTIF(sentiment_label = 'FAVORABLE') AS fav,
          COUNTIF(sentiment_label = 'WARNING')   AS warn,
          COUNTIF(sentiment_label = 'ALERT')     AS alert
        FROM {tbl('mapear_events')}
        WHERE rn_relevant = TRUE AND person_role IN UNNEST(@roles)
          AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY grupo ORDER BY grupo
        """,
        roles=GOV_ROLES,
        days=days,
    )
    return df_to_records(df)


@router.get("/candidates/engagement")
def engagement(days: int = 30):
    df = query(
        f"""
        SELECT person_name, person_role, platform,
               SUM(engagement_total) AS engagement, SUM(posts) AS posts
        FROM {tbl('mart_engagement_by_person_platform')}
        WHERE person_role IN UNNEST(@roles)
          AND week_start >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY person_name, person_role, platform ORDER BY engagement DESC
        """,
        roles=["governor", "governor_candidate", "mayor"],
        days=days,
    )
    return df_to_records(df)


@router.get("/candidates/mayors")
def mayors(days: int = 30):
    """Prefeitos das 5 maiores cidades do RN — sempre retorna as 5, mesmo
    sem menções no período. `supports_candidate` é o apoio declarado (curadoria
    manual no seed); pode vir vazio quando ainda não declarado."""
    df = query(
        f"""
        WITH top5 AS (
            SELECT city, mayor, party, population, supports_candidate
            FROM {tbl('dim_rn_cities_mayors')}
            WHERE is_current = TRUE AND state = 'RN'
            ORDER BY population DESC LIMIT 5
        )
        SELECT t.mayor AS person_name, t.city AS person_city,
               t.party AS person_party, t.supports_candidate,
               COUNT(e.event_id) AS mentions,
               ROUND(AVG(e.sentiment_overall), 3) AS avg_sentiment,
               COUNTIF(e.sentiment_label = 'FAVORABLE') AS fav,
               COUNTIF(e.sentiment_label = 'WARNING')   AS warn,
               COUNTIF(e.sentiment_label = 'ALERT')     AS alert
        FROM top5 t
        LEFT JOIN {tbl('mapear_events')} e
               ON e.person_city = t.city
              AND e.person_role = 'mayor'
              AND e.rn_relevant = TRUE
              AND CAST(e.published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY t.mayor, t.city, t.party, t.population, t.supports_candidate
        ORDER BY t.population DESC
        """,
        days=days,
    )
    return df_to_records(df)


@router.get("/candidates/mayor-endorsements")
def mayor_endorsements(days: int = 30):
    """Veredito de apoio por prefeito — investigação com LLM (Sonnet) feita
    pelo job out-of-band `run_mayor_endorsement_detection`, com override
    manual do seed quando preenchido.

    Lê `fct_mayor_endorsements`. O parâmetro `days` é aceito por
    compatibilidade mas ignorado: o veredito é o estado corrente, não uma
    janela temporal."""
    _ = days
    df = query(
        f"""
        SELECT city, mayor_name, mayor_party,
               endorsed_candidate, endorsement_source, manual_override,
               llm_candidate, llm_confidence, llm_rationale,
               article_count, endorsement_model, investigated_at
        FROM {tbl('fct_mayor_endorsements')}
        ORDER BY population DESC
        """
    )
    return df_to_records(df)
