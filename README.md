# Subscription Watchdog

Privacy-first web app that finds recurring charges in a bank/card **CSV export or pasted statement text**, flags price hikes and easy-to-forget subscriptions, shows monthly/yearly cost, and gives step-by-step cancel guides plus calendar reminders. **No bank logins, no marketing email, no third-party trackers.**

Docs: [`docs/competitors.md`](docs/competitors.md) · [`docs/mvp.md`](docs/mvp.md) · [`docs/storage-plan.md`](docs/storage-plan.md)

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
| | **Optional email recovery** (only when `EMAIL_PROVIDER` is configured): get the recovery link by email and re-send it later via `/recover` |

How it works (`app/payments.py`, routes in `app/main.py`):
1. `POST /checkout` creates a Stripe Checkout Session (`mode=payment`, `STRIPE_PRICE_ID` or inline `price_data` of `UNLOCK_PRICE_CENTS`) and sets a random `sw_checkout` nonce cookie; only its SHA-256 goes to Stripe as metadata.
2. `GET /checkout/success?session_id=…` verifies the session with Stripe (paid + nonce matches this browser), records the entitlement and shows the **recovery link** `/restore#code=swu1.…` (fragment, so it never hits server logs).
3. `POST /stripe/webhook` verifies the `Stripe-Signature` HMAC (5-min tolerance), is idempotent (processed event ids stored; one entitlement per session), handles `checkout.session.completed`, `checkout.session.async_payment_succeeded` (grant) and `charge.refunded` (full refund ⇒ revoke). If the buyer closed the tab, visiting `/pricing` in the same browser claims the webhook-recorded unlock.
4. The unlock code is HMAC-signed (`UNLOCK_SECRET`) over the session id. If the SQLite file is wiped (free hosting), a valid code is re-verified with Stripe on next use, so **purchases survive data loss**. The DB stores only: session id, payment-intent id (for refunds), sha256(code), nonce hash, status, timestamp. **No card data or name; no email address** (opt-in email recovery stores only a keyed hash — see below).

States: no keys ⇒ unlock button shown disabled as "coming soon" (free app fully works) · `DEV_UNLOCK=1` ⇒ adds a "Simulate unlock" button for local testing (ignored with `sk_live_` keys) · test keys ⇒ "TEST MODE" badge · live keys ⇒ real checkout (also requires `SUPPORT_EMAIL`).

### Optional email recovery
Off by default (`EMAIL_PROVIDER=none` ⇒ the UI says "Email recovery: not available yet" and `/recover` returns 503).
- **Opt-in only**, after purchase (success page or `/pricing`): email + consent checkbox ⇒ the link is emailed immediately.
- **Minimal storage:** we store only `HMAC-SHA256(EMAIL_HASH_KEY or key derived from UNLOCK_SECRET, normalised email)` linked to the entitlement — **never the address**. To re-send, the buyer types the email again; we match the hash and send to the typed address. A DB leak reveals no addresses and we can't market to anyone.
- **No enumeration:** `POST /recover` returns the identical page whether or not the address exists; lookup + send run in a background task after the response. **Rate limits** (in-memory): 5 requests/IP/hour, 3/address-hash/hour (counted whether or not it exists), 5 opt-in saves/unlock/hour.
- **Deletion:** "Delete my recovery email" on `/pricing`; also removed by **Delete my data** (in a browser holding the unlock) and automatically on a full refund.
- The email contains only the recovery link (`APP_BASE_URL/restore#code=…`) — no card or payment data.
- Providers (no SDKs, nothing to install): `smtp` (any SMTP server, STARTTLS/SSL), `resend` or `postmark` (HTTP APIs), `console` (logs the message — local testing only, refused with live Stripe keys). Tests mock every send; `smtplib` is patched to fail if a test ever tried to connect.
- Free-hosting caveat: on Render free (`PERSISTENT_STORAGE=0`) the SQLite file — including stored hashes — is wiped on restart, so a re-send may find nothing. The emailed link itself keeps working forever. Durable email recovery comes with paid storage ([`docs/storage-plan.md`](docs/storage-plan.md)).

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

### Going live (Stripe)
1. Deploy, then Stripe Dashboard → Developers → Webhooks → **Add endpoint**: `https://<your-app>.onrender.com/stripe/webhook`,
   events `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `charge.refunded`. Copy its signing secret into `STRIPE_WEBHOOK_SECRET`.
2. Set `STRIPE_SECRET_KEY` (secret key, or a restricted key with **write** on Checkout Sessions and **read** on Checkout Sessions/Charges/PaymentIntents), `UNLOCK_SECRET`, `APP_BASE_URL`, `SUPPORT_EMAIL` in Render.
3. Managed Payments: if it's on, Stripe is merchant of record, may add sales tax/VAT at checkout and may refund buyers itself (refunds still revoke the unlock via `charge.refunded`).
4. Do one real test purchase in test mode in a browser (card 4242 4242 4242 4242), then refund it from the Dashboard and confirm the unlock disappears.

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
| `STRIPE_TAX_CODE` | `txcd_10103000` | Product tax code for the inline price (SaaS, personal use). **Required** when the Stripe account has Managed Payments (on by default for new accounts) or Stripe Tax; Stripe rejects the checkout without it |
| `SUPPORT_EMAIL` | — | Support/refund contact shown on pricing, privacy and help pages; required before live keys are accepted. Set it to **`subscriptionwatchdog@mail.grokbot.com`** (pre-filled in the Render Blueprints; never hardcoded in code) |
| `EMAIL_PROVIDER` | `none` | `none` \| `smtp` \| `resend` \| `postmark` \| `console` (local only) — optional email recovery |
| `EMAIL_FROM` | — | Sender, e.g. `Subscription Watchdog <recovery@yourdomain.com>` (must be allowed by your provider) |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_SECURITY` | — / `587` / `starttls` | SMTP server; `SMTP_SECURITY` = `starttls` \| `ssl` (port 465) \| `none` |
| `SMTP_USER` / `SMTP_PASSWORD` | — | SMTP login (secret) |
| `RESEND_API_KEY` / `POSTMARK_TOKEN` | — | API credential for the chosen HTTP provider (secret) |
| `EMAIL_HASH_KEY` | derived from `UNLOCK_SECRET` | Optional separate key for email hashes. Never change once used |
| `RECOVERY_IP_LIMIT` / `RECOVERY_EMAIL_LIMIT` | `5` / `3` | Re-send requests per hour per IP / per address |
| `DEV_UNLOCK` | off | `1` = "Simulate unlock" button for local testing; never enable in production |

Payments are enabled only when `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `UNLOCK_SECRET` and `APP_BASE_URL` are all set.

## Testing the Unlock with Stripe **test** keys (no real money)
1. Stripe Dashboard → toggle **Test mode** → Developers → API keys → copy the `sk_test_…` secret key.
2. Install the [Stripe CLI](https://docs.stripe.com/stripe-cli) and run `stripe login`, then
   `stripe listen --events checkout.session.completed,checkout.session.async_payment_succeeded,charge.refunded --forward-to localhost:8000/stripe/webhook`
   — it prints a `whsec_…` secret (CLI ≥ 1.5x requires `--events`; `--print-secret` prints only the secret).
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
4. On Render set `STRIPE_SECRET_KEY` (live), `STRIPE_WEBHOOK_SECRET`, `APP_BASE_URL`, `SUPPORT_EMAIL=subscriptionwatchdog@mail.grokbot.com` (`UNLOCK_SECRET` is generated by the Blueprint; if you ever move to a new service, copy the old value over). Make sure `DEV_UNLOCK` is not set.
5. Do one real purchase + refund to confirm, and replace the refund-policy contact placeholder if `SUPPORT_EMAIL` is unset (live mode refuses to start checkout without it).
6. Optional: turn on email recovery by setting `EMAIL_PROVIDER` + `EMAIL_FROM` + the provider's credentials (any SMTP account you already have works; Resend/Postmark have free tiers but need sign-up and a verified sending domain — your call). Leave `EMAIL_PROVIDER=none` to keep it hidden.
7. Saved history is only offered with `PERSISTENT_STORAGE=1`, i.e. on `render.disk.yaml` (paid) or another persistent database. On the free plan the Unlock still sells (price-change history + `.ics`), purchases survive restarts, and the pricing page labels saved history "coming soon".

## Deploy to Render (free tier)
1. Push this repo to GitHub/GitLab (or Cursor Origin).
2. Render Dashboard → **New → Blueprint** → pick the repo. Render reads `render.yaml` (free web service, `pip install -r requirements.txt`, start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, health check `/healthz`).
3. Open the `*.onrender.com` URL. (Manual alternative: New → Web Service → Python, same build/start commands, plan *Free*.)

**Persistence caveat:** Render **persistent disks require a paid instance type** — the free plan can't attach one. On the free tier the SQLite file lives on the ephemeral filesystem (`DATA_DIR=/tmp/watchdog-data`) and **is lost on every redeploy/restart and when the idle instance spins down**. For this session-scoped, auto-deleting MVP that is acceptable. If you later want data to survive deploys, use `render.disk.yaml` (Blueprint path setting at creation) which sets `DATABASE_PATH=/var/data/watchdog.db` and mounts a 1 GB disk at `/var/data` on the smallest paid plan — **this costs money (≈ $7.25/mo: `0.5c-512mb` $7 + 1 GB disk $0.25, Render pricing checked 2026-10-26); nothing in this repo enables it automatically.** See [`docs/storage-plan.md`](docs/storage-plan.md). Note a disk also prevents scaling beyond one instance (fine for SQLite).

## Project layout
```
app/main.py        routes, sessions, security headers
app/parsing.py     CSV + pasted-text parsing
app/normalize.py   merchant normalization, digit masking
app/detect.py      cadence detection, price hikes, flags, totals
app/guides.py      cancel-guide library
app/ics.py         .ics generation
app/payments.py    Stripe Checkout + webhook signature + unlock codes (no SDK, httpx)
app/mailer.py      pluggable email (none/smtp/resend/postmark/console), off by default
app/recovery.py    email hashing, validation, rate limiter, recovery message
app/db.py          SQLAlchemy models, retention purge
app/templates/     Jinja2 UI     app/static/  CSS + icon
tests/             pytest suite   sample/  demo CSV
```

## Known limitations
- Detection needs ≥3 monthly charges for unknown merchants (2 for known services); annual renewals need ≥13 months of history.
- Rate limits are in-memory (per instance, reset on restart) — fine for one instance; move to the DB/Redis if you ever scale out.
- Retention purge runs at startup and on each import; add a scheduled purge if traffic is sparse on a long-lived instance.
- Not financial or legal advice; cancel steps change — always follow the provider's official page.
