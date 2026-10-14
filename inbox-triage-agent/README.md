# Inbox Triage Agent

An email triage assistant that reads recent unread Gmail messages, decides
which ones a human needs to handle, and prepares safe draft replies for the
rest. It never sends anything. Every routine reply lands in your Gmail Drafts
folder for you to review and send, and every sensitive message is held back in
a review queue.

## What it does

For each unread message it processes, the agent:

1. Classifies the message as ROUTINE or SENSITIVE using Claude. SENSITIVE means
   refunds, complaints, legal matters, billing disputes, cancellations, or
   anything that needs a human judgment call.
2. For ROUTINE messages, drafts a reply with Claude in a configurable brand
   voice and saves it as a Gmail draft. It is never sent.
3. For SENSITIVE messages, it does not draft anything. It writes the item to a
   human-review queue (a local JSONL file) and prints a summary so a person can
   pick it up.

The core design point is simple: draft-first and escalation-gated. Nothing is
ever auto-sent, and when the classifier is unsure it escalates rather than
guessing.

## Stack

- Python 3.9 or newer
- Anthropic Claude via the official `anthropic` Python SDK
- Gmail API via `google-api-python-client` with OAuth
- `python-dotenv` for configuration

## Files

- `triage.py` - entry point: classification, drafting, and routing
- `gmail_client.py` - OAuth login, list, fetch, and draft-creation helpers
- `prompts.py` - classification and drafting prompts, with the brand-voice knob
- `requirements.txt` - Python dependencies
- `.env.example` - configuration template

## Setup

1. Create and activate a virtual environment, then install dependencies:

   ```
   python -m venv .venv
   .venv\Scripts\activate        (Windows)
   source .venv/bin/activate     (macOS or Linux)
   pip install -r requirements.txt
   ```

2. Enable the Gmail API and get OAuth credentials:
   - Go to the Google Cloud Console and create a project.
   - Enable the Gmail API for that project.
   - Create an OAuth client ID of type Desktop app.
   - Download the client secrets JSON and save it next to the code as
     `credentials.json` (or point `GOOGLE_CLIENT_SECRETS` at wherever you put it).

3. Copy `.env.example` to `.env` and fill it in. At minimum set
   `ANTHROPIC_API_KEY`. Adjust `BRAND_VOICE`, `GMAIL_QUERY`, and `SENDER_NAME`
   to taste.

## Run

```
python triage.py
```

On the first run a browser window opens for the Google sign-in. After you
approve access, the token is cached in `token.json` and later runs are silent.
The script prints each message, its classification, and the action taken, then
a final summary.

Check the results in two places: your Gmail Drafts folder for the routine
replies, and `review_queue.jsonl` for the sensitive messages waiting on a human.

## How it works

`triage.py` loads config from the environment, authorizes a Gmail service, and
asks Gmail for the most recent messages that match `GMAIL_QUERY`. For each one
it calls Claude twice at most: once to classify, and, only when the message is
routine, a second time to draft a reply.

The classifier is instructed to return structured JSON with `category`,
`reason`, and `confidence`. The parser is deliberately strict and fails safe:
if the JSON is missing or malformed, the message is treated as SENSITIVE and
escalated instead of being auto-drafted. Routine drafts are created with the
Gmail `drafts.create` call inside the original thread, so your reply threads
correctly when you choose to send it.

The OAuth scopes requested are `gmail.readonly` and `gmail.compose`. There is no
send scope, so the agent cannot send email even by accident. Every run appends a
structured record of each action to `triage_log.jsonl`, giving you an audit
trail of what was drafted and what was escalated.

## Reusing it for a client

The agent is generic. To re-skin it for a different mailbox or brand, change the
`.env` values: set `BRAND_VOICE` and `SENDER_NAME` for the client's tone, and
set `GMAIL_QUERY` to target the right messages (for example
`is:unread label:support newer_than:2d`). No code changes needed.

---

Built by Colin Bustos . Manila, UTC+8 . colinbustos276@gmail.com
