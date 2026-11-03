# Tillbird — AI customer support agent for online shops

Tillbird is an AI support assistant that sits on a shop's website as a chat
widget and answers customer questions — order status, returns, shipping,
sizing, product details — grounded in that shop's own policies and live
product catalogue. When it can't help, it hands the conversation off to a
human with a full transcript.

> **Showcase repository.** This is a trimmed, public copy of a private
> production codebase. Sales, lead-generation and audit tooling, deployment
> configs and all client data have been removed. The core agent, Shopify
> integration, onboarding pipeline and their test suites are included.

## What it does

- **Agent loop** (`app/agent/`) — Claude-powered tool-use loop with tools for
  catalogue search, order lookup and human handoff, a per-store system prompt,
  and guards against prompt injection, invented discounts and leaking other
  customers' data.
- **Shopify integration** (`app/shopify_client.py`, `app/shopify_oauth.py`,
  `app/routes/oauth.py`) — OAuth install flow, encrypted credential storage,
  live product and order lookups via the Admin API.
- **Knowledge-base onboarding** (`app/scraping.py`, `app/demo_kb.py`,
  `app/kb_grounding.py`, `app/live_onboarding.py`) — scrapes a store's policy,
  FAQ, care and size-guide pages, drafts a knowledge base with Claude, and
  checks every drafted fact against its source text before it is used.
- **Embeddable widget** (`app/static/widget.js`) — a dependency-free chat
  widget, themed per store, with CORS and per-origin allow-lists.
- **Cost and abuse controls** (`app/usage_limits.py`, `app/anthropic_usage.py`)
  — per-store and per-session caps, token accounting, rate limiting.
- **Reporting and eval** (`app/reporting.py`, `app/evaluation.py`) — monthly
  per-store reports and a scripted eval run of ~40 adversarial and routine
  customer messages.

Each client store is a config file in `stores/<store_id>.yaml`
(see `stores/demo-example.yaml`).

## Stack

Python 3.12 · Flask · SQLAlchemy (SQLite, WAL mode) · Anthropic API ·
Shopify Admin API · BeautifulSoup · vanilla JS widget · Docker · gunicorn ·
pytest (600+ tests)

## Running locally

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY at minimum
pip install -r requirements-dev.txt
python scripts/init_db.py
flask --app wsgi run --port 8000
```

Or with Docker:

```bash
docker compose up --build
```

Try the agent from the terminal against the example store:

```bash
python scripts/ask_agent.py demo-example "Do you ship to Ireland?"
```

## Tests

```bash
pytest -q
```

The suite runs fully offline — Anthropic calls go through a fake client
(`tests/fake_anthropic.py`) and Shopify/HTTP calls are stubbed.

## License

All rights reserved. Shared for portfolio and review purposes only.
