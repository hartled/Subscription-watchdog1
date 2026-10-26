# Paid persistent storage plan

Status: **planned, nothing enabled.** Today the app runs on Render's free web plan (`render.yaml`, `PERSISTENT_STORAGE=0`):
the SQLite file is wiped on every redeploy/restart/spin-down. Purchases still survive (signed unlock codes are re-verified
with Stripe), but **saved history** is shown as "coming soon" and **email re-send** may find nothing after a restart.

Prices below were checked on <https://render.com/pricing> and <https://render.com/docs/blueprint-spec> on **2026-10-26**.
Hobby workspace = $0/month + compute (Pro workspace is $25/mo — not needed).

## Options

| | A. Paid web + persistent disk (SQLite) — **recommended** | B. Free web + managed Postgres | C. Paid web + managed Postgres |
|---|---|---|---|
| Render pieces | Web plan `0.5c-512mb` (0.5 CPU / 512 MB) + 1 GB disk | Web `free` + Postgres `0.1c-256mb` (1 GB SSD incl.) | Web `0.5c-512mb` + Postgres `0.1c-256mb` |
| Monthly cost | **$7 + $0.25 = ~$7.25** | **~$6** | **~$13** |
| Code changes | None — `render.disk.yaml` already exists (`DATABASE_PATH=/var/data/watchdog.db`, `PERSISTENT_STORAGE=1`) | Add `psycopg` driver + `DATABASE_URL` support in `app/db.py`, port the tiny SQLite migration, drop SQLite PRAGMAs | Same as B |
| Backups | Automatic daily disk snapshots, kept 7 days (whole-disk restore only) | Paid Postgres: logical backups + PITR (3-day window on Hobby) | Same as B |
| Downsides | Single instance only (a disk prevents scaling); **no zero-downtime deploys** (a few seconds' downtime per deploy) | Free web still spins down after 15 min idle (slow cold start) | Highest cost |
| Notes | Disk is encrypted at rest. Plan ID `0.5c-512mb` in `render.disk.yaml` **verified correct** (also Render's default for new web services). | Don't use Render's *free* Postgres: it **expires after 30 days** and is deleted 14 days later. | Only worth it once we need >1 instance |

Not chosen: Key Value/Redis (wrong data model for this).

**Recommendation: Option A.** Zero code changes, cheapest always-on choice, SQLite is plenty for this traffic level, and the
one-file DB is easy to copy out. Revisit C only if we ever need multiple instances or zero-downtime deploys.

## Trigger (when to switch)
Switch when **either** happens:
1. **5 Unlocks sold** (5 × $7 = $35 gross, ≈ $32 after Stripe fees ≈ 4+ months of Option A), **or**
2. buyers ask for saved history / lose data after restarts (≥2 support emails about it).

Until then the free plan is fine: no feature we sell depends on the disk.

## Migration steps (Option A)
1. Get D's OK for the ~$7.25/mo charge (Render bills the card on file; prorated per second).
2. Render Dashboard → the `subscription-watchdog` service → **Settings → Instance type → `Starter` (`0.5c-512mb`)**.
   (Or create a fresh service from `render.disk.yaml` via New → Blueprint, Blueprint path `render.disk.yaml`; then copy
   the old service's `UNLOCK_SECRET` (and `EMAIL_HASH_KEY` if set) into the new one **before** going live — changing it breaks every existing code.)
3. **Disks → Add disk**: name `watchdog-data`, mount path `/var/data`, size 1 GB.
4. Environment: set `DATABASE_PATH=/var/data/watchdog.db`, `PERSISTENT_STORAGE=1`; remove `DATA_DIR` (or leave; `DATABASE_PATH` wins).
5. Deploy. Nothing to migrate: the free-tier DB is ephemeral anyway, and Unlock entitlements re-create themselves from Stripe the first time each code is used.
6. Verify: `/healthz` OK; buy a **test-mode** unlock, turn on saved history, opt in to email recovery, trigger a manual deploy, confirm history + `/recover` still work.
7. Pricing page automatically drops the "coming soon" badge on saved history (driven by `PERSISTENT_STORAGE`). Announce it to existing buyers only through the in-app page — we have no mailing list by design.

## Migration steps (Option B/C, if ever)
1. Add `psycopg[binary]` to `requirements.txt`; in `app/db.py` use `DATABASE_URL` when set (`postgresql+psycopg://…`), keep SQLite otherwise; guard the SQLite-only PRAGMAs and `_migrate`.
2. Create the database (`plan: 0.1c-256mb` under `databases:` in a Blueprint) and wire `DATABASE_URL` via `fromDatabase.connectionString`.
3. If moving from Option A, copy rows with a one-off script (SQLAlchemy reflect → insert), then switch `DATABASE_URL` and keep the disk for a week as a fallback.
4. Rate limits are in-memory; with >1 instance move them to the database or Key Value.

## Costs at a glance (Render, verified 2026-10-26)
- Web: `free` $0 · `0.5c-512mb` $7/mo · `1c-2g` $25/mo
- Persistent disk: $0.25/GB/month (paid web plans only)
- Postgres: `free` $0 (expires in 30 days) · `0.1c-256mb` $6/mo · `0.5c-1g` $19/mo; extra storage $0.30/GB
- Bandwidth: 5 GB/month included on Hobby
