from fastapi import APIRouter
from api.bq import query, tbl, df_to_records

router = APIRouter()

GOV_ROLES = ["governor", "governor_candidate"]


@router.get("/overview")
def overview(days: int = 30):
    phase_df = query(
        f"SELECT electoral_phase FROM {tbl('dim_dates')} WHERE date_day = CURRENT_DATE() LIMIT 1"
    )
    countdown_df = query(
        "SELECT DATE_DIFF(DATE '2026-10-04', CURRENT_DATE(), DAY) AS days_to_first_round"
    )
    phase = phase_df["electoral_phase"].iloc[0] if not phase_df.empty else "none"
    days_to_first = (
        int(countdown_df["days_to_first_round"].iloc[0])
        if not countdown_df.empty
        else None
    )

    hero_df = query(
        f"""
        WITH curr AS (
            SELECT person_name, person_party, COUNT(*) AS mentions
            FROM {tbl('mapear_events')}
            WHERE rn_relevant = TRUE AND person_id IS NOT NULL
              AND person_role IN UNNEST(@roles)
              AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
            GROUP BY person_name, person_party ORDER BY mentions DESC LIMIT 1
        ),
        prev AS (
            SELECT COUNT(*) AS prev_mentions
            FROM {tbl('mapear_events')} e JOIN curr c ON e.person_name = c.person_name
            WHERE e.rn_relevant = TRUE
              AND CAST(e.published_at AS DATE) BETWEEN
                DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY)
                AND DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
        )
        SELECT c.person_name, c.person_party, c.mentions, p.prev_mentions
        FROM curr c, prev p
        """,
        roles=GOV_ROLES,
    )

    kpis_df = query(
        f"""
        WITH curr AS (
            SELECT COUNT(*) AS total,
                   COUNTIF(source_type='rss') AS rss,
                   COUNTIF(source_type='social') AS social,
                   COUNT(DISTINCT person_id) AS persons
            FROM {tbl('mapear_events')}
            WHERE rn_relevant = TRUE
              AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        ),
        prev AS (
            SELECT COUNT(*) AS total,
                   COUNTIF(source_type='rss') AS rss,
                   COUNTIF(source_type='social') AS social,
                   COUNT(DISTINCT person_id) AS persons
            FROM {tbl('mapear_events')}
            WHERE rn_relevant = TRUE
              AND CAST(published_at AS DATE) BETWEEN
                DATE_SUB(CURRENT_DATE(), INTERVAL @days2 DAY)
                AND DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        )
        SELECT c.total, p.total AS prev_total,
               c.rss, p.rss AS prev_rss,
               c.social, p.social AS prev_social,
               c.persons, p.persons AS prev_persons
        FROM curr c, prev p
        """,
        days=days,
        days2=days * 2,
    )

    anom_df = query(
        f"""
        SELECT COUNT(*) AS n FROM {tbl('mart_anomalies_daily')}
        WHERE is_anomaly = TRUE AND day >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        """,
        days=days,
    )

    freshness_df = query(
        f"""
        SELECT FORMAT_TIMESTAMP('%d/%m às %H:%M', MAX(published_at), 'America/Fortaleza') AS ts
        FROM {tbl('mapear_events')} WHERE rn_relevant = TRUE
        """
    )

    map_df = query(
        f"""
        WITH unnested AS (
            SELECT c AS city FROM {tbl('mapear_events')}, UNNEST(mentioned_cities) AS c
            WHERE rn_relevant = TRUE
              AND CAST(published_at AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        )
        SELECT u.city, COUNT(*) AS mentions,
               m.latitude, m.longitude, m.mayor, m.party, m.population
        FROM unnested u
        JOIN {tbl('dim_rn_cities_mayors')} m ON u.city = m.city AND m.is_current = TRUE
        WHERE m.latitude IS NOT NULL
        GROUP BY u.city, m.latitude, m.longitude, m.mayor, m.party, m.population
        ORDER BY mentions DESC LIMIT 25
        """,
        days=days,
    )

    candidates_df = query(
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
        GROUP BY person_name, person_party ORDER BY mentions DESC LIMIT 6
        """,
        roles=GOV_ROLES,
        days=days,
    )

    anomalies_df = query(
        f"""
        SELECT person_name, person_role, day, mentions, zscore
        FROM {tbl('mart_anomalies_daily')}
        WHERE is_anomaly = TRUE AND day >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        ORDER BY day DESC, zscore DESC LIMIT 6
        """,
        days=days,
    )

    hero = df_to_records(hero_df)[0] if not hero_df.empty else None
    kpis = df_to_records(kpis_df)[0] if not kpis_df.empty else {}
    kpis["anomalies"] = int(anom_df["n"].iloc[0]) if not anom_df.empty else 0

    return {
        "phase": phase,
        "days_to_first_round": days_to_first,
        "freshness": freshness_df["ts"].iloc[0] if not freshness_df.empty else "—",
        "hero": hero,
        "kpis": kpis,
        "map_data": df_to_records(map_df),
        "candidates": df_to_records(candidates_df),
        "anomalies": df_to_records(anomalies_df),
    }
