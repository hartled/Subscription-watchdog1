# Subscription Watchdog

Privacy-first web app that finds recurring charges in a bank/card **CSV export or pasted statement text**, flags price hikes and easy-to-forget subscriptions, shows monthly/yearly cost, and gives step-by-step cancel guides plus calendar reminders. **No bank logins, no emails, no third-party trackers.**

Docs: [`docs/competitors.md`](docs/competitors.md) · [`docs/mvp.md`](docs/mvp.md)

## Features
- **Import:** CSV upload (auto-detects `date/description/amount`, `debit/credit` or `withdrawal/deposit` split, `Credit/Debit` type column, negative-or-positive = spend, preamble lines, `, ; tab |` delimiters, headerless files) or pasted statement lines (`01/15 NETFLIX.COM 15.49`, `Jan 9, 2026 …`, `… 10.00 CR`, trailing balances).
- **Merchant normalization:** strips processor prefixes (`SQ *`, `TST*`, `PAYPAL *`…), store numbers, phone numbers; alias map for ~30 popular services.
- **Detection:** weekly / biweekly / monthly / quarterly / yearly with date tolerance; rejects variable-amount shopping; splits two plans from one merchant (e.g. two Apple charges); ignores one-off outliers (e.g. gym annual fee); marks stopped subscriptions inactive.
- **Flags:** price hike (old → new, % and extra $/yr), long-running, small (≤$10/mo), annual, recently increased; "worth reviewing" when ≥2 signals.
- **Totals:** monthly + yearly across active subscriptions.
- **Cancel guides:** 29 services + generic fallback, with official links (checked Oct 2026).
- **Reminders:** expected renewal dates, "renewing in 14 days" list, custom in-app reminders (free); downloadable `.ics` with 3-day alarms (Unlock).
- **Privacy:** anonymous session cookie, only date/description/amount stored, long digit runs masked, auto-purge after `RETENTION_DAYS` (default 7), one-click **Delete my data**, strict security headers (CSP, no-store, DENY framing).
- JSON: `GET /api/subscriptions`; health: `GET /healthz`.
- **CSRF protection** on every POST form (double-submit token: `sw_csrf` cookie + hidden `csrf_token` field).

## Free vs. Unlock (one-time, default $7)
| Free, unlimited | Unlock (one-time Stripe Checkout payment) |
|---|---|
| CSV/paste import, detection, monthly/yearly totals | **Saved history**: opt-in, kept across visits/devices, restored with a private recovery link (no account/password). Only offered when `PERSISTENT_STORAGE=1` |
| Price-hike flags + latest hikes on `/alerts` | **Full price-change history** on `/alerts` (every step, $/yr impact, extra paid since) |
| Cancel guides, renewal dates, in-app reminders | **Calendar export** `/calendar.ics` (renewals + reminders, 3-day alarms) |

How it works (`app/payments.py`, routes in `app/main.py`):
1. `POST /checkout` creates a Stripe Checkout Session (`mode=payment`, `STRIPE_PRICE_ID` or inline `price_data` of `UNLOCK_PRICE_CENTS`) and sets a random `sw_checkout` nonce cookie; only its SHA-256 goes to Stripe as metadata.
2. `GET /checkout/success?session_id=…` verifies the session with Stripe (paid + nonce matches this browser), records the entitlement and shows the **recovery link** `/restore#code=swu1.…` (fragment, so it never hits server logs).
3. `POST /stripe/webhook` verifies the `Stripe-Signature` HMAC (5-min tolerance), is idempotent (processed event ids stored; one entitlement per session), handles `checkout.session.completed`, `checkout.session.async_payment_succeeded` (grant) and `charge.refunded` (full refund ⇒ revoke). If the buyer closed the tab, visiting `/pricing` in the same browser claims the webhook-recorded unlock.
4. The unlock code is HMAC-signed (`UNLOCK_SECRET`) over the session id. If the SQLite file is wiped (free hosting), a valid code is re-verified with Stripe on next use, so **purchases survive data loss**. The DB stores only: session id, payment-intent id (for refunds), sha256(code), nonce hash, status, timestamp. **No card data, name or email.**

States: no keys ⇒ unlock button shown disabled as "coming soon" (free app fully works) · `DEV_UNLOCK=1` ⇒ adds a "Simulate unlock" button for local testing (ignored with `sk_live_` keys) · test keys ⇒ "TEST MODE" badge · live keys ⇒ real checkout (also requires `SUPPORT_EMAIL`).

## Run locally
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload        # http://127.0.0.1:8000  → "Try it with sample data"
pytest -q                            # parsing, detection and HTTP-flow tests
```
Sample data: `sample/sample_statement.csv` (regenerate with `python sample/make_sample.py`).

With Docker:
```bash
docker build -t subscription-watchdog .
docker run -p 8000:8000 -v "$PWD/data:/data" subscription-watchdog
```

### Configuration (env vars)
| Var | Default | Meaning |
|---|---|---|
| `DATABASE_PATH` | — | Full path to the SQLite file (wins over `DATA_DIR`) |
| `DATA_DIR` | `./data` (`/data` in Docker) | Directory for `watchdog.db` |
| `RETENTION_DAYS` | `7` | Inactive sessions are purged after this many days (checked at startup and on each import) |
| `SECURE_COOKIES` | `0` | Set `1` behind HTTPS (Render) so the session cookie is `Secure` |
| `PERSISTENT_STORAGE` | `1` | `0` when the disk is wiped on restart (Render free): shows a data-loss notice and does **not** offer saved history |
| `SAVED_RETENTION_DAYS` | `365` | Opt-in saved history is purged after this many days without a visit |
| `STRIPE_SECRET_KEY` | — | `sk_test_…` / `sk_live_…` (secret) |
| `STRIPE_WEBHOOK_SECRET` | — | `whsec_…` for `<APP_BASE_URL>/stripe/webhook` (secret) |
| `UNLOCK_SECRET` | — | Long random string that signs unlock codes (secret). **Never change it** — existing codes would stop working |
| `APP_BASE_URL` | — | Public URL, no trailing slash (used for Stripe success/cancel URLs) |
| `STRIPE_PRICE_ID` | — | Optional `price_…`; otherwise inline price of `UNLOCK_PRICE_CENTS` |
| `UNLOCK_PRICE_CENTS` | `700` | Display price (and inline price) |
| `SUPPORT_EMAIL` | — | Shown in the refund policy; required before live keys are accepted |
| `DEV_UNLOCK` | off | `1` = "Simulate unlock" button for local testing; never enable in production |

Payments are enabled only when `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `UNLOCK_SECRET` and `APP_BASE_URL` are all set.

## Testing the Unlock with Stripe **test** keys (no real money)
1. Stripe Dashboard → toggle **Test mode** → Developers → API keys → copy the `sk_test_…` secret key.
2. Install the [Stripe CLI](https://docs.stripe.com/stripe-cli) and run `stripe login`, then
   `stripe listen --forward-to localhost:8000/stripe/webhook` — it prints a `whsec_…` secret.
3. Run the app:
   ```bash
   export STRIPE_SECRET_KEY=sk_test_… STRIPE_WEBHOOK_SECRET=whsec_… \
          UNLOCK_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))") APP_BASE_URL=http://localhost:8000
   uvicorn app.main:app --reload
   ```
4. `/pricing` → **Unlock** → pay with card `4242 4242 4242 4242`, any future expiry, any CVC/ZIP → you land on the success page with your recovery link. Check `/alerts`, `/calendar.ics`, *Save my history*, then open the recovery link in a private window.
5. Refund test: Dashboard → Payments → the payment → Refund (full). The CLI forwards `charge.refunded`; the unlock stops working.
6. Without Stripe at all: `DEV_UNLOCK=1 uvicorn app.main:app --reload` and use "Simulate unlock".

## Going live (D's checklist)
1. Create/activate a Stripe account (business details + bank account for payouts).
2. Optional: create a Product "Subscription Watchdog Unlock" with a one-time $7 USD price → copy `price_…` into `STRIPE_PRICE_ID` (otherwise the inline $7 price is used).
3. Developers → Webhooks → **Add endpoint** `https://<your-app>/stripe/webhook`, events: `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `charge.refunded` → copy its signing secret into `STRIPE_WEBHOOK_SECRET`.
4. On Render set `STRIPE_SECRET_KEY` (live), `STRIPE_WEBHOOK_SECRET`, `APP_BASE_URL`, `SUPPORT_EMAIL` (`UNLOCK_SECRET` is generated by the Blueprint; if you ever move to a new service, copy the old value over). Make sure `DEV_UNLOCK` is not set.
5. Do one real purchase + refund to confirm, and replace the refund-policy contact placeholder if `SUPPORT_EMAIL` is unset (live mode refuses to start checkout without it).
6. Saved history is only offered with `PERSISTENT_STORAGE=1`, i.e. on `render.disk.yaml` (paid) or another persistent database. On the free plan the Unlock still sells (price-change history + `.ics`), purchases survive restarts, and the pricing page labels saved history "coming soon".

## Deploy to Render (free tier)
1. Push this repo to GitHub/GitLab (or Cursor Origin).
2. Render Dashboard → **New → Blueprint** → pick the repo. Render reads `render.yaml` (free web service, `pip install -r requirements.txt`, start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, health check `/healthz`).
3. Open the `*.onrender.com` URL. (Manual alternative: New → Web Service → Python, same build/start commands, plan *Free*.)

**Persistence caveat:** Render **persistent disks require a paid instance type** — the free plan can't attach one. On the free tier the SQLite file lives on the ephemeral filesystem (`DATA_DIR=/tmp/watchdog-data`) and **is lost on every redeploy/restart and when the idle instance spins down**. For this session-scoped, auto-deleting MVP that is acceptable. If you later want data to survive deploys, use `render.disk.yaml` (Blueprint path setting at creation) which sets `DATABASE_PATH=/var/data/watchdog.db` and mounts a 1 GB disk at `/var/data` on the smallest paid plan — **this costs money; nothing in this repo enables it automatically.** Note a disk also prevents scaling beyond one instance (fine for SQLite).

## Project layout
```
app/main.py        routes, sessions, security headers
app/parsing.py     CSV + pasted-text parsing
app/normalize.py   merchant normalization, digit masking
app/detect.py      cadence detection, price hikes, flags, totals
app/guides.py      cancel-guide library
app/ics.py         .ics generation
app/payments.py    Stripe Checkout + webhook signature + unlock codes (no SDK, httpx)
app/db.py          SQLAlchemy models, retention purge
app/templates/     Jinja2 UI     app/static/  CSS + icon
tests/             pytest suite   sample/  demo CSV
```

## Known limitations
- Detection needs ≥3 monthly charges for unknown merchants (2 for known services); annual renewals need ≥13 months of history.
- Retention purge runs at startup and on each import; add a scheduled purge if traffic is sparse on a long-lived instance.
- Not financial or legal advice; cancel steps change — always follow the provider's official page.
