"""Inbox Triage Agent.

Pulls recent unread Gmail messages, classifies each as ROUTINE or SENSITIVE
with Claude, drafts replies for routine mail (saved as Gmail drafts, never
sent), and routes sensitive mail to a human-review queue.

Reliability rules, all enforced in code (see RELIABILITY.md):

* Draft-first and escalation-gated. Nothing is ever auto-sent; the Gmail
  client exposes no send path (gmail_client.py).
* Durable, concurrency-safe processing. Each message is claimed atomically
  in a SQLite state file (state.py), so re-runs and overlapping runs never
  produce a duplicate draft.
* Confidence-gated. A ROUTINE verdict below MIN_ROUTINE_CONFIDENCE is
  downgraded to human review rather than auto-drafted.
* Schema-safe. Malformed or non-object classifier output fails closed to
  SENSITIVE (parse_classification).
* Bounded spend. A per-run message and dollar ceiling stops the agent no
  matter how full the inbox is (limits.py).
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

import gmail_client
import limits
import prompts
import state

# The anthropic SDK is imported lazily inside main() so that the pure logic in
# this module (parsing, the confidence gate, budgeting) can be imported and
# unit-tested without the SDK or an API key present.


def env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def log_action(log_file, record):
    record["ts"] = datetime.now(timezone.utc).isoformat()
    with open(log_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_classification(text):
    """Pull the JSON object out of Claude's reply, tolerant of stray text.

    Fails safe: any parse problem, or output that is valid JSON but not an
    object (a bare list, string, or number), is treated as SENSITIVE so a bad
    classifier response escalates to a human rather than triggering an
    auto-draft.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {
            "category": "SENSITIVE",
            "reason": "Could not parse classifier output.",
            "confidence": 0.0,
        }
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {
            "category": "SENSITIVE",
            "reason": "Invalid classifier JSON.",
            "confidence": 0.0,
        }
    if not isinstance(data, dict):
        return {
            "category": "SENSITIVE",
            "reason": "Classifier output was not a JSON object.",
            "confidence": 0.0,
        }
    category = str(data.get("category", "")).upper()
    if category not in ("ROUTINE", "SENSITIVE"):
        category = "SENSITIVE"
    try:
        confidence = float(data.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    return {
        "category": category,
        "reason": str(data.get("reason", "")).strip(),
        "confidence": confidence,
    }


def classify(client, model, message):
    """Return (classification dict, usage)."""
    resp = client.messages.create(
        model=model,
        max_tokens=300,
        system=prompts.CLASSIFICATION_SYSTEM,
        messages=[
            {"role": "user", "content": prompts.build_classification_prompt(message)}
        ],
    )
    return parse_classification(resp.content[0].text), resp.usage


def draft_reply(client, model, message, brand_voice, sender_name):
    """Return (draft body text, usage)."""
    resp = client.messages.create(
        model=model,
        max_tokens=800,
        system=prompts.DRAFTING_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": prompts.build_drafting_prompt(
                    message, brand_voice, sender_name
                ),
            }
        ],
    )
    return resp.content[0].text.strip(), resp.usage


def queue_for_review(queue_file, message, result):
    item = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "message_id": message["id"],
        "thread_id": message["thread_id"],
        "from": message["sender"],
        "subject": message["subject"],
        "reason": result["reason"],
        "confidence": result["confidence"],
        "snippet": message["snippet"],
    }
    with open(queue_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return item


def main():
    load_dotenv()

    api_key = env("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "ERROR: ANTHROPIC_API_KEY is not set. "
            "Copy .env.example to .env and fill it in."
        )
        sys.exit(1)

    model = env("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
    query = env("GMAIL_QUERY", "is:unread in:inbox")
    max_messages = int(env("MAX_MESSAGES", "10"))
    max_run_usd = float(env("MAX_RUN_USD", "0.50"))
    price_in = float(env("PRICE_IN_PER_MTOK", "3.0"))
    price_out = float(env("PRICE_OUT_PER_MTOK", "15.0"))
    min_routine_conf = float(env("MIN_ROUTINE_CONFIDENCE", "0.75"))
    brand_voice = env("BRAND_VOICE", prompts.DEFAULT_BRAND_VOICE)
    sender_name = env("SENDER_NAME", "The Support Team")
    review_queue_file = env("REVIEW_QUEUE_FILE", "review_queue.jsonl")
    log_file = env("LOG_FILE", "triage_log.jsonl")
    state_db = env("STATE_DB", "triage_state.db")
    client_secrets = env("GOOGLE_CLIENT_SECRETS", "credentials.json")
    token_file = env("GOOGLE_TOKEN_FILE", "token.json")

    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    service = gmail_client.get_service(client_secrets, token_file)
    budget = limits.RunBudget(max_messages, max_run_usd, price_in, price_out)

    print("Inbox Triage Agent")
    print("Query: %s" % query)
    print("Model: %s" % model)
    print("Limits: %s" % budget.summary())
    print("-" * 60)

    # Ask for more than the message ceiling so already-processed mail does not
    # starve fresh mail of its slots; the budget still caps real work.
    refs = gmail_client.list_unread(service, query, max_messages * 3)
    if not refs:
        print("No messages matched the query. Nothing to do.")
        return

    drafted = 0
    escalated = 0
    skipped = 0
    stopped_on_budget = False

    with state.connect(state_db) as conn:
        for ref in refs:
            try:
                budget.check()
            except limits.BudgetExceeded as exc:
                print("\nStopping: %s" % exc)
                stopped_on_budget = True
                break

            msg_id = ref["id"]
            if state.is_done(conn, msg_id):
                skipped += 1
                continue
            if not state.claim(conn, msg_id):
                # Another run owns it, or it is freshly done. Leave it alone.
                skipped += 1
                continue

            try:
                message = gmail_client.get_message(service, msg_id)
                result, usage = classify(client, model, message)
                budget.record_usage(usage)
                budget.record_message()

                category = result["category"]
                # Confidence gate: a shaky ROUTINE is escalated, not drafted.
                if category == "ROUTINE" and result["confidence"] < min_routine_conf:
                    category = "SENSITIVE"
                    result = dict(result)
                    result["reason"] = (
                        "Low confidence (%.2f < %.2f): %s"
                        % (result["confidence"], min_routine_conf, result["reason"])
                    )

                print("\nFrom:    %s" % message["sender"])
                print("Subject: %s" % message["subject"])
                print(
                    "Class:   %s (confidence %.2f)"
                    % (category, result["confidence"])
                )
                print("Reason:  %s" % result["reason"])

                if category == "ROUTINE":
                    body, draft_usage = draft_reply(
                        client, model, message, brand_voice, sender_name
                    )
                    budget.record_usage(draft_usage)
                    draft_id = gmail_client.create_draft(
                        service,
                        to=message["sender"],
                        subject=message["subject"],
                        body_text=body,
                        thread_id=message["thread_id"],
                        in_reply_to=message["message_id_header"],
                    )
                    drafted += 1
                    print(
                        "Action:  ROUTINE. Draft saved to Gmail (draft id %s). "
                        "Not sent." % draft_id
                    )
                    state.mark_done(conn, msg_id, "draft_created", draft_id)
                    log_action(
                        log_file,
                        {
                            "action": "draft_created",
                            "message_id": msg_id,
                            "draft_id": draft_id,
                            "category": category,
                            "confidence": result["confidence"],
                            "reason": result["reason"],
                        },
                    )
                else:
                    queue_for_review(review_queue_file, message, result)
                    escalated += 1
                    print(
                        "Action:  SENSITIVE. Added to human-review queue. "
                        "No draft written."
                    )
                    state.mark_done(conn, msg_id, "escalated")
                    log_action(
                        log_file,
                        {
                            "action": "escalated",
                            "message_id": msg_id,
                            "category": category,
                            "confidence": result["confidence"],
                            "reason": result["reason"],
                        },
                    )
            except Exception as exc:  # noqa: BLE001 - keep the run alive
                # The work failed after we claimed it. Release the claim so a
                # later run retries this message instead of losing it.
                state.release(conn, msg_id)
                print("Action:  ERROR (%s). Claim released for retry." % exc)
                log_action(
                    log_file,
                    {"action": "error", "message_id": msg_id, "error": str(exc)},
                )

    print("\n" + "=" * 60)
    print(
        "Done. %d drafted, %d escalated, %d skipped (already handled)."
        % (drafted, escalated, skipped)
    )
    print("Budget: %s" % budget.summary())
    if stopped_on_budget:
        print("Run stopped early on its budget ceiling; re-run to continue.")
    if escalated:
        print("Review the escalation queue: %s" % review_queue_file)
    print("Full action log: %s" % log_file)


if __name__ == "__main__":
    main()
