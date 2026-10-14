"""Inbox Triage Agent.

Pulls recent unread Gmail messages, classifies each as ROUTINE or SENSITIVE
with Claude, drafts replies for routine mail (saved as Gmail drafts, never
sent), and routes sensitive mail to a human-review queue.

Design rule: draft-first and escalation-gated. Nothing is ever auto-sent.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

from anthropic import Anthropic
from dotenv import load_dotenv

import gmail_client
import prompts


def env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def log_action(log_file, record):
    record["ts"] = datetime.now(timezone.utc).isoformat()
    with open(log_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_classification(text):
    """Pull the JSON object out of Claude's reply, tolerant of stray text.

    Fails safe: any parse problem is treated as SENSITIVE so a bad classifier
    response escalates to a human rather than triggering an auto-draft.
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
    category = str(data.get("category", "")).upper()
    if category not in ("ROUTINE", "SENSITIVE"):
        category = "SENSITIVE"
    try:
        confidence = float(data.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "category": category,
        "reason": str(data.get("reason", "")).strip(),
        "confidence": confidence,
    }


def classify(client, model, message):
    resp = client.messages.create(
        model=model,
        max_tokens=300,
        system=prompts.CLASSIFICATION_SYSTEM,
        messages=[
            {"role": "user", "content": prompts.build_classification_prompt(message)}
        ],
    )
    return parse_classification(resp.content[0].text)


def draft_reply(client, model, message, brand_voice, sender_name):
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
    return resp.content[0].text.strip()


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
    brand_voice = env("BRAND_VOICE", prompts.DEFAULT_BRAND_VOICE)
    sender_name = env("SENDER_NAME", "The Support Team")
    review_queue_file = env("REVIEW_QUEUE_FILE", "review_queue.jsonl")
    log_file = env("LOG_FILE", "triage_log.jsonl")
    client_secrets = env("GOOGLE_CLIENT_SECRETS", "credentials.json")
    token_file = env("GOOGLE_TOKEN_FILE", "token.json")

    client = Anthropic(api_key=api_key)
    service = gmail_client.get_service(client_secrets, token_file)

    print("Inbox Triage Agent")
    print("Query: %s" % query)
    print("Model: %s" % model)
    print("-" * 60)

    refs = gmail_client.list_unread(service, query, max_messages)
    if not refs:
        print("No messages matched the query. Nothing to do.")
        return

    drafted = 0
    escalated = 0

    for ref in refs:
        message = gmail_client.get_message(service, ref["id"])
        result = classify(client, model, message)

        print("\nFrom:    %s" % message["sender"])
        print("Subject: %s" % message["subject"])
        print(
            "Class:   %s (confidence %.2f)"
            % (result["category"], result["confidence"])
        )
        print("Reason:  %s" % result["reason"])

        if result["category"] == "ROUTINE":
            body = draft_reply(client, model, message, brand_voice, sender_name)
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
                "Action:  ROUTINE. Draft saved to Gmail (draft id %s). Not sent."
                % draft_id
            )
            log_action(
                log_file,
                {
                    "action": "draft_created",
                    "message_id": message["id"],
                    "draft_id": draft_id,
                    "category": result["category"],
                    "confidence": result["confidence"],
                    "reason": result["reason"],
                },
            )
        else:
            queue_for_review(review_queue_file, message, result)
            escalated += 1
            print("Action:  SENSITIVE. Added to human-review queue. No draft written.")
            log_action(
                log_file,
                {
                    "action": "escalated",
                    "message_id": message["id"],
                    "category": result["category"],
                    "confidence": result["confidence"],
                    "reason": result["reason"],
                },
            )

    print("\n" + "=" * 60)
    print(
        "Done. %d processed: %d drafted, %d escalated."
        % (len(refs), drafted, escalated)
    )
    if escalated:
        print("Review the escalation queue: %s" % review_queue_file)
    print("Full action log: %s" % log_file)


if __name__ == "__main__":
    main()
