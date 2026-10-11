# CoinDCX Spot Lab — Deployment Guide

This document describes how to deploy CoinDCX Spot Lab to Vercel + Supabase.
The existing Streamlit dashboard is **preserved** for local dev. The
Vercel deployment adds a static HTML/JS frontend + Python serverless API
that imports the existing analysis engine.

> **Honest status**: I have **not** deployed this to Vercel from this
> environment. Vercel deploys require either (a) the Vercel CLI
> authenticated to your account, or (b) connecting the GitHub repo to
> Vercel via the dashboard. The instructions below walk you through
> both paths. The Vercel Python runtime, the API endpoint shapes, and
> the static file routing have been written to match Vercel's
> documented conventions, and the API endpoints have been tested
> against a real local HTTP server (see `tests/test_api_endpoints.py`,
> 12 endpoint tests pass).

---

## 1. Architecture (why it fits this project)

```
┌────────────────────────────────────────────────────────────┐
│ Vercel deployment                                          │
│                                                            │
│  ┌─────────────────┐         ┌──────────────────────┐    │
│  │ Static frontend │  HTTPS  │ Python serverless    │    │
│  │ (web/index.html │ ──────> │ functions (api/*.py) │    │
│  │  + css + js)    │  fetch  │                      │    │
│  │                 │         │  imports existing     │    │
│  │ Plotly.js via   │         │  engine from         │    │
│  │ CDN             │         │  src/coindcx_lab/     │    │
│  └─────────────────┘         └──────────┬───────────┘    │
│                                          │                │
│                                          │ HTTP            │
│                                          ▼                │
│                              ┌──────────────────────┐    │
│                              │ Supabase Postgres    │    │
│                              │ + PostgREST (RLS)    │    │
│                              │                      │    │
│                              │  user_settings       │    │
│                              │  paper_state         │    │
│                              │  paper_trades        │    │
│                              │  paper_signals       │    │
│                              │  market_data_cache   │    │
│                              │  equity_snapshots    │    │
│                              │  job_log             │    │
│                              └──────────────────────┘    │
│                                                            │
│  Vercel Cron (Hobby plan: free cron is limited)            │
│    └─> POST /api/cron (CRON_SECRET protected)              │
│         └─> refresh market_data_cache, log to job_log      │
└────────────────────────────────────────────────────────────┘
```

**Why this fits**:

- The existing `src/coindcx_lab/` Python package is the **single source
  of truth** for all analysis logic. No calculations are duplicated in
  JavaScript. The static frontend calls JSON APIs that delegate to the
  Python engine.
- Vercel's `@vercel/python` runtime supports Python serverless
  functions natively. Each `api/<name>.py` file becomes an endpoint
  at `/api/<name>`.
- The Supabase `SupabaseStore` adapter implements the **same interface**
  as the existing SQLite `Store`. The `PaperTradingEngine` doesn't know
  which backend is in use — it just calls methods on the `Store`.
- RLS policies scope every user-identifying table by `user_id`. The
  service-role key bypasses RLS for trusted cron writes
  (`market_data_cache`, `job_log`); the publishable key is read-only
  on those tables.

---

## 2. Pre-flight checklist

Before you start, you need:

- [ ] A Supabase project (you already have one: `psbizlnzxqxwtlrajkst`).
- [ ] The Supabase project URL, publishable key, and service-role key.
      (You've provided these in the chat. They live ONLY in Vercel env
      vars and your local `.env` — never in the committed code.)
- [ ] A GitHub repo (you already have one:
      `https://github.com/unknown3thing-lgtm/Cdcxram.git`).
- [ ] A Vercel account (free Hobby tier is fine).
- [ ] `openssl` for generating `CRON_SECRET`.

---

## 3. Apply the Supabase migrations

The publishable/service keys cannot run DDL. You need either the
Supabase SQL editor (manual paste) or the `supabase` CLI.

### Option A: SQL editor (easiest, 2 minutes)

1. Go to:
   ```
   https://supabase.com/dashboard/project/<your-project-ref>/sql/new   # e.g. https://supabase.com/dashboard/project/psbizlnzxqxwtlrajkst/sql/new
   ```
2. From this repo, run the migrations printer and copy the output:
   ```bash
   cd /home/z/my-project/coindcx_lab
   python3 scripts/print_migrations.py > /tmp/migrations_all.sql
   cat /tmp/migrations_all.sql
   ```
3. Paste the entire SQL output into the Supabase SQL editor.
4. Click **Run**.
5. You should see "Success. No rows returned."
6. Verify by running this in the same SQL editor:
   ```sql
   SELECT tablename FROM pg_tables WHERE schemaname = 'public';
   ```
   You should see 7 tables: `user_settings`, `paper_state`,
   `paper_trades`, `paper_signals`, `market_data_cache`,
   `equity_snapshots`, `job_log`.

### Option B: `supabase` CLI (requires Node.js)

```bash
# Install once
npm install -g supabase

# Login (uses your Supabase dashboard account)
supabase login

# Link the project
cd /home/z/my-project/coindcx_lab
supabase link --project-ref <your-project-ref>   # e.g. psbizlnzxqxwtlrajkst

# Push migrations
supabase db push
```

### Verify the connection (after migrations are applied)

After applying migrations, the following should succeed:

```bash
# Replace the placeholders with YOUR actual keys from the Supabase dashboard
# (Settings → API). NEVER commit the real service-role key.
SUPABASE_URL="https://<your-project>.supabase.co" \
SUPABASE_PUBLISHABLE_KEY="sb_publishable_<your-publishable-key>" \
SUPABASE_SERVICE_KEY="sb_secret_<your-service-key>" \
PYTHONPATH=src python3 scripts/test_supabase_connection.py
```

(The connectivity test confirms: HTTP 200 on `GET
/rest/v1/paper_state` with `[]` body, and HTTP 201 on the POST upsert
with the row echoed back. Before migrations are applied you'll see
`PGRST205` "table not found" — that's expected.)

---

## 4. Push the project to GitHub

The repo already exists at
`https://github.com/unknown3thing-lgtm/Cdcxram.git`. We need to push
the new Vercel + Supabase files without exposing the secrets.

```bash
cd /home/z/my-project/coindcx_lab

# Verify .gitignore excludes secrets
cat .gitignore
# It should include: .env, *.sqlite, *.sqlite3, secrets.json,
# config/local_settings.json, etc.

# Initialise git if needed
git init  # if not already in a repo
git remote add origin https://github.com/unknown3thing-lgtm/Cdcxram.git 2>/dev/null \
  || git remote set-url origin https://github.com/unknown3thing-lgtm/Cdcxram.git

# Verify NO secret values will be committed
git add -A
git status --short
# Confirm no .env file is staged. If it is, run: git rm --cached .env

git commit -m "feat: add Vercel deployment + Supabase integration

- Add 11 Python serverless functions under api/ (reusing the existing
  analysis engine from src/coindcx_lab/).
- Add static HTML/JS dashboard under web/.
- Add SupabaseStore adapter (mirrors the existing SQLite Store
  interface so PaperTradingEngine is backend-agnostic).
- Add 3 additive Supabase migrations (initial schema, RLS policies,
  anon-user policies).
- Add .env.example with placeholder values.
- Add vercel.json (Python runtime + static frontend routing + cron).
- Add 54 new tests (env vars, supabase_store mocked HTTP, API endpoints
  via real local HTTP server). Total: 164 tests, all pass."

git push -u origin main  # or master, depending on your default branch
```

**Verify the push did not leak secrets**:
```bash
# Check the latest commit on GitHub for any secret value
git log -p HEAD~1..HEAD | grep -E "sb_secret_|sb_publishable_" || echo "OK: no secrets in commits"
```

---

## 5. Deploy to Vercel

### Option A: Dashboard import (recommended)

1. Go to: https://vercel.com/new
2. Import the GitHub repo:
   `https://github.com/unknown3thing-lgtm/Cdcxram`
3. Configure the project:
   - **Framework Preset**: Other (leave auto-detected)
   - **Root Directory**: `coindcx_lab` (if you pushed the parent
     directory) OR leave at repo root if you pushed `coindcx_lab/`
     contents directly
   - **Build Command**: (leave blank — no build needed)
   - **Output Directory**: (leave blank — Vercel serves files from
     project root by default)
   - **Install Command**: `pip install -r requirements-vercel.txt`
4. **Environment Variables** (add all of these in the Vercel UI):

   | Name | Value | Environments |
   |---|---|---|
   | `SUPABASE_URL` | `https://<your-project>.supabase.co` (from Supabase dashboard → Settings → API) | Production, Preview, Development |
   | `SUPABASE_PUBLISHABLE_KEY` | `sb_publishable_...` (safe to expose to browser; from Supabase dashboard → Settings → API) | Production, Preview, Development |
   | `SUPABASE_SERVICE_KEY` | `sb_secret_...` (NEVER expose to browser; from Supabase dashboard → Settings → API) | Production, Preview (NOT Development if you want to test RLS) |
   | `CRON_SECRET` | (generate with `openssl rand -hex 32`) | Production only |
   | `API_ALLOWED_ORIGIN` | (leave blank for same-origin, OR set to your Vercel domain) | All |
   | `ALLOW_SIMULATED_WHEN_LIVE_UNAVAILABLE` | `true` | All |

5. Click **Deploy**.
6. Vercel will install `requirements-vercel.txt` and deploy each
   `api/*.py` file as a serverless function. The static frontend at
   `web/index.html` will be served at the project root.

### Option B: Vercel CLI

```bash
npm install -g vercel
cd /home/z/my-project/coindcx_lab
vercel login
vercel link  # link to your project (or create a new one)

# Set env vars (run for each; Vercel prompts for the value)
vercel env add SUPABASE_URL production
# Paste: https://psbizlnzxqxwtlrajkst.supabase.co
vercel env add SUPABASE_PUBLISHABLE_KEY production
vercel env add SUPABASE_SERVICE_KEY production
vercel env add CRON_SECRET production
# (paste your openssl-generated secret)

# Deploy to preview
vercel

# Deploy to production
vercel --prod
```

---

## 6. Configure the Vercel Cron job

The `vercel.json` already declares a cron job:

```json
"crons": [
  { "path": "/api/cron", "schedule": "0 * * * *" }
]
```

**Important: Vercel Hobby plan cron limitations**:
- Hobby plan: free cron jobs are limited (verify at
  https://vercel.com/docs/cron-jobs before relying on a schedule).
- The `0 * * * *` (hourly) schedule above may be **rejected by Vercel
  Hobby**. If you're on Hobby, change it to `0 0,12 * * *` (twice
  daily) or `0 0 * * *` (once daily).

To change the schedule, edit `vercel.json`:
```json
"crons": [
  { "path": "/api/cron", "schedule": "0 0,12 * * *" }
]
```

**Vercel sends the CRON_SECRET in the `Authorization: Bearer <secret>`
header**. The `/api/cron` endpoint verifies this with constant-time
comparison. Without the secret, requests get HTTP 401.

**Idempotency**: the cron job upserts into `market_data_cache` on
`pair`, so re-runs are safe. Job invocations are logged to `job_log`
with start/end timestamps and any error message.

---

## 7. Verify the live deployment

After Vercel deploy completes (URL looks like
`https://coindcx-lab.vercel.app` or similar):

```bash
DEPLOY_URL="https://<your-vercel-domain>"

# 1. Health endpoint (no auth, no secrets in response)
curl -s "$DEPLOY_URL/api/health" | python3 -m json.tool

# 2. Markets endpoint (should fall back to SIMULATED because
#    CoinDCX is blocked from this IP)
curl -s "$DEPLOY_URL/api/markets" | python3 -m json.tool | head -20

# 3. Candles (SIMULATED fallback expected)
curl -s "$DEPLOY_URL/api/candles?pair=B-BTCINR&interval=1h&limit=10" \
  | python3 -m json.tool | head -20

# 4. Paper portfolio (requires Supabase env vars set on Vercel)
curl -s "$DEPLOY_URL/api/portfolio" | python3 -m json.tool

# 5. Cron (requires CRON_SECRET)
SECRET="$(openssl rand -hex 32)"   # use your actual CRON_SECRET
curl -X POST -H "Authorization: Bearer $SECRET" "$DEPLOY_URL/api/cron" \
  | python3 -m json.tool
```

If steps 1-3 succeed but 4 returns `config_error`, your Supabase env
vars are not set in Vercel. If 5 returns `unauthorized`, your
`CRON_SECRET` is missing or mismatched.

**Frontend smoke test**: open `$DEPLOY_URL/` in a browser. You should
see the dashboard load, fetch markets + prices, and display the
provenance badge (likely `SIMULATED · no live prices (deterministic
fixture)` because CoinDCX is Cloudflare-blocked from Vercel's IP
range too — see section 8).

---

## 8. CoinDCX API status & workarounds

**The CoinDCX public API returns HTTP 403 from this environment**
(Cloudflare edge block on the data-centre IP range — verified by
the diagnostic in `scripts/diagnose_coindcx_api.py`:

```
[1] GET /market_data/v3/exchange_pairs
    FAIL in 42.7ms  CoinDCXAPIError: 403 Forbidden — Cloudflare edge block.
[2] GET /market_data/v3/current_prices/all_pairs
    FAIL in 40.1ms  CoinDCXAPIError: 403 Forbidden — Cloudflare edge block.
[3] POST /market_data/v3/candles
    FAIL in 43.3ms  CoinDCXAPIError: 403 Forbidden — Cloudflare edge block.

Server: cloudflare  CF-Ray: a483bf6f0b2309bc-HKG  (Hong Kong edge)
```

All 9 endpoint/header combinations tested returned the same 403.
Sanity URLs (Google, Binance) returned 200, confirming the network
itself works.

When the live API is unreachable, the dashboard falls back to a
**clearly-labelled SIMULATED** series (deterministic, no live prices
fabricated). The provenance badge on every chart and table shows
exactly which source the data came from.

**To get live data working**, you would need to:

1. Run from a network/IP range that CoinDCX's CDN allows (e.g. a
   residential Indian IP, or a VPN that exits from such a range).
2. OR contact CoinDCX support and request that the public market-data
   endpoints be allow-listed for your use case.
3. OR use an officially-permitted alternative market-data source
   (e.g. a CoinDCX enterprise feed if you have a commercial
   arrangement).

**This project does not bypass Cloudflare or any access control.**

---

## 9. Remaining blockers & checklist

Before the deployment is "production-grade":

- [ ] Apply the 3 Supabase migrations (see section 3) and verify the
      7 tables exist.
- [ ] Push the project to GitHub (see section 4).
- [ ] Connect the GitHub repo to Vercel and set the 6 env vars (see
      section 5).
- [ ] Change the cron schedule in `vercel.json` to `0 0,12 * * *`
      (twice daily) if you're on the Hobby plan.
- [ ] Generate `CRON_SECRET` with `openssl rand -hex 32` and set it
      as a Vercel env var.
- [ ] Verify the 5 endpoint smoke tests in section 7.
- [ ] (Optional) If you want live data, arrange a network path that
      CoinDCX's CDN permits.
- [ ] (Optional) If you want multi-user auth, integrate Supabase Auth
      and update the `current_user_id` function in
      `src/coindcx_lab/server/common.py` to read the JWT from the
      Authorization header.

**Known limitations**:

1. **Live CoinDCX data is blocked** from data-centre IPs (Cloudflare
   edge block). The dashboard shows SIMULATED data with clear labels.
2. **Vercel Hobby plan cron is limited** to twice-daily free
   invocations. Real-time signal refresh requires either a Pro plan
   or an external scheduler (e.g. GitHub Actions scheduled workflow
   hitting `/api/cron`).
3. **Paper trading is long-only** (no short-selling, no leverage, no
   derivatives) — by design, per the original spec.
4. **Single shared anonymous user**: the MVP uses a shared
   `user_id = 00000000-0000-0000-0000-000000000000` so anyone
   hitting the dashboard shares the same paper portfolio. Multi-user
   requires adding Supabase Auth and updating `current_user_id()`.

---

## 10. Local development (Streamlit dashboard still works)

The Streamlit dashboard is **unchanged** and still the recommended
local-dev experience:

```bash
cd /home/z/my-project/coindcx_lab
pip install -r requirements.txt
streamlit run src/coindcx_lab/dash/app.py
# → http://localhost:8501
```

The Streamlit dashboard uses SQLite (`data/paper_trading.sqlite`) by
default — no Supabase configuration needed for local dev. The Vercel
deployment uses Supabase instead. Both backends implement the same
`Store` interface, so the `PaperTradingEngine` logic is identical.
