# AmazonScraper License Server

Tiny Flask + PostgreSQL service that issues, activates, and validates license keys
for the AmazonScraper desktop app. Web service runs on Render's free tier;
the database is Supabase Postgres (external, not Render-managed).

## Local dev

```bash
cd license_server
pip install -r requirements.txt
LICENSE_SIGNING_SECRET=dev LICENSE_ADMIN_TOKEN=dev \
  DATABASE_URL=postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres \
  python app.py
```

**Use the pooler connection string, not the direct one.** Supabase's direct
host (`db.<project-ref>.supabase.co`) is IPv6-only unless you pay for the
IPv4 add-on, and Render's free web services don't reliably support outbound
IPv6 — a direct-host `DATABASE_URL` will silently fail to connect once
deployed. Get the pooler string from Supabase → **Project Settings →
Database → Connection string → "Session pooler"** tab (dual-stack, works
from Render).

Health check:

```bash
curl http://localhost:8000/healthz
# → {"ok": true, "time": "..."}
```

Tables (`keys`, `activations`, `runs`) are created automatically on first
connect via `CREATE TABLE IF NOT EXISTS` in `app.py` — no separate migration
step needed against a fresh Supabase database.

## Deploy to Render (web service) + Supabase (database)

1. Create a Supabase project → **Project Settings → Database → Connection
   string → "Session pooler"** tab (not "URI"/direct — that host is
   IPv6-only and Render's free web services can't reliably reach it) →
   copy it and substitute the real database password for `[YOUR-PASSWORD]`.
2. Push this repo to GitHub.
3. In Render: **New +** → **Blueprint** → connect the repo → pick
   `license_server/render.yaml`.
4. Render will:
   - provision a free web service
   - auto-generate `LICENSE_SIGNING_SECRET` and `LICENSE_ADMIN_TOKEN`
   - run `gunicorn app:app` and start hitting `/healthz`
5. `DATABASE_URL` is **not** auto-populated (Blueprint has no managed
   database anymore). In the Render dashboard: Service → Environment →
   add `DATABASE_URL` = the Supabase connection string from step 1. Never
   commit this value to the repo.
6. Note the deployed URL (e.g. `https://amazon-scraper-license.onrender.com`).
7. **Important:** edit `license.py` in the desktop project and set
   `LICENSE_SERVER_URL` to that URL before building the next release.
8. Set the **same** `LICENSE_SIGNING_SECRET` as `AMZ_LICENSE_SECRET` in the
   build environment before running PyInstaller — the client needs it to
   verify signed tokens offline.

## Admin CLI setup

After deploy, copy your Render-generated `LICENSE_ADMIN_TOKEN` and create:

```bash
cat > ~/.amazon_scraper_admin <<'EOF'
URL=https://amazon-scraper-license.onrender.com
TOKEN=paste-the-render-generated-token-here
EOF
chmod 600 ~/.amazon_scraper_admin
```

Now you can use the CLI:

```bash
# Issue a 1-year, single-machine key
python issue_key.py issue --customer "Lapcare" --days 365 --machines 1 \
    --notes "PO-2026-001"

# Show all keys
python issue_key.py list

# Extend a key by 30 days
python issue_key.py extend --key AMZ-XXXX-XXXX-XXXX-XXXX --days 30

# Revoke immediately
python issue_key.py revoke --key AMZ-XXXX-XXXX-XXXX-XXXX

# Free up an activation slot (so the customer can move to a new PC)
python issue_key.py release-machine \
    --key AMZ-XXXX-XXXX-XXXX-XXXX --machine-id 1f3...

# Detailed view of a key + every machine it's on
python issue_key.py info --key AMZ-XXXX-XXXX-XXXX-XXXX

# Price Verification Tool: a key for it only, or add it to an existing key
python issue_key.py issue --customer "Lapcare" --days 365 --products price_verifier
python issue_key.py set-products --key AMZ-XXXX-XXXX-XXXX-XXXX --products amazon_scraper,price_verifier
```

## Products (one server, several apps)

The same server and database license both desktop apps. Each key has a
product list (`keys.products`):

| Product | App | Sends `product` |
|---|---|---|
| `amazon_scraper` | the delivery scraper (`gui.py` / `license.py`) | no — requests without it are treated as `amazon_scraper` |
| `price_verifier` | the Price Verification Tool (`price_verifier/licensing.py`) | yes |

- A key only unlocks the products on its list; anything else gets
  `product_not_licensed`.
- `issue` defaults to `amazon_scraper`, so existing keys and existing scraper
  builds behave exactly as before.
- One key covering both products on one PC uses **one** machine slot
  (activations are per key + machine).
- `revoke` still blocks every product on the key.
- `/admin/runs` records which product each run was for.

**Upgrading a live server:** just redeploy. On start-up, `init_db()` adds
`keys.products` and `runs.product` if they're missing; both default to
`amazon_scraper`. It only adds columns, never touches existing data, and is
safe to run on every start. Tests: `python -m unittest tests.test_server`
(SQLite stand-in, runs anywhere) and `license_server/test_app.py` (real
Postgres, including migrating an old-schema database; set
`LICENSE_TEST_DATABASE_URL` to a throwaway database).

## How the client behaves

- On launch, the client loads `license.json` (in the user's app-data dir) to
  decide whether to show the activation page.
- **Every run is authorized by the server** (`POST /authorize-run`) before
  it starts. The server checks the key, expiry, product and machine, and
  logs the run.
- If the server can't be reached, a run is still allowed within **24 hours**
  of the last successful authorization. After that the user must reconnect.
- Every 7 days the client also sends `POST /heartbeat`. If the server says
  `revoked`, `expired` or `product_not_licensed`, the client shows the
  activation page.
- The 35-second request timeout covers the free Render service waking up
  from sleep.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | — | Render health check |
| POST | `/activate` | — | First-time key activation, binds to machine_id |
| POST | `/heartbeat` | — | Periodic re-validation |
| POST | `/authorize-run` | — | Per-run gate (key, expiry, product, machine); logs the run |
| POST | `/admin/set-products` | Bearer | Set the products a key unlocks |
| POST | `/admin/unrevoke` | Bearer | Re-enable a revoked key |
| GET | `/admin/runs?key=K` | Bearer | Recent run authorizations (with product) |
| POST | `/admin/issue` | Bearer | Issue a new key |
| GET | `/admin/list` | Bearer | List all keys |
| POST | `/admin/extend` | Bearer | Bump expiry |
| POST | `/admin/revoke` | Bearer | Revoke a key |
| POST | `/admin/release-machine` | Bearer | Free a slot |
| GET | `/admin/info?key=K` | Bearer | Detail view |
