# Scheduled Monitor

A small, dependable watchdog. On a schedule it fetches a list of web pages or
JSON APIs, pulls out one value from each (a price, a stock status, a count, a
keyword), stores every reading in a local SQLite database, compares the newest
reading against the previous one, and posts a Slack alert when a rule is
crossed: a price drop, a back in stock flip, a count spike, a keyword
appearing, or any content change.

It is built to run unattended. Fetches retry on network errors, a failed
target never kills the rest of the pass, alerts are edge triggered so a steady
state does not spam your channel, and every pass writes structured logs.

## What it is good for

- Price watching, alert when a product drops to or below your target
- Back in stock alerts, fire the moment an item is available again
- Count or metric spikes, watch an API number and alert on a jump
- Status and keyword watching, alert when a page starts saying "degraded"
- Change detection, alert when a value or block of text changes at all

## Stack

- Python 3.9 or newer
- requests for fetching
- beautifulsoup4 for HTML parsing (CSS selectors)
- pyyaml for the config file
- sqlite3 and the standard library for storage, scheduling, and logging

No database server, no message queue, no extra services. One folder, one
SQLite file.

## Project layout

```
scheduled-monitor/
  monitor.py        fetch, parse, store, compare, alert, plus the CLI and loop
  storage.py        SQLite schema and read/write helpers
  notify.py         Slack webhook message building and posting
  config.yaml       the list of targets and the rules for each
  requirements.txt  pinned dependencies
  .env.example      the one secret, your Slack webhook URL
  README.md         this file
```

## Setup

```bash
# 1. create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate

# 2. install dependencies
pip install -r requirements.txt

# 3. add your Slack webhook
cp .env.example .env             # on Windows: copy .env.example .env
# then edit .env and paste your real webhook URL
```

Get a Slack incoming webhook at https://api.slack.com/messaging/webhooks. Paste
the URL into `.env` as `SLACK_WEBHOOK_URL`.

## Configure your targets

Edit `config.yaml`. Each target has a URL, a selector, an extract mode, and a
rule. The shipped file has four worked examples (price drop, back in stock,
count spike, keyword watch) that you can copy and adjust.

Extract modes:

| extract  | meaning                                                     |
| -------- | ----------------------------------------------------------- |
| `number` | parse the first number from the matched text (price, count) |
| `text`   | keep the matched text as is                                 |
| `exists` | did the selector match anything (true or false)             |

Rules:

| rule            | fires when                                                        |
| --------------- | ---------------------------------------------------------------- |
| `below`         | the number is at or below `threshold`                            |
| `above`         | the number is at or above `threshold`                            |
| `decrease`      | the number fell by at least `threshold` since the last reading   |
| `increase`      | the number rose by at least `threshold` since the last reading   |
| `back_in_stock` | the item went from out of stock to in stock                      |
| `contains`      | the text now contains `keyword` and did not before               |
| `change`        | the value or text changed (optionally by at least `threshold`)   |

For `html` targets `selector` is a CSS selector, for example `span.price` or
`#system-status`. For `json` targets `selector` is a dotted path into the
response, for example `data.signups_today` or `items.0.price`.

## Run

Run a single pass (the default), useful for a cron job or a quick test:

```bash
python monitor.py --once
```

Run forever on the interval set by `schedule.every_minutes` in `config.yaml`:

```bash
python monitor.py --loop
```

Handy flags:

```bash
python monitor.py --loop --interval 10     # override the interval, in minutes
python monitor.py --once --target "Widget price"   # check just one target
python monitor.py --once --dry-run         # log alerts instead of sending them
python monitor.py --once --config other.yaml --db other.db
```

If `SLACK_WEBHOOK_URL` is not set, the monitor automatically runs in dry run
mode and logs what it would have sent, so you can try it with zero setup.

### Running on a schedule with cron

The built in `--loop` keeps the process alive and times itself. If you would
rather let the operating system schedule it, use `--once` from cron:

```cron
# every 30 minutes
*/30 * * * * cd /path/to/scheduled-monitor && /path/to/.venv/bin/python monitor.py --once >> monitor.log 2>&1
```

On Windows, Task Scheduler can run the same `--once` command on a trigger.

## How it works

1. Load `.env` and `config.yaml`, then open the SQLite database, creating the
   schema on first run.
2. For each target, fetch the URL with retries and a linear backoff.
3. Extract the value: a CSS selector for HTML, a dotted path for JSON. Parse a
   number if the target asks for one, and derive an in stock flag for stock
   rules.
4. Look up the previous successful reading for that target and evaluate the
   rule against the new one. Rules are edge triggered, so an alert fires only
   when the condition newly becomes true. Running the same pass twice does not
   send a duplicate, which keeps the tool idempotent.
5. Store the new reading with a UTC timestamp, so the database keeps a full
   history you can query with plain SQL.
6. If the rule fired, post a formatted Slack message through the webhook, with
   its own retries.

Every reading, retry, alert, and pass is written as a structured log line in
`event=... key=value` (logfmt) form, so the output is easy to read and easy to
grep or ship to a log collector.

## Inspect the history

Everything is in `monitor.db`. For example:

```bash
sqlite3 monitor.db "SELECT ts, target, value, in_stock, status FROM readings ORDER BY id DESC LIMIT 20;"
```

## Notes and limits

- This reads pages as a plain HTTP client. It does not run JavaScript, so for
  pages that render their price in the browser, point the target at the
  underlying API (use `type: json`) or at a server rendered version.
- Be a polite client: keep the interval reasonable and the default User Agent
  (or set your own in `config.yaml`) so the sites you watch can identify the
  traffic.

---

Built by Colin Bustos . Manila, UTC+8 . colinbustos276@gmail.com
