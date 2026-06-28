# WhatsApp Business AI Sales Agent

A production-shaped FastAPI + LangGraph backend for a single-business WhatsApp AI Sales Agent using the official WhatsApp Cloud API.

## Stack

- **Python 3.11+** · FastAPI + Uvicorn · LangGraph (StateGraph + Postgres checkpointer)
- **PostgreSQL** via SQLAlchemy 2.0 (async/asyncpg) · Alembic migrations
- **LLM:** Anthropic (`claude-*`) or OpenAI (`gpt-4o`) — switched by env var
- **Structured logging:** structlog JSON · Metrics: prometheus-client

## Quick start

```bash
# 1. Copy and fill in your secrets
cp .env.example .env

# 2. Install dependencies
make install

# 3. Start Postgres (Docker)
docker-compose up -d postgres

# 4. Run migrations
make migrate

# 5. Seed demo data
make seed

# 6. Start the dev server
make dev
```

Point Meta's webhook at `https://<your-ngrok-url>/webhook`.

## Commerce modes

| Mode | Catalog source | Order creation |
|---|---|---|
| `whatsapp_only` (default) | Local `products` table | `orders` row, returns `order_ref` |
| `website` | Shopify Admin API | Shopify draft order, returns checkout URL |

Switch by setting `DEFAULT_COMMERCE_MODE=website` and supplying Shopify credentials.  
No other code changes needed.

## Environment variables

See [`.env.example`](.env.example) for the full list.

## Docker

```bash
docker-compose up          # app + postgres + pgadmin (localhost:5050)
docker-compose exec app alembic upgrade head
docker-compose exec app python -m scripts.seed
```

## Tests

The test suite (117 tests) runs entirely against an in-memory SQLite DB — no external services required.

```bash
make test          # run full suite
make test-cov      # with coverage report

# Against a real Postgres DB:
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/wa_agent_test make test
```

## Analytics endpoints

All endpoints are read-only; no authentication is enforced at the application layer (add an API gateway or IP-allow-list in production).

```
GET /analytics/funnel       Customer counts per CRM stage + per-stage conversion rates
GET /analytics/kpis         Revenue, avg order value, opt-out count, overall conversion rate
GET /analytics/customers    Paginated customer list (optional ?stage= filter)
```

## File tree

```
app/
  main.py              FastAPI app + lifespan
  config.py            pydantic-settings (validated on startup)
  logging_config.py    structlog JSON + correlation IDs
  db/
    base.py            Async engine / session / get_db dependency
    models.py          SQLAlchemy 2.0 models (all tables)
    crud.py            Data-access helpers
  events/
    recorder.py        Append-only event + message_log + stage_history writes
  webhook/             Meta webhook validation + ingest (Phase 2)
  whatsapp/            Graph API client — send text, download media (Phase 3)
  messaging/           Outbound choke-point: 24h window guard, opt-out (Phase 4)
  llm/                 Provider-agnostic LLM + Vision wrapper (Phase 5-7)
  agents/              LangGraph graph, supervisor, sales agent, tools (Phase 5-8)
  analytics/           Funnel KPI computation + read-only endpoints (Phase 9)
  schemas/             Shared Pydantic schemas (Phase 6+)
alembic/               Alembic migrations
scripts/seed.py        Demo data seeder
```
