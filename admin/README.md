# License Admin Dashboard

A simple web dashboard to manage AmazonScraper license keys: view all keys with
status / activation dates / machines used, generate new keys, revoke / un-revoke,
extend expiry, and release machine slots.

Deploys to **Vercel** as a static page + one serverless proxy function.

## How it works (security)

- The page (`index.html`) never holds the license server's admin token.
- All calls go to `/api/proxy` (same origin → no CORS), which runs on Vercel and
  injects the admin token from an environment variable, then forwards to the
  Render license server.
- The dashboard is gated by a separate `DASHBOARD_PASSWORD` you choose. Only the
  password (not the admin token) is ever entered in the browser.

```
Browser (you) ──password──▶ /api/proxy (Vercel) ──Bearer admin token──▶ Render license server
```

## Deploy

1. Push this repo to GitHub (the `admin/` folder).
2. On vercel.com → **New Project** → import the repo.
3. Set **Root Directory** to `admin`.
4. Framework preset: **Other**. Build command: empty. Output dir: leave default.
5. Add these **Environment Variables** (Settings → Environment Variables):

   | Name | Value |
   |---|---|
   | `LICENSE_SERVER_URL` | `https://amazon-scraper-license.onrender.com` |
   | `LICENSE_ADMIN_TOKEN` | your server's `LICENSE_ADMIN_TOKEN` (the Bearer token) |
   | `DASHBOARD_PASSWORD` | a password you pick to open this dashboard |

6. **Deploy.** Open the `.vercel.app` URL, enter your `DASHBOARD_PASSWORD`, done.

> After changing any env var, redeploy (or use "Redeploy") so the function picks it up.

## Local test

```bash
cd admin
npm i -g vercel    # if not installed
vercel dev         # serves index.html + /api/proxy locally
```
Set the three env vars in a `.env` for `vercel dev`, or via `vercel env`.

## Features

- **Dashboard:** searchable table of all keys — customer, key, status
  (Active / Revoked / Expired), machines used / max, issued + expiry dates, notes.
- **Stats:** totals for keys, active, revoked, expired, machines in use.
- **Generate key:** customer, validity (days), max machines, notes → returns the
  new key to copy.
- **Revoke / Un-revoke:** hard kill switch (blocks all machines on the key) and
  restore.
- **Extend:** add N days to a key's expiry.
- **Details:** list a key's activated machines (activation date, last seen,
  app version) and **release** any machine slot.

> Note: `Un-revoke` and machine self-heal require the updated `license_server`
> to be deployed (it adds `/admin/unrevoke`).
