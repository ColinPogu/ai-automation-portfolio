"""Per-run spending and delegation limits.

Delegation here means a hard ceiling on how much the agent is trusted to do
on its own before it stops and hands back control, no matter how full the
inbox is. Two ceilings are enforced:

* a message ceiling (process at most N messages per run), and
* a money ceiling (stop once estimated model spend for the run reaches a
  dollar cap).

Cost is estimated from token usage the model API reports, priced per million
tokens from config. The estimate is deliberately conservative: it is checked
before each new message, so the run stops at or just under the cap rather
than blowing through it. This is an operational guardrail, not an accounting
system of record.
"""

from __future__ import annotations


class BudgetExceeded(Exception):
    """Raised when a run has hit its message or dollar ceiling."""


class RunBudget:
    def __init__(self, max_messages, max_usd, price_in_per_mtok, price_out_per_mtok):
        self.max_messages = int(max_messages)
        self.max_usd = float(max_usd)
        self.price_in = float(price_in_per_mtok)
        self.price_out = float(price_out_per_mtok)
        self.messages_done = 0
        self.input_tokens = 0
        self.output_tokens = 0

    @property
    def usd_spent(self):
        return (
            self.input_tokens / 1_000_000 * self.price_in
            + self.output_tokens / 1_000_000 * self.price_out
        )

    def check(self):
        """Raise BudgetExceeded if no further message may be started."""
        if self.messages_done >= self.max_messages:
            raise BudgetExceeded(
                "message ceiling reached (%d)" % self.max_messages
            )
        if self.usd_spent >= self.max_usd:
            raise BudgetExceeded(
                "spend ceiling reached ($%.4f of $%.4f)"
                % (self.usd_spent, self.max_usd)
            )

    def record_message(self):
        self.messages_done += 1

    def record_usage(self, usage):
        """Add one API call's token usage. usage is the API usage object."""
        if usage is None:
            return
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)

    def summary(self):
        return (
            "messages=%d/%d spend=$%.4f/$%.2f (in=%d out=%d tokens)"
            % (
                self.messages_done,
                self.max_messages,
                self.usd_spent,
                self.max_usd,
                self.input_tokens,
                self.output_tokens,
            )
        )
