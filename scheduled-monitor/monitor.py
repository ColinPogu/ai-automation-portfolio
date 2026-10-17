#!/usr/bin/env python3
"""Scheduled Monitor: scrape, analyze, alert.

Fetches a configurable list of web pages or JSON APIs, extracts a value
with a selector, stores every reading in SQLite, compares the new reading
against the previous one using a per target rule, and posts a Slack alert
when a rule is crossed (price drop, back in stock, count spike, keyword
appears, content change).

Run a single pass:
    python monitor.py --once

Run forever on the interval from config.yaml:
    python monitor.py --loop

Secrets come from the environment (see .env.example). Nothing is hardcoded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time

import requests
import yaml

import notify
import storage

logger = logging.getLogger("monitor")

DEFAULTS = {
    "timeout_seconds": 20,
    "retries": 3,
    "retry_backoff_seconds": 2,
    "user_agent": "ScheduledMonitor/1.0 (+https://example.com/monitor-bot)",
}

_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def fmt(value):
    """Format a number for humans, dropping a trailing .0."""
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return str(int(value)) if value == int(value) else f"{value:.2f}"
    return str(value)


def _field(value):
    """Quote a logfmt field value if it contains a space or equals sign."""
    text = str(value)
    if text == "" or " " in text or "=" in text:
        return '"' + text.replace('"', "'") + '"'
    return text


def log_event(level, event, **fields):
    """Emit a single logfmt style structured log line."""
    parts = [f"event={event}"]
    parts.extend(f"{key}={_field(val)}" for key, val in fields.items())
    logger.log(level, " ".join(parts))


def load_env_file(path=".env"):
    """Load KEY=VALUE pairs from a .env file, no extra dependency needed.

    Real environment variables always win, so .env only fills the gaps.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(),
                                  value.strip().strip('"').strip("'"))


def configure_logging(level_name):
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="ts=%(asctime)s level=%(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    logging.Formatter.converter = time.gmtime


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------
def parse_number(text):
    """Pull the first number out of a string, ignoring currency and commas."""
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def extract_html(html, selector):
    """Return (text, matched) for a CSS selector against an HTML document."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    if not selector:
        return soup.get_text(" ", strip=True), True
    element = soup.select_one(selector)
    if element is None:
        return None, False
    return element.get_text(" ", strip=True), True


def extract_json(payload, path):
    """Return (value, matched) for a dotted path like \"data.items.0.price\"."""
    current = payload
    for part in (path or "").split("."):
        if part == "":
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None, False
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None, False
    return current, True


def derive_in_stock(extract, matched, value, raw_text, keyword):
    """Turn a reading into a 0/1/None in stock flag for stock rules."""
    if extract == "exists":
        return 1 if matched else 0
    if extract == "number":
        return None if value is None else (1 if value > 0 else 0)
    if keyword:
        if not raw_text:
            return 0
        return 1 if keyword.lower() in raw_text.lower() else 0
    return None


# ---------------------------------------------------------------------------
# fetching, with retries
# ---------------------------------------------------------------------------
def fetch_raw(session, url, timeout, retries, backoff, name):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_err = exc
            log_event(logging.WARNING, "fetch_retry", target=name,
                      attempt=attempt, max=retries, error=exc)
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise last_err


# ---------------------------------------------------------------------------
# rule engine (edge triggered, so a steady state does not re-alert)
# ---------------------------------------------------------------------------
def evaluate(rule, prev, curr, threshold, keyword):
    """Decide whether this reading should fire an alert.

    Rules compare the current reading (curr, a dict) against the previous
    successful reading (prev, a sqlite Row or None). The comparison is edge
    triggered: an alert fires only when the condition newly becomes true,
    so running the same pass twice does not send a duplicate.
    """
    rule = (rule or "change").lower()
    cv = curr["value"]
    pv = prev["value"] if prev else None

    if rule == "below":
        if cv is None or threshold is None:
            return False, ""
        if cv <= threshold and not (pv is not None and pv <= threshold):
            return True, f"Value {fmt(cv)} is at or below {fmt(threshold)}"
        return False, ""

    if rule == "above":
        if cv is None or threshold is None:
            return False, ""
        if cv >= threshold and not (pv is not None and pv >= threshold):
            return True, f"Value {fmt(cv)} is at or above {fmt(threshold)}"
        return False, ""

    if rule == "decrease":
        if cv is None or pv is None or threshold is None:
            return False, ""
        if (pv - cv) >= threshold:
            return True, f"Value fell {fmt(pv - cv)}, from {fmt(pv)} to {fmt(cv)}"
        return False, ""

    if rule == "increase":
        if cv is None or pv is None or threshold is None:
            return False, ""
        if (cv - pv) >= threshold:
            return True, f"Value rose {fmt(cv - pv)}, from {fmt(pv)} to {fmt(cv)}"
        return False, ""

    if rule == "back_in_stock":
        now_in = curr["in_stock"] == 1
        was_in = prev is not None and prev["in_stock"] == 1
        if now_in and not was_in:
            return True, "Back in stock"
        return False, ""

    if rule == "contains":
        kw = (keyword or "").lower()
        now_has = bool(kw) and kw in (curr["raw_text"] or "").lower()
        prev_text = (prev["raw_text"] or "").lower() if prev else ""
        was_has = bool(kw) and kw in prev_text
        if now_has and not was_has:
            return True, f"Keyword '{keyword}' is present"
        return False, ""

    # default rule: change
    if cv is not None and pv is not None:
        delta = abs(cv - pv)
        if delta > 0 and delta >= (threshold or 0):
            return True, f"Value changed from {fmt(pv)} to {fmt(cv)}"
        return False, ""
    if prev is not None and curr["raw_text"] != prev["raw_text"]:
        return True, "Content changed"
    return False, ""


def display_reading(reading):
    """A short human readable summary of a reading for the alert body."""
    if reading is None:
        return "none"
    if reading["value"] is not None:
        return fmt(reading["value"])
    if reading["raw_text"]:
        text = reading["raw_text"]
        return text if len(text) <= 80 else text[:77] + "..."
    if reading["in_stock"] is not None:
        return "in stock" if reading["in_stock"] == 1 else "out of stock"
    return "n/a"


# ---------------------------------------------------------------------------
# one target, one pass, and the loop
# ---------------------------------------------------------------------------
def check_target(session, conn, target, defaults, webhook, dry_run):
    """Fetch, parse, store, evaluate, and alert for a single target.

    Returns True when an alert fired. Any failure is recorded as an error
    reading and logged, and never crashes the rest of the pass.
    """
    name = target.get("name") or target.get("url", "unnamed")
    url = target["url"]
    ttype = target.get("type", "html")
    selector = target.get("selector")
    extract = target.get("extract", "text")
    rule = target.get("rule", "change")
    threshold = target.get("threshold")
    keyword = target.get("keyword")

    prev = storage.last_reading(conn, name)
    ts = storage.utc_now_iso()

    try:
        response = fetch_raw(
            session, url,
            defaults["timeout_seconds"], defaults["retries"],
            defaults["retry_backoff_seconds"], name,
        )
        if ttype == "json":
            selected, matched = extract_json(response.json(), selector)
            raw_text = None if selected is None else str(selected)
        else:
            raw_text, matched = extract_html(response.text, selector)

        value = parse_number(raw_text) if extract == "number" else None
        in_stock = derive_in_stock(extract, matched, value, raw_text, keyword)
        reading = {
            "target": name, "url": url, "status": "ok",
            "raw_text": raw_text, "value": value, "in_stock": in_stock,
            "error": None, "ts": ts,
        }
    except Exception as exc:  # network, decode, or parse failure
        reading = {
            "target": name, "url": url, "status": "error",
            "raw_text": None, "value": None, "in_stock": None,
            "error": str(exc), "ts": ts,
        }
        storage.save_reading(conn, reading)
        log_event(logging.ERROR, "check_error", target=name, error=exc)
        return False

    triggered, headline = evaluate(rule, prev, reading, threshold, keyword)

    # Enqueue the alert into the durable outbox BEFORE advancing the baseline.
    # If the process dies between these two writes, the worst case is a
    # pending alert with no saved reading, which redelivers the same alert.
    # That is the safe failure direction: we never silently drop an alert.
    if triggered:
        payload = {
            "target": name,
            "url": url,
            "rule": rule,
            "headline": headline,
            "previous": display_reading(prev),
            "current": display_reading(reading),
            "timestamp": ts,
        }
        if storage.enqueue_alert(conn, alert_key(payload), json.dumps(payload)):
            log_event(logging.INFO, "alert_enqueued", target=name,
                      key=alert_key(payload))

    storage.save_reading(conn, reading)
    log_event(logging.INFO, "check_ok", target=name, rule=rule,
              value=reading["value"], in_stock=reading["in_stock"],
              triggered=triggered)
    return triggered


def alert_key(payload):
    """Stable, collision-resistant key for one alert event.

    Built from the fields that identify a distinct firing. Enqueuing the same
    event twice is a no-op (unique key), so overlapping passes cannot create
    duplicate messages.
    """
    basis = "|".join(
        str(payload.get(field, ""))
        for field in ("target", "timestamp", "rule", "headline")
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def flush_pending(conn, webhook, defaults, dry_run):
    """Attempt delivery of every pending alert; keep failures for next time.

    This is the whole durability guarantee: an alert leaves the outbox only
    after notify.send returns True. A failed Slack post stays pending and is
    retried on the next pass, no matter how far the reading baseline has
    moved on.
    """
    delivered = 0
    still_pending = 0
    for row in storage.list_pending(conn):
        payload = json.loads(row["payload"])
        storage.mark_attempt(conn, row["id"])
        ok = notify.send(
            webhook, payload, dry_run=dry_run,
            retries=defaults["retries"],
            backoff=defaults["retry_backoff_seconds"],
            logger=logger,
        )
        if ok:
            storage.delete_alert(conn, row["id"])
            delivered += 1
        else:
            still_pending += 1
            log_event(logging.WARNING, "alert_still_pending",
                      target=payload.get("target"), key=row["alert_key"],
                      attempts=row["attempts"] + 1)
    if delivered or still_pending:
        log_event(logging.INFO, "flush_done",
                  delivered=delivered, still_pending=still_pending)
    return delivered, still_pending


def merge_defaults(config):
    merged = dict(DEFAULTS)
    merged.update(config.get("defaults") or {})
    return merged


def run_once(config, db_path, webhook, dry_run, only=None):
    """Run a single pass over every target. Returns the number of alerts."""
    defaults = merge_defaults(config)
    session = requests.Session()
    session.headers.update({"User-Agent": defaults["user_agent"]})
    targets = config.get("targets") or []

    checked = 0
    alerts = 0
    with storage.connect(db_path) as conn:
        # Retry anything a previous pass could not deliver, first thing.
        flush_pending(conn, webhook, defaults, dry_run)

        for target in targets:
            if only and target.get("name") != only:
                continue
            checked += 1
            try:
                if check_target(session, conn, target, defaults, webhook, dry_run):
                    alerts += 1
            except Exception as exc:  # defensive, keep the pass alive
                log_event(logging.ERROR, "target_failed",
                          target=target.get("name", target.get("url")), error=exc)

        # Deliver everything this pass just enqueued; survivors stay pending.
        flush_pending(conn, webhook, defaults, dry_run)
        outstanding = storage.count_pending(conn)
    log_event(logging.INFO, "pass_done", checked=checked, alerts=alerts,
              pending=outstanding, dry_run=dry_run)
    return alerts


def run_loop(config, db_path, webhook, dry_run, interval_minutes, only=None):
    """Run passes forever, sleeping between them. Ctrl C stops cleanly."""
    interval = max(1.0, float(interval_minutes)) * 60.0
    log_event(logging.INFO, "loop_start", interval_minutes=interval_minutes)
    try:
        while True:
            start = time.time()
            try:
                run_once(config, db_path, webhook, dry_run, only=only)
            except Exception as exc:
                log_event(logging.ERROR, "pass_crashed", error=exc)
            sleep_for = max(1.0, interval - (time.time() - start))
            log_event(logging.INFO, "loop_sleep", seconds=round(sleep_for))
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        log_event(logging.INFO, "loop_stop", reason="keyboard_interrupt")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def load_config(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    data.setdefault("defaults", {})
    data.setdefault("targets", [])
    data.setdefault("schedule", {})
    return data


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scheduled scrape to analyze to alert monitor.")
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "config.yaml"),
                        help="path to the YAML config (default config.yaml)")
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "monitor.db"),
                        help="path to the SQLite database (default monitor.db)")
    parser.add_argument("--loop", action="store_true",
                        help="run forever on the configured interval")
    parser.add_argument("--once", action="store_true",
                        help="run a single pass (this is the default)")
    parser.add_argument("--interval", type=float,
                        help="override the loop interval, in minutes")
    parser.add_argument("--target", help="only check the target with this name")
    parser.add_argument(
        "--dry-run", action="store_true",
        default=os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes"),
        help="log alerts instead of posting them to Slack")
    return parser.parse_args()


def main():
    load_env_file()
    args = parse_args()
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))

    config = load_config(args.config)
    webhook = os.getenv("SLACK_WEBHOOK_URL", "")
    dry_run = args.dry_run

    if not webhook and not dry_run:
        log_event(logging.WARNING, "no_webhook",
                  note="SLACK_WEBHOOK_URL is not set, forcing dry run")
        dry_run = True

    if args.loop:
        interval = args.interval or config.get("schedule", {}).get("every_minutes", 30)
        run_loop(config, args.db, webhook, dry_run, interval, only=args.target)
    else:
        run_once(config, args.db, webhook, dry_run, only=args.target)


if __name__ == "__main__":
    main()
