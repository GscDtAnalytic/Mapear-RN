from fastapi import APIRouter
from api.bq import query, tbl, df_to_records

router = APIRouter()


@router.get("/coverage/cities")
def cities(days: int = 30):
    df = query(
        f"""
        WITH unnested AS (
            SELECT c AS city FROM {tbl('mapear_events')}, UNNEST(mentioned_cities) AS c
            WHERE rn_relevant = TRUE
              AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        )
        SELECT u.city, COUNT(*) AS mentions,
               m.population, m.mayor, m.party, m.latitude, m.longitude
        FROM unnested u
        JOIN {tbl('dim_rn_cities_mayors')} m ON u.city = m.city AND m.is_current = TRUE
        GROUP BY u.city, m.population, m.mayor, m.party, m.latitude, m.longitude
        ORDER BY mentions DESC LIMIT 25
        """,
        days=days,
    )
    return df_to_records(df)


@router.get("/coverage/feeds")
def feeds(days: int = 30):
    df = query(
        f"""
        SELECT source_feed, COUNT(DISTINCT content_id) AS articles
        FROM {tbl('fct_content')}
        WHERE source_type = 'rss' AND is_rn_relevant = TRUE
          AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY source_feed ORDER BY articles DESC LIMIT 20
        """,
        days=days,
    )
    return df_to_records(df)


@router.get("/coverage/platforms")
def platforms(days: int = 30):
    df = query(
        f"""
        SELECT e.platform, p.platform_category, COUNT(*) AS events
        FROM {tbl('mapear_events')} e
        LEFT JOIN {tbl('dim_platforms')} p ON e.platform = p.platform_id
        WHERE rn_relevant = TRUE
          AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY e.platform, p.platform_category ORDER BY events DESC
        """,
        days=days,
    )
    return df_to_records(df)


@router.get("/coverage/schedule")
def schedule(days: int = 30):
    df = query(
        f"""
        SELECT EXTRACT(HOUR FROM published_at) AS hour,
               d.dow_num, d.dow_name, COUNT(*) AS articles
        FROM {tbl('fct_content')} c
        LEFT JOIN {tbl('dim_dates')} d ON CAST(c.published_at AS DATE) = d.date_day
        WHERE c.source_type = 'rss' AND c.is_rn_relevant = TRUE
          AND CAST(c.published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY hour, d.dow_num, d.dow_name ORDER BY d.dow_num, hour
        """,
        days=days,
    )
    return df_to_records(df)
