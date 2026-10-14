"""Gmail helpers: OAuth login, list, fetch, and draft creation.

Only two scopes are requested, both draft-first. There is no gmail.send
scope here on purpose, so this client physically cannot send mail even if
something downstream asked it to.
"""

import base64
import os
import re
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Read mail and create drafts only. No send scope by design.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


def get_service(client_secrets, token_file):
    """Return an authorized Gmail API service.

    Caches the OAuth token in token_file after the first browser login and
    refreshes it silently on later runs.
    """
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(client_secrets):
                raise FileNotFoundError(
                    "Missing OAuth client secrets file: %s. Download it from the "
                    "Google Cloud Console and point GOOGLE_CLIENT_SECRETS at it."
                    % client_secrets
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w", encoding="utf-8") as handle:
            handle.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def list_unread(service, query, max_results):
    """Return a list of message id references matching query, newest first."""
    response = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    return response.get("messages", [])


def get_message(service, msg_id):
    """Fetch one message and return a parsed, flat dict."""
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="full")
        .execute()
    )
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    body = _decode_body(payload)
    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId"),
        "sender": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", "(no subject)"),
        "message_id_header": headers.get("message-id", ""),
        "snippet": msg.get("snippet", ""),
        "body": body.strip(),
    }


def create_draft(service, to, subject, body_text, thread_id=None, in_reply_to=""):
    """Create a Gmail draft reply and return its draft id. Never sends."""
    mime = MIMEText(body_text, "plain", "utf-8")
    mime["To"] = to
    reply_subject = subject if subject.lower().startswith("re:") else "Re: " + subject
    mime["Subject"] = reply_subject
    if in_reply_to:
        mime["In-Reply-To"] = in_reply_to
        mime["References"] = in_reply_to
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
    message = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    draft = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": message})
        .execute()
    )
    return draft.get("id")


# Body decoding helpers. Gmail payloads are a nested MIME tree with
# base64url encoded parts, so we walk it and prefer plain text.

def _decode_body(payload):
    plain = _find_part(payload, "text/plain")
    if plain:
        return plain
    html = _find_part(payload, "text/html")
    if html:
        return _strip_html(html)
    data = payload.get("body", {}).get("data")
    if data:
        return _b64(data)
    return ""


def _find_part(payload, mime_type):
    if payload.get("mimeType") == mime_type:
        data = payload.get("body", {}).get("data")
        if data:
            return _b64(data)
    for part in payload.get("parts", []) or []:
        found = _find_part(part, mime_type)
        if found:
            return found
    return ""


def _b64(data):
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", "replace")


def _strip_html(html):
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
