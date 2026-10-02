# BetPlus production deployment

Target architecture:

```text
Internet
   │
   ▼
Vercel (Next.js)          https://yourdomain.com
   │  HTTPS  /api/v1/*
   ▼
Heroku (FastAPI/Uvicorn)  https://api.yourdomain.com
   │  PostgreSQL
   ▼
Supabase PostgreSQL
```

The browser talks only to Vercel (and Vercel rewrites `/api/v1/*` to FastAPI).
The browser never receives `DATABASE_URL`, payment secrets, or JWT secrets.

This application is **not real-money ready** until `PAYMENTS_MODE=paystack`
(or another verified provider) is configured, webhook signatures are verified
in staging, and a restore test has been run. Simulated deposits credit the
wallet on the server after `/api/v1/payments/deposits`; they are ledger
simulations, not processor funds.

---

## 1. Supabase PostgreSQL

1. Create a project.
2. Copy the **URI** (direct port 5432 or pooler port 6543).
3. Enable backups on the chosen plan (Point-in-Time Recovery on paid plans).
4. Restore procedure: Supabase Dashboard → Database → Backups → Restore to a
   **staging** project. Confirm `alembic current` and a login + wallet read.
5. Recommended backup settings to document for ops:
   - Daily backups (plan default)
   - PITR retention per plan
   - Restore test at least once before production traffic

Set on Heroku (never on Vercel):

```text
DATABASE_URL=postgresql://postgres.[ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres
```

`postgres://` URLs are accepted and normalized. SSL is added in production.

---

## 2. Heroku (FastAPI)

Preferred: set the app **root directory** to `backend/` so Heroku uses
`backend/Procfile`, `backend/requirements.txt`, and `backend/runtime.txt`.

```bash
cd backend
heroku create betplus-api
heroku config:set ENVIRONMENT=production
heroku config:set SECRET_KEY="$(python -c "import secrets; print(secrets.token_urlsafe(48))")"
heroku config:set DATABASE_URL="postgresql://..."
heroku config:set CORS_ORIGINS="https://yourdomain.com,https://www.yourdomain.com"
heroku config:set SEED_DEMO_DATA=false
heroku config:set SEED_DEMO_USERS=false
heroku config:set ALLOW_DEMO_SEED=false
heroku config:set RATE_LIMIT_ENABLED=true
heroku config:set PAYMENTS_MODE=paystack
heroku config:set PAYMENT_SECRET_KEY=sk_...
heroku config:set PAYMENT_PUBLIC_KEY=pk_...
heroku config:set PAYMENT_WEBHOOK_SECRET=...
heroku config:set PAYMENT_CURRENCY=GHS
heroku config:set NOWPAYMENTS_ENABLED=false
heroku config:set NOWPAYMENTS_API_KEY=
heroku config:set NOWPAYMENTS_BASE_URL=https://api.sandbox.nowpayments.io
heroku config:set NOWPAYMENTS_IPN_SECRET=
heroku config:set NOWPAYMENTS_IPN_CALLBACK_URL=https://api.yourdomain.com/api/v1/payments/webhooks/nowpayments
heroku config:set NOWPAYMENTS_PRICE_CURRENCY=GHS
```

Staging (simulated money only):

```bash
heroku config:set ENVIRONMENT=staging
heroku config:set PAYMENTS_MODE=simulated
heroku config:set ALLOW_SIMULATED_PAYMENTS=true
heroku config:set SEED_DEMO_DATA=true
heroku config:set SEED_DEMO_USERS=false
heroku config:set CORS_ORIGINS="https://staging.yourdomain.com,http://localhost:3000"
```

Deploy from repo root with the backend as the app root, or:

```bash
git subtree push --prefix backend heroku main
```

Release phase runs `alembic upgrade head`. Do **not** use `create_all()` in
production.

Verify:

```text
GET https://api.yourdomain.com/health/        → {"status":"ok"}
GET https://api.yourdomain.com/health/ready   → database reachable
```

The process binds `0.0.0.0:$PORT`.

---

## 3. Vercel (Next.js)

Environment variables (Production and Preview):

```text
NEXT_PUBLIC_USE_BACKEND=true
BACKEND_URL=https://api.yourdomain.com
NEXT_PUBLIC_ENVIRONMENT=production
```

Do not set `DATABASE_URL`, `SECRET_KEY`, or payment secrets on Vercel.

`BACKEND_URL` is read when Next.js **builds** rewrites. Change it → redeploy.

```bash
npx vercel --prod
```

Verify:

- Homepage loads
- Login hits `/api/v1/auth/login` through the rewrite
- Catalog loads `/api/v1/catalog/games`
- Wallet operations go to FastAPI, not localStorage

---

## Required backend environment

| Variable | Production |
|----------|------------|
| `ENVIRONMENT` | `production` |
| `DATABASE_URL` | Supabase PostgreSQL |
| `SECRET_KEY` | strong random, unique |
| `CORS_ORIGINS` | exact Vercel/custom origins, no `*` |
| `SEED_DEMO_DATA` | `false` |
| `SEED_DEMO_USERS` | `false` |
| `PAYMENTS_MODE` | `paystack` for live money; `simulated` only with `ALLOW_SIMULATED_PAYMENTS=true` on staging |
| `PAYMENT_*` | provider keys; webhook URL `https://api.yourdomain.com/api/v1/payments/webhook` |
| `RATE_LIMIT_ENABLED` | `true` |
| `SPORTYBET_*` | optional; public facts-center sync headers/timeout only. Do not set SportyBet user cookies or session tokens |

---

## Staging checklist

- [ ] Alembic head applied
- [ ] Health and ready endpoints 200
- [ ] Register / login / `/me`
- [ ] Catalog games from PostgreSQL
- [ ] Bet placement uses server odds (tampered client odds ignored)
- [ ] Concurrent withdrawals/bets against Postgres (`POSTGRES_TEST_URL`)
- [ ] Double settlement does not double-pay
- [ ] Admin/manager 403 for normal users
- [ ] Demo passwords not seeded
- [ ] CORS rejects unknown origins
- [ ] Backup restore tested on a staging database
- [ ] Paystack webhook signature verified (if using live/test keys)
