"""Slack notifications for Mapear-RN — pipeline completion and semantic alerts.

Two categories:
  notify_slack()      — batch pipeline completion summary (existing).
  send_spike_alert()  — mention-spike anomaly (is_anomaly=TRUE in mart_anomalies_daily).
  send_cib_alert()    — CIB sustained cluster (score > threshold, series > N days).

All functions return True on success, False on failure / skipped (never raise).
Silently skip when the webhook URL is not configured.
"""

import os

import httpx
from loguru import logger


def notify_slack(
    pipeline_name: str = "Mapear",
    discovered: int = 0,
    extracted: int = 0,
    unique: int = 0,
    rn_relevant: int = 0,
    gold_enriched: int = 0,
    errors: list[str] | None = None,
    batch_id: str = "",
) -> bool:
    """Send pipeline summary to Slack.

    Returns True if sent successfully, False otherwise.
    """
    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        logger.debug("SLACK_WEBHOOK_URL not set, skipping notification")
        return False

    status_emoji = ":white_check_mark:" if not errors else ":warning:"
    error_block = ""
    if errors:
        error_lines = "\n".join(f"• {e}" for e in errors[:5])
        error_block = f"\n*Errors:*\n{error_lines}"

    message = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{status_emoji} {pipeline_name} — Batch {batch_id}",
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Discovered:* {discovered}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Extracted:* {extracted}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Unique:* {unique}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*RN-Relevant:* {rn_relevant}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Gold Enriched:* {gold_enriched}",
                    },
                ],
            },
        ],
    }

    if error_block:
        message["blocks"].append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": error_block},
            }
        )

    try:
        response = httpx.post(
            webhook_url,
            json=message,
            timeout=10.0,
        )
        response.raise_for_status()
        logger.info("Slack notification sent for batch {batch}", batch=batch_id)
        return True
    except Exception as e:
        logger.error(
            "Failed to send Slack notification: {error}",
            error=str(e),
        )
        return False


def send_spike_alert(
    spikes: list[dict],
    run_date: str,
    webhook_url: str = "",
) -> bool:
    """Send mention-spike anomaly alert to Slack.

    ``spikes`` is a list of dicts with keys: person_name, person_role,
    mentions, zscore — as returned by mart_anomalies_daily.
    Returns False (without raising) when webhook_url is empty or the call fails.
    """
    if not webhook_url:
        webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        logger.debug("SLACK_WEBHOOK_URL not set, skipping spike alert")
        return False

    lines = [
        f"• *{s['person_name']}* ({s['person_role']}) — "
        f"{s['mentions']} menções · Z-Score: {float(s['zscore']):.2f}"
        for s in spikes[:5]
    ]
    suffix = f"\n_...e mais {len(spikes) - 5} anomalia(s)_" if len(spikes) > 5 else ""

    message = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f":red_circle: Spike de menções detectado — {run_date}",
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines) + suffix},
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "_Ver dashboard → Alertas › PQ-011_"}
                ],
            },
        ]
    }

    try:
        response = httpx.post(webhook_url, json=message, timeout=10.0)
        response.raise_for_status()
        logger.info(
            "Spike alert sent for {date} ({n} anomalies)", date=run_date, n=len(spikes)
        )
        return True
    except Exception as e:
        logger.error("Failed to send spike alert: {error}", error=str(e))
        return False


def send_cib_alert(
    clusters: list[dict],
    run_date: str,
    webhook_url: str = "",
) -> bool:
    """Send CIB sustained-cluster alert to Slack.

    ``clusters`` is a list of dicts with keys: community_id, community_size,
    composite_score, avg_synchrony_score, avg_alignment_score, series_age_days
    — as returned by the CIB alert query.
    Returns False (without raising) when webhook_url is empty or the call fails.
    """
    if not webhook_url:
        webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        logger.debug("SLACK_WEBHOOK_URL not set, skipping CIB alert")
        return False

    lines = [
        "• *{}* ({}m, série {}d) — Score: {:.2f} · Sync: {:.2f} · Align: {:.2f}".format(
            c["community_id"],
            c["community_size"],
            c["series_age_days"],
            float(c["composite_score"]),
            float(c["avg_synchrony_score"]),
            float(c["avg_alignment_score"]),
        )
        for c in clusters[:5]
    ]
    suffix = (
        f"\n_...e mais {len(clusters) - 5} cluster(s)_" if len(clusters) > 5 else ""
    )

    message = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f":warning: Cluster CIB sustentado — {run_date}",
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines) + suffix},
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "_Ver dashboard → Alertas › CIB_"}
                ],
            },
        ]
    }

    try:
        response = httpx.post(webhook_url, json=message, timeout=10.0)
        response.raise_for_status()
        logger.info(
            "CIB alert sent for {date} ({n} clusters)", date=run_date, n=len(clusters)
        )
        return True
    except Exception as e:
        logger.error("Failed to send CIB alert: {error}", error=str(e))
        return False
