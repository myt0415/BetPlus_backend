# BetPlus Backend (FastAPI)

## Quick start

```bash
cd backend
cp .env.example .env
docker compose up --build
```

API: `http://localhost:8000`  
Health: `GET /health/`  
Readiness: `GET /health/ready`  
OpenAPI docs: `http://localhost:8000/docs`

Production deploy: see [`DEPLOY.md`](../DEPLOY.md).

## Environment

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL (required in production) or SQLite for local/dev |
| `SECRET_KEY` | JWT signing secret (required in production) |
| `CORS_ORIGINS` | Comma-separated frontend origins; `*` is rejected in production |
| `ENVIRONMENT` | `development` / `staging` / `production` / `test` |
| `SEED_DEMO_DATA` | Seed catalog sports/matches (disabled in production unless `ALLOW_DEMO_SEED`) |
| `SEED_DEMO_USERS` | Seed `admin@betplus.com` / `admin123` — **never in production** |
| `PAYMENTS_MODE` | `simulated` / `moolre` / `disabled` |
| `NOWPAYMENTS_*` | Optional BTC/USDT deposits. Secrets stay on this API, never on the frontend |
| `RATE_LIMIT_ENABLED` | DB-backed limits for login, register, bets, webhooks |
| `SPORTYBET_FACTS_URL` | Optional catalog sync URL (defaults to SportyBet Ghana facts-center) |
| `SPORTYBET_SPORT_ID` | Upstream sport id (default `sr:sport:1` / football) |
| `SPORTYBET_TIMEOUT_SECONDS` | Upstream HTTP timeout (1–60, default 15) |

## Migrations

```bash
cd backend
alembic upgrade head
```

Production must use Alembic. `Base.metadata.create_all()` is skipped when `ENVIRONMENT=production`.

## Tests

```bash
cd backend
py -m pytest tests/ -v
```

PostgreSQL concurrency tests run only when `POSTGRES_TEST_URL` is set.

## Demo users (SEED_DEMO_USERS=true, never production)

| Email | Password | Role |
|-------|----------|------|
| admin@betplus.com | admin123 | admin |
| demo@betplus.local | demo123 | admin |
| manager@betplus.local | manager123 | manager |

## API versioning

- Primary: `/api/v1/...`
- Legacy aliases: `/api/auth`, `/api/wallet`, `/api/bets`, `/api/catalog`

## Frontend integration

```env
NEXT_PUBLIC_USE_BACKEND=true
BACKEND_URL=http://localhost:8000
```
