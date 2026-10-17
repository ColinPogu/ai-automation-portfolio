# Reliability notes

These are the failure modes that separate a demo from something you can leave
running against a real inbox or a real alerting channel. Each item says what
the risk is, where it is handled in the code, and the test that proves it.
Run the tests from either project folder with:

    python -m unittest discover -s tests -t . -v

30 tests, no network or API key required (the pure logic is importable
without the Anthropic SDK installed).

---

## Inbox Triage Agent

### 1. No duplicate drafts on re-runs (durable processed state)
A naive "loop over unread mail" drafts a second reply every time it runs,
because the message is still unread. Every message is recorded in a SQLite
state file the moment it is claimed, and a message marked done is never
processed again.
- Code: `inbox-triage-agent/state.py`, wired in `triage.py` (`state.is_done`,
  `state.claim`, `state.mark_done`).
- Tests: `test_state.py::test_done_message_never_reclaims`.

### 2. Atomic duplicate prevention under concurrency
If two runs overlap (cron overlap, a manual run on top of a scheduled one),
both could draft the same message. Claiming a message is a single
`INSERT OR IGNORE`; SQLite serializes writers, so exactly one process wins
and the other sees zero rows changed and skips. No lock file to leak.
- Code: `state.claim`.
- Tests: `test_state.py::test_claim_is_won_once`,
  `test_fresh_claim_is_not_reclaimable`.

### 3. Crash recovery
A run that dies mid-message would otherwise strand it as "claimed" forever.
A claim older than `STALE_CLAIM_SECONDS` is atomically reclaimable, and a
message whose work throws is explicitly released for the next run to retry.
- Code: `state.claim` (stale takeover), `state.release`; the `except` block
  in `triage.main`.
- Tests: `test_state.py::test_stale_claim_is_reclaimable`,
  `test_released_claim_can_be_retaken`.

### 4. Schema and confidence handling for bad model output
Model output is never trusted to be well formed. Non-JSON, invalid JSON, or
valid JSON that is not an object (a bare list, string, or number) all fail
closed to SENSITIVE, so a malformed response escalates to a human rather than
auto-drafting. A ROUTINE verdict below `MIN_ROUTINE_CONFIDENCE` is also
downgraded to human review.
- Code: `triage.parse_classification` (object guard, clamping), the
  confidence gate in `triage.main`.
- Tests: `test_classification.py` (9 cases, incl. array/scalar/unknown
  category/garbage confidence).

### 5. The draft-only boundary (the gmail.compose question)
Google has no "create drafts but can never send" scope. `gmail.compose` is
the least-privilege scope that can write a draft, and it technically also
permits `drafts.send` / `messages.send`. So the boundary is enforced in the
application layer, three ways: the code has no send call anywhere, it refuses
to start if a send-capable scope is ever added, and the OAuth client is a
per-user desktop app with no domain-wide delegation. The consent screen will
still mention sending; that is the known gap these controls cover.
- Code: `gmail_client.py` (`SCOPES`, `FORBIDDEN_SCOPES`,
  `_assert_safe_scopes`), which only ever calls `drafts().create`.
- Tests: `test_no_send.py` (static scan for any send call + scope assertions).

### 6. Bounded spend and delegation
The agent will not run away. It stops before starting a new message once it
hits either a message ceiling or a per-run dollar ceiling, estimated from the
token usage the API reports.
- Code: `limits.py` (`RunBudget`), checked each loop in `triage.main`.
- Tests: `test_limits.py` (message ceiling, dollar ceiling, cost math).

---

## Scheduled Monitor

### 7. A failed alert is never lost after the baseline advances
The hard one. The rule engine is edge-triggered, so once a reading is saved
as the new baseline the condition will not fire again. If delivery happened
after saving and then failed, the alert was gone. Fixed with a durable
outbox: the alert is written to `pending_alerts` before the baseline
advances, and removed only after Slack confirms receipt. A failed post stays
pending and is retried at the start of every later pass, independent of the
baseline.
- Code: `storage.py` (`pending_alerts` table + `enqueue_alert`,
  `list_pending`, `delete_alert`), `monitor.flush_pending`,
  `monitor.check_target` (enqueue before `save_reading`), `monitor.run_once`
  (flush before and after each pass).
- Tests: `test_pending.py::test_failed_delivery_stays_pending_then_delivers`.

### 8. No duplicate alerts from overlapping passes
Each alert has a stable key (hash of target + timestamp + rule + headline).
Enqueuing is `INSERT OR IGNORE` on that key, so the same firing enqueued
twice collapses to one pending row.
- Code: `monitor.alert_key`, `storage.enqueue_alert`.
- Tests: `test_pending.py::test_enqueue_is_idempotent`,
  `test_alert_key_is_stable_and_distinct`.

### 9. Edge-triggered rules (a steady state does not re-alert)
- Code: `monitor.evaluate`.
- Tests: `test_evaluate.py` (below / back-in-stock / contains all fire once
  on the crossing, then go quiet).

---

## n8n Lead-Gen pipeline

### 10. Validation, suppression, idempotency, concurrency
- **Validation:** malformed emails are dropped before send (regex check).
- **Suppression / opt-outs:** log rows with status `opted_out`,
  `unsubscribed`, `bounced`, `do_not_contact`, or `complained` go to a hard
  suppression set and are never re-contacted, separate from plain
  already-contacted.
- **Idempotency:** every lead carries a stable `idempotency_key` (hash of the
  normalized email), written to the log so a retried append is detectable and
  a real CRM (HubSpot) can upsert on it instead of creating a duplicate.
- **Concurrency:** Sheets has no atomic upsert, so concurrent-run safety comes
  from running the workflow single-flight (n8n per-workflow concurrency = 1)
  plus the idempotency key; against a CRM you let the CRM dedupe on the key.
- Code: the `Normalize & Dedupe` and `Assemble Draft` Code nodes in
  `n8n-leadgen-outbound/workflow.json`.

---

## Honest scope of this portfolio
These are reference builds with synthetic/sample data, not a specific
client's production deployment. What is real: the code runs, the reliability
controls above are implemented and tested, and the same patterns are how I
build the production versions. Where something is a demo limitation rather
than a shipped guarantee, it is called out as such here and in the per-build
READMEs rather than glossed over.
