"""Slack alerting through an incoming webhook.

build_message turns an alert payload into a Slack message with Block Kit
formatting and a plain text fallback. send posts it, with retries, and
falls back to logging a preview when there is no webhook or dry run is on.
"""

from __future__ import annotations

import json
import logging
import time

import requests

DEFAULT_LOGGER = logging.getLogger("monitor")


def build_message(payload):
    """Return a Slack webhook body (text plus blocks) from an alert payload."""
    target = payload.get("target", "unknown target")
    headline = payload.get("headline", "Monitor alert")
    url = payload.get("url", "")
    rule = payload.get("rule", "")
    previous = payload.get("previous", "none")
    current = payload.get("current", "n/a")
    ts = payload.get("timestamp", "")

    text = f"Monitor alert for {target}: {headline}"

    fields = [
        {"type": "mrkdwn", "text": f"*Target*\n{target}"},
        {"type": "mrkdwn", "text": f"*Rule*\n{rule}"},
        {"type": "mrkdwn", "text": f"*Previous*\n{previous}"},
        {"type": "mrkdwn", "text": f"*Current*\n{current}"},
    ]
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "Monitor alert"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*{headline}*"}},
        {"type": "section", "fields": fields},
    ]

    context_bits = []
    if url:
        context_bits.append(f"<{url}|open target>")
    if ts:
        context_bits.append(f"checked {ts}")
    if context_bits:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " . ".join(context_bits)}],
        })

    return {"text": text, "blocks": blocks}


def send(webhook_url, payload, dry_run=False, timeout=10, retries=3,
         backoff=2, logger=None):
    """Post an alert to Slack. Returns True on success or dry run.

    On a network error the post is retried up to "retries" times with a
    linear backoff. When there is no webhook or dry_run is set, the alert
    is logged instead of sent, which makes local testing safe.
    """
    logger = logger or DEFAULT_LOGGER
    message = build_message(payload)

    if dry_run or not webhook_url:
        logger.info("event=alert_dry_run target=%s headline=%s",
                    payload.get("target"), payload.get("headline"))
        logger.info("event=alert_preview body=%s", json.dumps(message["text"]))
        return True

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(webhook_url, json=message, timeout=timeout)
            response.raise_for_status()
            logger.info("event=alert_sent target=%s status=%s",
                        payload.get("target"), response.status_code)
            return True
        except requests.RequestException as exc:
            last_err = exc
            logger.warning("event=alert_retry target=%s attempt=%s error=%s",
                           payload.get("target"), attempt, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)

    logger.error("event=alert_failed target=%s error=%s",
                 payload.get("target"), last_err)
    return False
