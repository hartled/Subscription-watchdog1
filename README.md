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
- **Reminders:** expected renewal dates, "renewing in 14 days" list, custom reminders, downloadable `.ics` (with 3-day alarms).
- **Privacy:** anonymous session cookie, only date/description/amount stored, long digit runs masked, auto-purge after `RETENTION_DAYS` (default 7), one-click **Delete my data**, strict security headers (CSP, no-store, DENY framing).
- JSON: `GET /api/subscriptions`; health: `GET /healthz`.

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
app/db.py          SQLAlchemy models, retention purge
app/templates/     Jinja2 UI     app/static/  CSS + icon
tests/             pytest suite   sample/  demo CSV
```

## Known limitations
- Detection needs ≥3 monthly charges for unknown merchants (2 for known services); annual renewals need ≥13 months of history.
- No CSRF tokens (SameSite=Lax cookie + `form-action 'self'` CSP mitigate); add tokens before adding accounts.
- Retention purge runs at startup and on each import; add a scheduled purge if traffic is sparse on a long-lived instance.
- Not financial or legal advice; cancel steps change — always follow the provider's official page.
