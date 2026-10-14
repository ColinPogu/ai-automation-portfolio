# n8n Lead-Gen and Outbound Pipeline

An importable n8n workflow that turns a raw list of leads into personalized, ready-to-send outbound drafts, with deduplication and a built-in rate limit so it never blasts your list.

It is a reusable automation: point it at a lead source, connect your API keys, and it writes a tailored first line for every new lead, then logs a draft row you can review before anything is sent.

## What it does

On a manual click or a daily schedule, the workflow:

1. Reads the list of leads you have already contacted (the log sheet).
2. Pulls a fresh batch of leads from a CSV source over HTTP.
3. Normalizes the fields and drops duplicates, both within the batch and against the already-contacted log.
4. Filters out any lead missing an email address.
5. For each remaining lead, calls the Anthropic API to write a short, specific opening line based on the lead's company and role.
6. Assembles a full draft (subject plus body) from that line.
7. Appends the draft as a row to a Google Sheet, which doubles as the dedupe source on the next run.
8. Waits a few seconds between leads so the run is paced, not a burst.

Nothing is emailed automatically. The output is a review queue of drafts, which keeps a human in the loop. Swapping the final step for a send node (Gmail, Outlook, SMTP, or a webhook into your CRM) is a one-node change, described below.

## Stack

- n8n (self-hosted or cloud), workflow export format with `executionOrder: v1`.
- Anthropic Messages API for the per-lead personalization (called via a plain HTTP Request node, so no extra community packages are required).
- Google Sheets as the drafts log and dedupe store (via the built-in Google Sheets node).
- Two small JavaScript Code nodes for CSV parsing, normalization, dedupe, and draft assembly.

All node types used are shipped with a standard n8n install. There is nothing to `npm install`.

## Files

| File | Purpose |
| --- | --- |
| `workflow.json` | The importable n8n workflow. |
| `sample_leads.csv` | Five fake leads to test with. One is intentionally missing an email to show the filter working. |
| `.env.example` | Every config value the workflow reads, with notes. Copy to `.env`. |
| `.gitignore` | Keeps your real `.env` out of version control. |
| `README.md` | This file. |

## Setup

### 1. Get n8n running

Cloud: use your n8n Cloud instance. Self-hosted with Docker:

```
docker run -it --rm --name n8n -p 5678:5678 \
  --env-file .env \
  -v n8n_data:/home/node/.n8n \
  docker.n8n.io/n8nio/n8n
```

Open http://localhost:5678.

### 2. Set the environment variables

Copy `.env.example` to `.env` and fill in:

- `ANTHROPIC_API_KEY` from https://console.anthropic.com
- `ANTHROPIC_MODEL` (optional, defaults to `claude-3-5-haiku-20241022`)
- `LEADS_CSV_URL` a URL that returns your leads as CSV text
- `DRAFTS_SHEET_ID` the id of the Google Sheet used for the drafts log

The workflow reads these inside node expressions as `{{ $env.NAME }}`. If you run n8n through Docker, pass the file with `--env-file .env`. On n8n Cloud, add them under Variables or set them as instance environment variables, then reference them the same way.

A quick way to host the CSV for testing: upload `sample_leads.csv` to any static host, or publish a Google Sheet to the web and use its CSV export link, which looks like `https://docs.google.com/spreadsheets/d/<ID>/export?format=csv`.

### 3. Create the Drafts sheet

Make a Google Sheet with a tab named `Drafts`. Add this header row so the append maps cleanly:

```
contacted_at, first_name, last_name, email, company, role, opening_line, subject, draft_body, status
```

On the first run the sheet can be empty apart from the header. It fills itself and becomes the dedupe source from then on.

### 4. Import the workflow

In n8n: top right menu, **Import from File**, choose `workflow.json`. Or paste its contents into **Import from URL or clipboard**. The canvas loads with all fourteen nodes wired up.

### 5. Connect the Google Sheets credential

The two Google Sheets nodes (`Read Contacted` and `Append Draft Row`) ship with a placeholder credential id. Open each one, click the credential dropdown, and select or create your **Google Sheets OAuth2** credential. The Anthropic call needs no credential in n8n because the key is injected from `$env` in the request header.

### 6. Run it

Click **Execute Workflow** (or **Test Workflow**) to fire the Manual Trigger. Watch each node light up. Open your Drafts sheet to see the new rows. The schedule trigger fires once a day at 09:00 in the instance timezone once the workflow is activated.

## How it works, node by node

```
Manual Trigger  \
                 >-- Read Contacted -- Collapse To One -- Fetch Leads CSV -- Normalize & Dedupe -- Has Valid Email?
Daily Schedule  /                                                                                        |  |
                                                                                            (true) -----/    \----- (false)
                                                                                               |                      |
                                                                                        Loop Over Leads        Skip Missing Email
                                                                                          |  (loop)  \ (done)
                                                                                          |           All Drafts Logged
                                                                             Personalize First Line
                                                                                          |
                                                                                    Assemble Draft
                                                                                          |
                                                                                   Append Draft Row
                                                                                          |
                                                                                   Rate Limit Wait --> back to Loop Over Leads
```

- **Manual Trigger** and **Daily Schedule**. Two entry points into the same flow. The schedule fires daily at 09:00; the manual trigger is for testing and ad hoc runs.

- **Read Contacted** (Google Sheets, read). Reads every row already in the `Drafts` sheet. This is the memory of who has been contacted. `Always Output Data` is on, so an empty sheet still passes a single item downstream and the first run is not blocked.

- **Collapse To One** (Limit, max 1). The read above can return many rows. Without this, the next HTTP node would fire once per row. Limit reduces the stream to a single item so the leads are fetched exactly once. The full contacted list is still available by name to the dedupe node, so nothing is lost.

- **Fetch Leads CSV** (HTTP Request, GET). Downloads `LEADS_CSV_URL` and returns the raw body as text under `json.data`. Swapping this for a Google Sheets read or an S3 node is straightforward; the only contract the next node needs is CSV text, or structured rows if you adapt the parser.

- **Normalize & Dedupe** (Code, run once for all items). Parses the CSV with a small quote-aware parser, lowercases and trims emails, maps loose header names (`title` to `role`, `url` to `website`, and so on) to a clean shape, then removes two kinds of duplicates: repeats inside this batch, and anyone whose email is already in the log. It reads the log through `$('Read Contacted').all()`, which is why the read runs first.

- **Has Valid Email?** (IF). Routes leads with a non-empty email to the true branch and everything else to **Skip Missing Email**, a No-Op that gives skipped leads a clean dead end. In the sample data, the lead with the blank email lands here.

- **Loop Over Leads** (Loop Over Items, batch size 1). Processes one lead at a time. The `loop` output feeds the personalization branch; the `done` output goes to **All Drafts Logged** once every lead is handled. Batching one at a time is what makes the pacing below effective.

- **Personalize First Line** (HTTP Request, POST to Anthropic). Calls `https://api.anthropic.com/v1/messages` with the `x-api-key` and `anthropic-version` headers and a JSON body carrying the model, a tight system prompt, and a user message built from the lead's company, role, and industry. The model returns one short opening sentence.

- **Assemble Draft** (Code, run once for each item). Reads the opening line from the API response and the original lead through the loop's paired item, then builds a subject and a full email body. If the API returns something unusable, a safe fallback line is used so a draft is never empty.

- **Append Draft Row** (Google Sheets, append). Writes the assembled draft to the `Drafts` sheet using auto-map, so the item fields line up with the header columns by name. This row is also what the next run dedupes against.

- **Rate Limit Wait** (Wait, 3 seconds). Pauses before looping back to the next lead. This is the throttle: one lead roughly every three seconds plus API latency, instead of firing the whole list at once. Raise or lower the amount to match your sending reputation and API limits.

- **All Drafts Logged** (No-Op). A clean finish marker on the loop's `done` output.

## Customizing

- **Send instead of draft.** Replace `Append Draft Row` (or add after it) with a Gmail, Microsoft Outlook, SMTP, or HTTP Request node pointed at your CRM. Keep the Wait node in the loop so sends stay paced.
- **Different lead source.** Swap `Fetch Leads CSV` for a Google Sheets read, an Airtable node, or a database query. If it returns structured rows instead of CSV text, simplify the parser in `Normalize & Dedupe` to just map fields.
- **Enrichment.** The task mentions a quick lookup per lead. `website` and `industry` from the CSV already act as lightweight enrichment fed into the prompt. To go further, add an HTTP Request node before `Personalize First Line` that hits your enrichment API and merges the result onto the item.
- **Throttle.** Change the `Rate Limit Wait` amount, or raise `Loop Over Leads` batch size if your API quota allows bigger bursts.

## Notes

- Credentials and ids in `workflow.json` are placeholders. Nothing real ships in the file.
- The Anthropic key is never stored as an n8n credential here; it is read from the environment at call time, which keeps it out of the exported JSON.
- Keep the human review step. Auto-sending cold outreach without a review pass is how you get a domain flagged.

Built by Colin Bustos . Manila, UTC+8 . colinbustos276@gmail.com
