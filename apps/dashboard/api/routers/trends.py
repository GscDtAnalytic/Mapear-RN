from fastapi import APIRouter, Query
from api.bq import query, tbl, df_to_records

router = APIRouter()
GOV_ROLES = ["governor", "governor_candidate"]


@router.get("/trends/weekly")
def weekly(days: int = 90, roles: list[str] = Query(default=GOV_ROLES)):
    df = query(
        f"""
        SELECT person_name, person_role, week_start, mentions_total, electoral_phase
        FROM {tbl('mart_mentions_by_person_weekly')}
        WHERE person_role IN UNNEST(@roles)
          AND week_start >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        ORDER BY week_start
        """,
        roles=roles,
        days=days,
    )
    return df_to_records(df)


@router.get("/trends/daily-sentiment")
def daily_sentiment(days: int = 60, roles: list[str] = Query(default=GOV_ROLES)):
    df = query(
        f"""
        SELECT person_name, DATE(published_at) AS day, sentiment_label, COUNT(*) AS n
        FROM {tbl('mapear_events')}
        WHERE rn_relevant = TRUE AND source_type = 'social'
          AND person_role IN UNNEST(@roles) AND person_id IS NOT NULL
          AND sentiment_label IS NOT NULL
          AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY person_name, day, sentiment_label ORDER BY day
        """,
        roles=roles,
        days=days,
    )
    return df_to_records(df)


@router.get("/trends/spikes")
def spikes(
    days: int = 90, z_min: float = 2.0, roles: list[str] = Query(default=GOV_ROLES)
):
    df = query(
        f"""
        SELECT person_name, person_role, day, mentions,
               rolling_mean_30d, rolling_sd_30d, zscore, is_anomaly
        FROM {tbl('mart_anomalies_daily')}
        WHERE is_anomaly = TRUE AND zscore >= @z_min
          AND person_role IN UNNEST(@roles)
          AND day >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        ORDER BY day DESC, zscore DESC LIMIT 200
        """,
        roles=roles,
        days=days,
        z_min=z_min,
    )
    return df_to_records(df)
