"""Subscription Watchdog — FastAPI app."""
from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as dbm
from .db import Reminder, Transaction, UserSession, get_db
from .detect import MONTHS_PER, detect_subscriptions, totals, upcoming
from .guides import GENERIC, GUIDES, guide_for
from .ics import build_ics
from .normalize import mask_digits
from .parsing import ParseError, Txn, parse_csv, parse_pasted

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
COOKIE = "sw_session"
MAX_UPLOAD = 2 * 1024 * 1024
SECURE_COOKIE = os.getenv("SECURE_COOKIES", "0") == "1"

@asynccontextmanager
async def lifespan(_app):
    dbm.init_db()
    with dbm.SessionLocal() as db:
        dbm.purge_expired(db)
    yield


app = FastAPI(title="Subscription Watchdog", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals["money"] = lambda v: f"${v:,.2f}"
templates.env.globals["retention_days"] = dbm.RETENTION_DAYS


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- session helpers ----------
def current_sid(request: Request, db: Session) -> str | None:
    sid = request.cookies.get(COOKIE)
    if not sid:
        return None
    s = db.get(UserSession, sid)
    if not s:
        return None
    s.last_seen = datetime.now(timezone.utc)
    db.commit()
    return sid


def ensure_sid(request: Request, db: Session) -> tuple[str, bool]:
    sid = current_sid(request, db)
    if sid:
        return sid, False
    sid = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.add(UserSession(id=sid, created_at=now, last_seen=now))
    db.commit()
    return sid, True


def set_cookie(resp: Response, sid: str):
    resp.set_cookie(COOKIE, sid, httponly=True, samesite="lax", secure=SECURE_COOKIE, max_age=dbm.RETENTION_DAYS * 86400)


def load_txns(db: Session, sid: str | None) -> list[Txn]:
    if not sid:
        return []
    return [Txn(t.date, t.description, t.amount)
            for t in db.scalars(select(Transaction).where(Transaction.session_id == sid))]


def store_txns(db: Session, sid: str, txns: list[Txn]) -> int:
    existing = {(t.date, t.description, round(t.amount, 2)) for t in load_txns(db, sid)}
    added = 0
    for t in txns:
        desc = mask_digits(t.description)[:200]
        k = (t.date, desc, round(t.amount, 2))
        if k in existing:
            continue
        existing.add(k)
        db.add(Transaction(session_id=sid, date=t.date, description=desc, amount=t.amount))
        added += 1
    db.commit()
    return added


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    ctx = ctx or {}
    ctx.setdefault("has_data", bool(request.cookies.get(COOKIE)))
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def analysis(db: Session, sid: str | None):
    txns = load_txns(db, sid)
    subs = detect_subscriptions(txns, today=date.today())
    return txns, subs


# ---------- pages ----------
@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    sid = current_sid(request, db)
    n = len(load_txns(db, sid))
    return render(request, "index.html", {"txn_count": n})


async def _ingest(request: Request, db: Session, txns: list[Txn]):
    dbm.purge_expired(db)  # opportunistic retention enforcement
    sid, new = ensure_sid(request, db)
    added = store_txns(db, sid, txns)
    resp = RedirectResponse(f"/dashboard?added={added}&parsed={len(txns)}", status_code=303)
    set_cookie(resp, sid)
    return resp


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        return render(request, "index.html", {"error": "File too large (max 2 MB)."}, 413)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        txns = parse_csv(text)
    except ParseError as e:
        return render(request, "index.html", {"error": f"Couldn't read that CSV: {e}"}, 400)
    return await _ingest(request, db, txns)


@app.post("/paste")
async def paste(request: Request, text: str = Form(...), db: Session = Depends(get_db)):
    if len(text) > MAX_UPLOAD:
        return render(request, "index.html", {"error": "Text too long (max 2 MB)."}, 413)
    try:
        txns = parse_pasted(text)
    except ParseError as e:
        return render(request, "index.html", {"error": str(e), "pasted": text}, 400)
    return await _ingest(request, db, txns)


@app.post("/sample")
async def sample(request: Request, db: Session = Depends(get_db)):
    txns = parse_csv((ROOT / "sample" / "sample_statement.csv").read_text())
    shift = date.today() - timedelta(days=2) - max(t.date for t in txns)  # make the demo look current
    txns = [Txn(t.date + shift, t.description, t.amount) for t in txns]
    return await _ingest(request, db, txns)


@app.get("/sample.csv")
def sample_csv():
    return PlainTextResponse((ROOT / "sample" / "sample_statement.csv").read_text(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=sample_statement.csv"})


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, added: int | None = None, parsed: int | None = None, db: Session = Depends(get_db)):
    sid = current_sid(request, db)
    txns, subs = analysis(db, sid)
    if not txns:
        return RedirectResponse("/", status_code=303)
    today = date.today()
    return render(request, "dashboard.html", {
        "subs": subs, "totals": totals(subs), "upcoming": upcoming(subs, today, 14), "today": today,
        "txn_count": len(txns), "range": (min(t.date for t in txns), max(t.date for t in txns)),
        "added": added, "parsed": parsed,
    })


@app.get("/subscription/{slug}", response_class=HTMLResponse)
def subscription(slug: str, request: Request, db: Session = Depends(get_db)):
    sid = current_sid(request, db)
    _, subs = analysis(db, sid)
    sub = next((s for s in subs if s.slug == slug), None)
    if not sub:
        raise HTTPException(404, "Subscription not found")
    reminders = db.scalars(select(Reminder).where(Reminder.session_id == sid, Reminder.merchant == sub.label)).all()
    return render(request, "subscription.html", {
        "s": sub, "guide": guide_for(sub.merchant), "reminders": reminders,
        "default_remind": max(sub.next_date - timedelta(days=3), date.today()),
    })


@app.get("/reminders", response_class=HTMLResponse)
def reminders_page(request: Request, db: Session = Depends(get_db)):
    sid = current_sid(request, db)
    _, subs = analysis(db, sid)
    rems = db.scalars(select(Reminder).where(Reminder.session_id == sid).order_by(Reminder.remind_on)).all() if sid else []
    today = date.today()
    return render(request, "reminders.html", {
        "reminders": rems, "renewals": sorted([s for s in subs if s.active], key=lambda s: s.next_date),
        "today": today, "soon": today + timedelta(days=14), "subs": subs,
    })


@app.post("/reminders")
def add_reminder(request: Request, merchant: str = Form(...), remind_on: date = Form(...), note: str = Form(""),
                 next_url: str = Form("/reminders"), db: Session = Depends(get_db)):
    sid, new = ensure_sid(request, db)
    db.add(Reminder(session_id=sid, merchant=merchant[:100], remind_on=remind_on, note=note[:300]))
    db.commit()
    resp = RedirectResponse(next_url if next_url.startswith("/") and not next_url.startswith("//") else "/reminders", status_code=303)
    if new:
        set_cookie(resp, sid)
    return resp


@app.post("/reminders/{rid}/delete")
def delete_reminder(rid: int, request: Request, db: Session = Depends(get_db)):
    sid = current_sid(request, db)
    r = db.get(Reminder, rid)
    if r and r.session_id == sid:
        db.delete(r)
        db.commit()
    return RedirectResponse("/reminders", status_code=303)


@app.get("/calendar.ics")
def calendar(request: Request, db: Session = Depends(get_db)):
    sid = current_sid(request, db)
    _, subs = analysis(db, sid)
    events = [{"uid": f"renew-{s.slug}-{s.next_date:%Y%m%d}", "date": s.next_date,
               "summary": f"{s.label} renews (~${s.current_amount:.2f})",
               "description": f"Expected {s.cadence} charge of ${s.current_amount:.2f} "
                              f"(${s.yearly_cost:.2f}/yr). Cancel before this date if you no longer use it.",
               "alarm_days": 3} for s in subs if s.active]
    if sid:
        for r in db.scalars(select(Reminder).where(Reminder.session_id == sid)):
            events.append({"uid": f"reminder-{r.id}", "date": r.remind_on, "summary": f"Review: {r.merchant}",
                           "description": r.note or "Subscription Watchdog reminder", "alarm_days": 0})
    return Response(build_ics(events), media_type="text/calendar",
                    headers={"Content-Disposition": "attachment; filename=subscription-watchdog.ics"})


@app.get("/guides", response_class=HTMLResponse)
def guides(request: Request):
    return render(request, "guides.html", {"guides": sorted(GUIDES.values(), key=lambda g: g["name"].lower()), "generic": GENERIC})


@app.get("/guides/{slug}", response_class=HTMLResponse)
def guide(slug: str, request: Request):
    g = GENERIC if slug == "generic" else GUIDES.get(slug)
    if not g:
        raise HTTPException(404, "Guide not found")
    return render(request, "guide.html", {"guide": g})


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return render(request, "privacy.html", {"retention": dbm.RETENTION_DAYS})


@app.post("/delete-my-data")
def delete_my_data(request: Request, db: Session = Depends(get_db)):
    sid = request.cookies.get(COOKIE)
    if sid:
        dbm.delete_session_data(db, sid)
    resp = RedirectResponse("/?deleted=1", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/subscriptions")
def api_subscriptions(request: Request, db: Session = Depends(get_db)):
    sid = current_sid(request, db)
    _, subs = analysis(db, sid)
    return JSONResponse({
        "totals": totals(subs),
        "subscriptions": [{
            "merchant": s.label, "slug": s.slug, "cadence": s.cadence, "amount": s.current_amount,
            "monthly": s.monthly_cost, "yearly": s.yearly_cost, "active": s.active, "charges": s.count,
            "first": s.first_date.isoformat(), "last": s.last_date.isoformat(), "next": s.next_date.isoformat(),
            "price_hike": ({**s.price_hike, "date": s.price_hike["date"].isoformat()} if s.price_hike else None),
            "flags": s.flags, "review": s.review,
        } for s in subs],
    })


@app.get("/healthz")
def healthz():
    return {"ok": True}
