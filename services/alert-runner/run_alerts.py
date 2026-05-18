"""Cloud Run Job entrypoint for semantic alerting — Mapear-RN.

Two alert types fired after the full NLP/CIB pipeline chain completes
(scheduled 11:00 Fortaleza, 30 min after last stance/community job):

  spike  — mart_anomalies_daily.is_anomaly = TRUE for today.
           Threshold is controlled by the dbt var anomaly_zscore_threshold
           (default 2.0). All flagged rows are forwarded to Slack.

  cib    — fct_community_score_daily.composite_score >= threshold
           AND fct_cluster_series.series_age_days >= threshold.
           Catches coordinated clusters that have been active for N+ days,
           which are far more likely to be intentional campaigns than
           one-day spikes.

Design: BQ queries are injected via ``bq_client`` and notifiers via
``spike_notifier`` / ``cib_notifier`` for testability without GCP creds.
The job never raises — each alert type is caught independently so a BQ
auth failure on one does not suppress the other.
"""

from __future__ import annotations

import os
import sys
from datetime import date


# ---------------------------------------------------------------------------
# BQ query templates
# ---------------------------------------------------------------------------

_SPIKE_QUERY = """\
SELECT
    person_name,
    person_role,
    CAST(mentions AS INT64)  AS mentions,
    ROUND(zscore, 3)         AS zscore
FROM `{project}.{gold}.mart_anomalies_daily`
WHERE day = CURRENT_DATE()
  AND is_anomaly = TRUE
ORDER BY zscore DESC
LIMIT 10
"""

# Parameterised: @composite_threshold (FLOAT64), @series_age_days (INT64)
_CIB_QUERY = """\
SELECT
    cs.community_id,
    cs.community_size,
    ROUND(cs.composite_score, 3)        AS composite_score,
    ROUND(cs.avg_synchrony_score, 3)    AS avg_synchrony_score,
    ROUND(cs.avg_alignment_score, 3)    AS avg_alignment_score,
    sr.series_age_days
FROM `{project}.{gold}.fct_community_score_daily` cs
JOIN `{project}.{gold}.fct_cluster_series` sr
  ON  cs.activation_date = sr.activation_date
  AND cs.region          = sr.region
  AND cs.algorithm       = sr.algorithm
  AND cs.community_id    = sr.community_id
WHERE cs.activation_date >= CURRENT_DATE() - INTERVAL 1 DAY
  AND cs.composite_score >= @composite_threshold
  AND sr.series_age_days >= @series_age_days
ORDER BY cs.composite_score DESC
LIMIT 10
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    sys.stderr.write(f"[alert-runner] {msg}\n")
    sys.stderr.flush()


def _bq_client():
    from google.cloud import bigquery  # lazy — not installed in dev

    return bigquery.Client(project=os.environ["GCP_PROJECT_ID"])


def query_spikes(client, project: str, gold: str) -> list[dict]:
    sql = _SPIKE_QUERY.format(project=project, gold=gold)
    return [dict(r) for r in client.query(sql).result()]


def query_cib_clusters(
    client,
    project: str,
    gold: str,
    composite_threshold: float,
    series_age_days: int,
) -> list[dict]:
    from google.cloud import bigquery

    sql = _CIB_QUERY.format(project=project, gold=gold)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "composite_threshold", "FLOAT64", composite_threshold
            ),
            bigquery.ScalarQueryParameter("series_age_days", "INT64", series_age_days),
        ]
    )
    return [dict(r) for r in client.query(sql, job_config=job_config).result()]


# ---------------------------------------------------------------------------
# Main entry point (dependency-injected for testing)
# ---------------------------------------------------------------------------


def run(
    *,
    bq_client=None,
    spike_notifier=None,
    cib_notifier=None,
) -> int:
    from mapear_infra.config import AlertConfig
    from mapear_infra.notifier import send_cib_alert, send_spike_alert

    cfg = AlertConfig()
    if not cfg.enabled:
        _log("alerting disabled (MAPEAR_ALERT_ENABLED=false)")
        return 0

    if not cfg.slack_webhook_url:
        _log("MAPEAR_ALERT_SLACK_WEBHOOK_URL not set — notifications will be skipped")

    project = os.environ.get("GCP_PROJECT_ID", "")
    gold = os.environ.get("GCP_BQ_DATASET_GOLD", "mapear_gold")
    run_date = date.today().isoformat()

    client = bq_client or _bq_client()
    notify_spike = spike_notifier or send_spike_alert
    notify_cib = cib_notifier or send_cib_alert

    alerts_sent = 0

    # --- Spike alerts ---
    try:
        spikes = query_spikes(client, project, gold)
        if spikes:
            ok = notify_spike(
                spikes=spikes, run_date=run_date, webhook_url=cfg.slack_webhook_url
            )
            alerts_sent += int(ok)
            _log(
                f"spike: {len(spikes)} anomalia(s) — "
                f"notificação {'enviada' if ok else 'pulada (sem webhook)'}"
            )
        else:
            _log("spike: nenhuma anomalia hoje")
    except Exception as exc:
        _log(f"spike query falhou: {exc}")

    # --- CIB cluster alerts ---
    try:
        clusters = query_cib_clusters(
            client,
            project,
            gold,
            cfg.cib_composite_score_threshold,
            cfg.cib_series_age_days,
        )
        if clusters:
            ok = notify_cib(
                clusters=clusters, run_date=run_date, webhook_url=cfg.slack_webhook_url
            )
            alerts_sent += int(ok)
            _log(
                f"cib: {len(clusters)} cluster(s) suspeito(s) — "
                f"notificação {'enviada' if ok else 'pulada (sem webhook)'}"
            )
        else:
            _log("cib: nenhum cluster acima do threshold hoje")
    except Exception as exc:
        _log(f"cib query falhou: {exc}")

    _log(f"concluído — {alerts_sent} notificação(ões) enviada(s)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
