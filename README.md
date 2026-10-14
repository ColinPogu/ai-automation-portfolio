# AI Automation Portfolio

Three real, runnable automation builds. Each one is a working tool, not a demo: clone it, add your keys, and it runs. These are the kinds of systems I build and hand to clients.

## The builds

### 1. [Inbox Triage Agent](./inbox-triage-agent)
Python + Claude. Connects to a mailbox, pulls recent unread messages, and for each one classifies it as routine or sensitive. Routine mail gets a brand-voice reply drafted and saved as a Gmail draft. Sensitive mail (refunds, complaints, legal, billing) is never drafted, it goes to a human-review queue instead. Draft-first and escalation-gated by design: it requests no send permission at all, so nothing can ever auto-send. Brand voice and rules are config-driven, so the same tool re-skins for any client.

### 2. [n8n Lead-Gen and Outbound Pipeline](./n8n-leadgen-outbound)
An importable n8n workflow. Ingests a lead list, dedupes against who has already been contacted, calls Claude to write a personalized first line per lead, assembles a draft, logs it, and rate-limits so it never blasts. Import the JSON, point it at your sheet and keys, and it runs.

### 3. [Scheduled Monitor](./scheduled-monitor)
Python. Watches a configurable list of pages or endpoints on a schedule, extracts a value (price, stock status, a count, a keyword), stores every reading in SQLite, and fires a Slack alert when a threshold is crossed. Retries and structured logging included.

## How I build
Every one of these follows the same principles: config over hardcoding, secrets in environment variables, a human in the loop wherever a mistake would be costly, and logs so you can see exactly what ran. The stack is the Claude Agent SDK and the Anthropic API, n8n and Make for orchestration, and Python and JavaScript for the custom pieces.

## See also
- Growth and attribution case study: https://colinpogu.github.io/affiliate-case-study/
- Growth engineering portfolio (edge attribution Worker + ops pipeline): https://github.com/ColinPogu/growth-engineering-portfolio

---
Built by Colin Bustos . Manila, UTC+8 . colinbustos276@gmail.com
