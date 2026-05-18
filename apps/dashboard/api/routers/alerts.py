from fastapi import APIRouter, Query
from api.bq import query, tbl, df_to_records

router = APIRouter()
GOV_ROLES = ["governor", "governor_candidate"]


@router.get("/alerts/sentiment-pct")
def sentiment_pct(
    days: int = 30, conf: float = 0.5, roles: list[str] = Query(default=GOV_ROLES)
):
    df = query(
        f"""
        SELECT person_name, sentiment_label, COUNT(*) AS n
        FROM {tbl('mapear_events')}
        WHERE rn_relevant = TRUE AND source_type = 'social'
          AND person_role IN UNNEST(@roles)
          AND sentiment_label IS NOT NULL
          AND sentiment_confidence >= @conf
          AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY person_name, sentiment_label
        """,
        roles=roles,
        days=days,
        conf=conf,
    )
    return df_to_records(df)


@router.get("/alerts/topics")
def topics(days: int = 30, mode: str = "warning"):
    where = (
        "e.sentiment_label = 'WARNING' AND e.source_type = 'rss'"
        if mode == "warning"
        else "e.sentiment_overall < -0.3"
    )
    df = query(
        f"""
        SELECT t.topic_label, COUNT(*) AS critical_count
        FROM {tbl('mapear_events')} e
        JOIN {tbl('dim_topics')} t ON e.topic_id = t.topic_id AND t.topic_id_source = 'keyword_map'
        WHERE e.rn_relevant = TRUE AND e.topic_id IS NOT NULL AND {where}
          AND CAST(e.published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY t.topic_label ORDER BY critical_count DESC LIMIT 15
        """,
        days=days,
    )
    return df_to_records(df)


@router.get("/alerts/quality")
def quality(
    days: int = 30,
    roles: list[str] = Query(default=["governor", "governor_candidate", "mayor"]),
):
    df = query(
        f"""
        SELECT person_name, person_role, platform,
               ROUND(AVG(sentiment_confidence), 3)  AS avg_conf,
               ROUND(AVG(resolution_confidence), 3) AS avg_res_conf,
               COUNT(*) AS n
        FROM {tbl('mapear_events')}
        WHERE rn_relevant = TRUE AND source_type = 'social'
          AND person_role IN UNNEST(@roles) AND person_id IS NOT NULL
          AND sentiment_confidence IS NOT NULL
          AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY person_name, person_role, platform HAVING n >= 5
        ORDER BY avg_conf ASC LIMIT 50
        """,
        roles=roles,
        days=days,
    )
    return df_to_records(df)
