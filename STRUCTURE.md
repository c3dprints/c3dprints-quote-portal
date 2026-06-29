# C3D Prints Quote Portal — Structure & Source of Truth

This project has been patched manually many times, which caused routes to vanish,
buttons to disappear, and old files to overwrite the live UI. This document is the
single source of truth for **which files are live** and **how things connect**, so
those mistakes stop happening.

## How it's deployed

| Piece            | Lives in        | Served by                                                        |
|------------------|-----------------|-----------------------------------------------------------------|
| Customer form    | `index.html`    | **GitHub Pages**, repo root → https://c3dprints.github.io/c3dprints-quote-portal/ |
| Admin dashboard  | `admin.html`    | **GitHub Pages**, repo root → `.../admin.html`                  |
| API / backend    | `backend/`      | **Render** → https://c3dprints-quote-portal.onrender.com         |
| Database         | Supabase Postgres (`DATABASE_URL`)                                                 |
| File uploads     | Supabase Storage bucket (`SUPABASE_*`)                                             |

GitHub Pages publishes **everything in the repo root**. The frontend talks to the
backend via the hardcoded `API_BASE` in `index.html` and `admin.html`.

## LIVE files — edit these, never replace wholesale

- `index.html` — public customer quote-request form
- `admin.html` — admin dashboard (single page; `API_BASE` set near top)
- `backend/main.py` — FastAPI app, all routes
- `backend/ai_triage.py` — AI quote-assist logic
- `backend/pricing_engine.py` — price calculation
- `backend/auth.py` — admin login / token
- `backend/email_service.py` — Resend email sending
- `backend/supabase_schema.sql` — DB schema
- `backend/requirements.txt`, `backend/.env.example`

## NOT live — do not deploy / edit by mistake

- `Archive/` — old UI versions, zip bundles, patch notes. Reference only.
- `backend/Archive/` — old backend snapshots.
- `frontend/` — an older duplicate UI that nothing references and Pages does not
  serve. Kept pending investigation; do NOT assume it is live.

## Rules to avoid past breakage

1. Work on a branch, open a PR, review the diff. Never hand-edit on `main`.
2. The admin UI is `admin.html` in the **root** — not `frontend/admin.html`,
   not any `admin_master_v*.html`.
3. When adding a frontend `fetch()`, confirm the matching route exists in
   `backend/main.py`. When removing a route, grep the HTML for callers first.
4. Keep `backend/.env.example` in sync with every `os.getenv(...)` in `backend/`.

## Known issues

- FIXED: `admin.html`'s "AI Quote Assist" button (`POST /admin/requests/{id}/ai-quote-assist`)
  now has a backend route. The backend generates the text summary plus the structured
  fields the panel reads (`ai_quote_structured`), stored in two self-migrating columns.
  Requires a Render redeploy (so `init_db()` adds the columns) to go live.
- `init_db()` has no `ADD COLUMN IF NOT EXISTS` for some older columns it SELECTs
  (`checkout_*`, `paid*`, `tracking_token`, `customer_status_note`). The live DB already
  has them, but a brand-new DB relies on the `CREATE TABLE` block instead. Worth
  reconciling if a fresh environment is ever stood up.
