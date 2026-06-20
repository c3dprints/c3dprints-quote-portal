# C3D Prints Quote Portal

Starter backend + Shopify quote form for C3D Prints custom quote requests.

## What this does

Customer submits a custom quote request from Shopify.

FastAPI backend:
- receives the request
- validates fields
- calculates a rough quote if grams/hours are provided
- emails `hi@c3dprints.com`
- optionally uses OpenAI to generate an AI triage summary
- stores requests in a local SQLite database for now

## Recommended deploy path

- Backend: Render
- Frontend: Shopify page/custom liquid
- Email: Resend
- AI: OpenAI API

## Local setup

```bash
cd backend
python -m venv .venv
source .venv/bin/activate   # Mac/Linux
# .venv\Scripts\activate    # Windows

pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

Backend will run at:

```text
http://127.0.0.1:8000
```

Health check:

```text
http://127.0.0.1:8000/health
```

## Environment variables

Create `backend/.env` from `.env.example`.

Required for email:
- `RESEND_API_KEY`
- `QUOTE_NOTIFY_EMAIL=hi@c3dprints.com`
- `FROM_EMAIL=quotes@yourverifieddomain.com`

Required for AI:
- `OPENAI_API_KEY`

## Shopify setup

1. Open Shopify Admin.
2. Go to **Online Store → Pages**.
3. Create page: **Custom Quote Request**.
4. Add the form HTML from `frontend/shopify-custom-quote-form.html`.
5. Replace:

```js
const API_URL = "https://YOUR-RENDER-APP.onrender.com/quote-request";
```

with your deployed backend URL.

## Current limits

This first version does not auto-slice STL files yet.

For now:
- customer submits request
- backend emails you
- you slice manually
- use grams/hours in calculator or backend pricing
- approve/edit quote manually

Next upgrade:
- add file storage
- add Supabase
- add Shopify customer/order creation
- add slicer integration
