"""Subscription Watchdog — FastAPI app."""
from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from jinja2 import pass_context
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import httpx

from . import db as dbm
from . import mailer
from . import payments as pay
from . import recovery as rec
from .db import Entitlement, RecoveryEmail, Reminder, Transaction, UserSession, WebhookEvent, get_db
from .detect import MONTHS_PER, detect_subscriptions, price_changes, totals, upcoming
from .guides import GENERIC, GUIDES, guide_for
from .ics import build_ics
from .normalize import mask_digits
from .parsing import ParseError, Txn, parse_csv, parse_pasted

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
COOKIE = "sw_session"
UNLOCK_COOKIE = "sw_unlock"        # holds the signed unlock code (proof of purchase); no personal data
PENDING_COOKIE = "sw_checkout"     # random nonce tying a Stripe checkout to the buyer's browser
CSRF_COOKIE = "sw_csrf"
MAX_UPLOAD = 2 * 1024 * 1024
MAX_WEBHOOK = 256 * 1024
SECURE_COOKIE = os.getenv("SECURE_COOKIES", "0") == "1"
log = logging.getLogger("watchdog")


def persistent_storage() -> bool:
    """False on hosts whose disk is wiped on restart (Render free). Saved history is only sold when True."""
    return os.getenv("PERSISTENT_STORAGE", "1") == "1"

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
templates.env.globals["saved_retention_days"] = dbm.SAVED_RETENTION_DAYS


@pass_context
def _csrf_field(ctx):
    tok = getattr(ctx["request"].state, "csrf_token", "")
    return Markup(f'<input type="hidden" name="csrf_token" value="{tok}">')


templates.env.globals["csrf_field"] = _csrf_field


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # CSRF: double-submit token. A random value lives in a SameSite=Lax cookie and must be echoed back in every
    # POST form (csrf_field()). Cross-site pages can neither read the cookie nor guess the value.
    tok = request.cookies.get(CSRF_COOKIE, "")
    new_tok = not (20 <= len(tok) <= 64 and tok.replace("-", "").replace("_", "").isalnum())
    if new_tok:
        tok = secrets.token_urlsafe(32)
    request.state.csrf_token = tok
    resp = await call_next(request)
    if new_tok:
        resp.set_cookie(CSRF_COOKIE, tok, httponly=True, samesite="lax", secure=SECURE_COOKIE, max_age=400 * 86400)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # form-action must allow the redirect to Stripe's hosted checkout after POST /checkout.
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self'; img-src 'self' data:; "
                                               "form-action 'self' https://checkout.stripe.com; frame-ancestors 'none'")
    resp.headers["Cache-Control"] = "no-store"
    return resp


class CSRFError(Exception):
    pass


async def verify_csrf(request: Request):
    form = await request.form()  # cached by Starlette; the route's own Form/File params reuse it
    sent = str(form.get("csrf_token") or request.headers.get("x-csrf-token") or "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not cookie or not sent or not secrets.compare_digest(sent, cookie):
        raise CSRFError()


@app.exception_handler(CSRFError)
async def csrf_failed(request: Request, _exc):
    return render(request, "error.html", {"title": "Form expired",
                  "message": "For your security this form needs a fresh page. Go back, reload the page and try again."}, 403)


CSRF = [Depends(verify_csrf)]


# ---------- entitlement (Unlock) helpers ----------
def upsert_entitlement(db: Session, session_id: str, *, nonce_hash: str | None = None,
                       payment_intent: str | None = None, source: str = "stripe") -> Entitlement:
    """Idempotent: one row per Checkout Session no matter how many times webhook/success page race."""
    ent = db.scalar(select(Entitlement).where(Entitlement.stripe_session_id == session_id))
    if ent is None:
        ent = Entitlement(stripe_session_id=session_id, token_hash=pay.sha256(pay.make_token(session_id)),
                          nonce_hash=nonce_hash, payment_intent=payment_intent, status="paid", source=source,
                          created_at=datetime.now(timezone.utc))
        db.add(ent)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            ent = db.scalar(select(Entitlement).where(Entitlement.stripe_session_id == session_id))
    else:
        changed = False
        if nonce_hash and not ent.nonce_hash:
            ent.nonce_hash, changed = nonce_hash, True
        if payment_intent and not ent.payment_intent:
            ent.payment_intent, changed = payment_intent, True
        if changed:
            db.commit()
    return ent


def entitlement_for_token(db: Session, token: str | None) -> Entitlement | None:
    session_id = pay.verify_token(token)
    if not session_id:
        return None
    ent = db.scalar(select(Entitlement).where(Entitlement.stripe_session_id == session_id))
    if ent is None and pay.payments_enabled() and session_id.startswith("cs_"):
        # DB was wiped (free hosting) or webhook not processed yet: the signed code is still valid — re-check with Stripe.
        try:
            sess = pay.retrieve_session(session_id)
        except (pay.StripeError, httpx.HTTPError) as e:
            log.warning("could not re-verify unlock with Stripe: %s", e)
            return None
        if pay.session_is_paid_unlock(sess) and not pay.session_refunded(sess):
            ent = upsert_entitlement(db, session_id, nonce_hash=(sess.get("metadata") or {}).get("nonce_hash"),
                                     payment_intent=pay.payment_intent_id(sess))
    return ent if ent is not None and ent.status == "paid" else None


def current_entitlement(request: Request, db: Session) -> Entitlement | None:
    if not hasattr(request.state, "entitlement"):
        request.state.entitlement = entitlement_for_token(db, request.cookies.get(UNLOCK_COOKIE))
    return request.state.entitlement


def set_unlock_cookie(resp: Response, token: str):
    resp.set_cookie(UNLOCK_COOKIE, token, httponly=True, samesite="lax", secure=SECURE_COOKIE, max_age=400 * 86400)


def merge_sessions(db: Session, src: str, dst: str):
    """Move transactions/reminders from session src into dst (duplicates skipped), then drop src."""
    if src == dst:
        return
    store_txns(db, dst, load_txns(db, src))
    for r in db.scalars(select(Reminder).where(Reminder.session_id == src)):
        r.session_id = dst
    db.commit()
    dbm.delete_session_data(db, src)


# ---------- session helpers ----------
def current_sid(request: Request, db: Session) -> str | None:
    sid = request.cookies.get(COOKIE)
    s = db.get(UserSession, sid) if sid else None
    if not s and request.cookies.get(UNLOCK_COOKIE):
        ent = current_entitlement(request, db)  # unlocked browser with opted-in saved history
        if ent and ent.saved_session_id:
            sid, s = ent.saved_session_id, db.get(UserSession, ent.saved_session_id)
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


def set_cookie(resp: Response, sid: str, saved: bool = False):
    days = dbm.SAVED_RETENTION_DAYS if saved else dbm.RETENTION_DAYS
    resp.set_cookie(COOKIE, sid, httponly=True, samesite="lax", secure=SECURE_COOKIE, max_age=days * 86400)


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
    ent = getattr(request.state, "entitlement", None)
    if not hasattr(request.state, "entitlement") and request.cookies.get(UNLOCK_COOKIE) and dbm.SessionLocal is not None:
        with dbm.SessionLocal() as db:
            ent = current_entitlement(request, db)
    ctx.setdefault("unlocked", ent is not None)
    ctx.setdefault("has_data", bool(request.cookies.get(COOKIE)) or bool(ent and ent.saved_session_id))
    ctx.setdefault("pay_mode", pay.mode())
    ctx.setdefault("price", pay.price_display())
    ctx.setdefault("persistent", persistent_storage())
    ctx.setdefault("support_email", pay.env("SUPPORT_EMAIL"))
    ctx.setdefault("email_recovery", mailer.enabled())
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
    sess = db.get(UserSession, sid)
    set_cookie(resp, sid, saved=bool(sess and sess.saved))
    return resp


@app.post("/upload", dependencies=CSRF)
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


@app.post("/paste", dependencies=CSRF)
async def paste(request: Request, text: str = Form(...), db: Session = Depends(get_db)):
    if len(text) > MAX_UPLOAD:
        return render(request, "index.html", {"error": "Text too long (max 2 MB)."}, 413)
    try:
        txns = parse_pasted(text)
    except ParseError as e:
        return render(request, "index.html", {"error": str(e), "pasted": text}, 400)
    return await _ingest(request, db, txns)


@app.post("/sample", dependencies=CSRF)
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
        "added": added, "parsed": parsed, "unlocked": current_entitlement(request, db) is not None,
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


@app.post("/reminders", dependencies=CSRF)
def add_reminder(request: Request, merchant: str = Form(...), remind_on: date = Form(...), note: str = Form(""),
                 next_url: str = Form("/reminders"), db: Session = Depends(get_db)):
    sid, new = ensure_sid(request, db)
    db.add(Reminder(session_id=sid, merchant=merchant[:100], remind_on=remind_on, note=note[:300]))
    db.commit()
    resp = RedirectResponse(next_url if next_url.startswith("/") and not next_url.startswith("//") else "/reminders", status_code=303)
    if new:
        set_cookie(resp, sid)
    return resp


@app.post("/reminders/{rid}/delete", dependencies=CSRF)
def delete_reminder(rid: int, request: Request, db: Session = Depends(get_db)):
    sid = current_sid(request, db)
    r = db.get(Reminder, rid)
    if r and r.session_id == sid:
        db.delete(r)
        db.commit()
    return RedirectResponse("/reminders", status_code=303)


@app.get("/calendar.ics")
def calendar(request: Request, db: Session = Depends(get_db)):
    if not current_entitlement(request, db):
        return RedirectResponse("/pricing?feature=calendar", status_code=303)  # premium extra
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


@app.post("/delete-my-data", dependencies=CSRF)
def delete_my_data(request: Request, db: Session = Depends(get_db)):
    sid = current_sid(request, db) or request.cookies.get(COOKIE)
    if sid:
        dbm.delete_session_data(db, sid)  # also unlinks any saved history; the purchase record itself stays
    ent = current_entitlement(request, db)
    if ent:
        dbm.delete_recovery_email(db, ent.id)  # opt-in recovery email (hash) goes too
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


# ---------- Unlock: pricing, checkout, webhook, restore ----------
def _pending_nonce(request: Request) -> str | None:
    n = request.cookies.get(PENDING_COOKIE)
    return n if n and len(n) <= 64 else None


def _unlock_ctx(request: Request, db: Session) -> dict:
    ent = current_entitlement(request, db)
    token = request.cookies.get(UNLOCK_COOKIE) if ent else None
    saved = ent is not None and ent.saved_session_id is not None and db.get(UserSession, ent.saved_session_id) is not None
    email_on_file = ent is not None and db.scalar(
        select(RecoveryEmail.id).where(RecoveryEmail.entitlement_id == ent.id)) is not None
    return {"ent": ent, "token": token, "saved": saved, "missing": pay.missing_config(), "email_on_file": email_on_file}


@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request, db: Session = Depends(get_db)):
    ent = current_entitlement(request, db)
    nonce = _pending_nonce(request)
    if not ent and nonce:
        # Paid, but the browser never made it back to the success page (tab closed) — webhook already recorded it.
        claimed = db.scalar(select(Entitlement).where(Entitlement.nonce_hash == pay.sha256(nonce),
                                                      Entitlement.status == "paid"))
        if claimed:
            return RedirectResponse(f"/checkout/success?session_id={claimed.stripe_session_id}", status_code=303)
    return render(request, "pricing.html", _unlock_ctx(request, db))


@app.post("/checkout", dependencies=CSRF)
def checkout(request: Request, db: Session = Depends(get_db)):
    if current_entitlement(request, db):
        return RedirectResponse("/pricing", status_code=303)
    if not pay.payments_enabled():
        return render(request, "pricing.html", {**_unlock_ctx(request, db),
                      "error": "Payments aren't switched on yet — nothing was charged."}, 503)
    nonce = secrets.token_urlsafe(24)
    try:
        sess = pay.create_checkout_session(pay.sha256(nonce))
    except (pay.StripeError, httpx.HTTPError) as e:
        log.error("checkout session failed: %s", e)
        return render(request, "pricing.html", {**_unlock_ctx(request, db),
                      "error": "Couldn't reach the payment provider. Nothing was charged — please try again."}, 502)
    url = sess.get("url") or ""
    if not url.startswith("https://checkout.stripe.com/"):
        log.error("unexpected checkout url: %r", url[:80])
        return render(request, "pricing.html", {**_unlock_ctx(request, db), "error": "Payment setup error."}, 502)
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(PENDING_COOKIE, nonce, httponly=True, samesite="lax", secure=SECURE_COOKIE, max_age=2 * 86400)
    return resp


@app.get("/checkout/success", response_class=HTMLResponse)
def checkout_success(request: Request, session_id: str = "", db: Session = Depends(get_db)):
    session_id = session_id[:255]
    ent = current_entitlement(request, db)
    if ent and ent.stripe_session_id == session_id:  # refresh / dev unlock
        return render(request, "success.html", _unlock_ctx(request, db))
    nonce = _pending_nonce(request)
    if not session_id.startswith("cs_") or not nonce:
        return render(request, "success.html", {"problem": "browser"}, 400)
    ent = db.scalar(select(Entitlement).where(Entitlement.stripe_session_id == session_id))
    if ent is None:
        if not pay.payments_enabled():
            return render(request, "success.html", {"problem": "browser"}, 400)
        try:
            sess = pay.retrieve_session(session_id)
        except pay.StripeError as e:
            log.warning("success page could not verify session: %s", e)
            if e.status == 404:  # unknown/foreign session id: not a temporary problem
                return render(request, "success.html", {"problem": "browser"}, 400)
            return render(request, "success.html", {"problem": "pending"}, 502)
        except httpx.HTTPError as e:
            log.warning("success page could not reach Stripe: %s", e)
            return render(request, "success.html", {"problem": "pending"}, 502)
        if (sess.get("metadata") or {}).get("nonce_hash") != pay.sha256(nonce):
            return render(request, "success.html", {"problem": "browser"}, 400)
        if not pay.session_is_paid_unlock(sess):
            return render(request, "success.html", {"problem": "pending"}, 202)
        ent = upsert_entitlement(db, session_id, nonce_hash=pay.sha256(nonce), payment_intent=pay.payment_intent_id(sess))
    elif ent.nonce_hash != pay.sha256(nonce):
        return render(request, "success.html", {"problem": "browser"}, 400)
    if ent.status != "paid":
        return render(request, "success.html", {"problem": "refunded"}, 410)
    token = pay.make_token(session_id)
    request.state.entitlement = ent
    resp = render(request, "success.html", {**_unlock_ctx(request, db), "token": token, "unlocked": True})
    set_unlock_cookie(resp, token)
    resp.delete_cookie(PENDING_COOKIE)
    return resp


def _handle_event(db: Session, event: dict):
    etype, obj = event.get("type", ""), (event.get("data") or {}).get("object") or {}
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if pay.session_is_paid_unlock(obj):
            upsert_entitlement(db, obj["id"], nonce_hash=(obj.get("metadata") or {}).get("nonce_hash"),
                               payment_intent=pay.payment_intent_id(obj))
    elif etype == "charge.refunded":
        if obj.get("refunded") and obj.get("payment_intent"):  # full refund => revoke the unlock
            for ent in db.scalars(select(Entitlement).where(Entitlement.payment_intent == obj["payment_intent"])):
                ent.status = "refunded"
                db.execute(delete(RecoveryEmail).where(RecoveryEmail.entitlement_id == ent.id))  # no longer needed
            db.commit()


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    # Deliberately no CSRF token: authenticity comes from Stripe's HMAC signature instead.
    payload = await request.body()
    if len(payload) > MAX_WEBHOOK:
        return JSONResponse({"error": "payload too large"}, 413)
    try:
        event = pay.verify_webhook(payload, request.headers.get("stripe-signature"))
    except pay.SignatureError as e:
        log.warning("rejected webhook: %s", e)
        return JSONResponse({"error": "invalid signature"}, 400)
    eid = str(event.get("id") or "")
    if not eid:
        return JSONResponse({"error": "no event id"}, 400)
    if db.get(WebhookEvent, eid):
        return {"received": True, "duplicate": True}
    _handle_event(db, event)  # raises => 500 => Stripe retries later (event not marked processed)
    db.add(WebhookEvent(id=eid, type=str(event.get("type", ""))[:100], received_at=datetime.now(timezone.utc)))
    try:
        db.commit()
    except IntegrityError:  # concurrent duplicate delivery; handlers are idempotent anyway
        db.rollback()
        return {"received": True, "duplicate": True}
    return {"received": True}


@app.post("/unlock/dev", dependencies=CSRF)
def dev_unlock(request: Request, db: Session = Depends(get_db)):
    if not pay.dev_unlock_enabled():
        raise HTTPException(404)
    session_id = "dev_" + secrets.token_urlsafe(16)
    upsert_entitlement(db, session_id, source="dev")
    resp = RedirectResponse(f"/checkout/success?session_id={session_id}", status_code=303)
    set_unlock_cookie(resp, pay.make_token(session_id))
    return resp


@app.get("/restore", response_class=HTMLResponse)
def restore_page(request: Request):
    # The code travels in the URL *fragment* (/restore#code=…), which browsers never send to the server or logs;
    # static/restore.js copies it into the form.
    return render(request, "restore.html", {})


@app.post("/restore", dependencies=CSRF)
def restore(request: Request, code: str = Form(""), db: Session = Depends(get_db)):
    code = code.strip()
    ent = entitlement_for_token(db, code)
    if not ent:
        return render(request, "restore.html", {"error": "That code isn't valid (or the purchase was refunded)."}, 400)
    request.state.entitlement = ent
    resp = RedirectResponse("/pricing?restored=1", status_code=303)
    set_unlock_cookie(resp, code)
    if ent.saved_session_id and db.get(UserSession, ent.saved_session_id):
        here = request.cookies.get(COOKIE)
        if here and db.get(UserSession, here):
            merge_sessions(db, here, ent.saved_session_id)  # bring anything uploaded in this browser along
        set_cookie(resp, ent.saved_session_id, saved=True)
        resp.headers["location"] = "/dashboard"
    return resp


@app.post("/unlock/forget", dependencies=CSRF)
def forget_unlock(request: Request, db: Session = Depends(get_db)):
    """Sign this browser out of the unlock (e.g. a shared computer). The code still works elsewhere."""
    resp = RedirectResponse("/pricing?forgot=1", status_code=303)
    resp.delete_cookie(UNLOCK_COOKIE)
    ent = current_entitlement(request, db)
    if ent and ent.saved_session_id and request.cookies.get(COOKIE) == ent.saved_session_id:
        resp.delete_cookie(COOKIE)
    return resp


@app.post("/history/save", dependencies=CSRF)
def history_save(request: Request, db: Session = Depends(get_db)):
    ent = current_entitlement(request, db)
    if not ent:
        return RedirectResponse("/pricing?feature=history", status_code=303)
    if not persistent_storage():
        return RedirectResponse("/pricing", status_code=303)
    sid, _ = ensure_sid(request, db)
    if ent.saved_session_id and ent.saved_session_id != sid and db.get(UserSession, ent.saved_session_id):
        merge_sessions(db, sid, ent.saved_session_id)
        sid = ent.saved_session_id
    ent.saved_session_id = sid
    db.get(UserSession, sid).saved = True
    db.commit()
    resp = RedirectResponse("/pricing?saved=1", status_code=303)
    set_cookie(resp, sid, saved=True)
    return resp


@app.post("/history/stop", dependencies=CSRF)
def history_stop(request: Request, db: Session = Depends(get_db)):
    ent = current_entitlement(request, db)
    if ent and ent.saved_session_id:
        sess = db.get(UserSession, ent.saved_session_id)
        if sess:
            sess.saved = False  # back to normal auto-delete after RETENTION_DAYS of inactivity
            sess.last_seen = datetime.now(timezone.utc)
        ent.saved_session_id = None
        db.commit()
        resp = RedirectResponse("/pricing?stopped=1", status_code=303)
        if sess:
            set_cookie(resp, sess.id)
        return resp
    return RedirectResponse("/pricing", status_code=303)


@app.get("/alerts", response_class=HTMLResponse)
def alerts(request: Request, db: Session = Depends(get_db)):
    sid = current_sid(request, db)
    txns, subs = analysis(db, sid)
    unlocked = current_entitlement(request, db) is not None
    hikes = [s for s in subs if s.price_hike]  # the headline flags stay free
    history = []
    if unlocked:
        for s in subs:
            ch = price_changes(s)
            if ch:
                history.append((s, ch))
        history.sort(key=lambda x: max(c["date"] for c in x[1]), reverse=True)
    return render(request, "alerts.html", {
        "subs": subs, "hikes": hikes, "history": history, "txn_count": len(txns),
        "range": (min(t.date for t in txns), max(t.date for t in txns)) if txns else None,
        "paid_extra_total": round(sum(c["paid_extra"] for _, ch in history for c in ch if c["new"] > c["old"]), 2),
    })


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- optional email recovery (Unlock holders only; OFF unless EMAIL_PROVIDER is configured) ----------
def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _send_links(to: str, tokens: list[str]):
    """Background task: mail only the recovery link(s). Errors are logged without the address."""
    subject, body = rec.message([rec.recovery_link(t) for t in tokens])
    try:
        mailer.send(to, subject, body)
    except mailer.MailError as e:
        log.warning("recovery email failed: %s", e)


def _lookup_and_send(email: str, ehash: str):
    """Background task for 'resend my link': runs after the response, so timing never reveals whether it exists."""
    with dbm.SessionLocal() as db:
        rows = db.execute(select(RecoveryEmail, Entitlement).join(Entitlement, RecoveryEmail.entitlement_id == Entitlement.id)
                          .where(RecoveryEmail.email_hash == ehash, Entitlement.status == "paid")).all()
        if not rows:
            return
        now = datetime.now(timezone.utc)
        for r, _ in rows:
            r.last_sent_at = now
        db.commit()
        tokens = [pay.make_token(e.stripe_session_id) for _, e in rows]
    _send_links(email, tokens)


@app.post("/recovery/email", dependencies=CSRF)
def recovery_email_save(request: Request, background: BackgroundTasks, email: str = Form(""), consent: str = Form(""),
                        db: Session = Depends(get_db)):
    ent = current_entitlement(request, db)
    if not ent:
        return RedirectResponse("/pricing", status_code=303)
    ctx = _unlock_ctx(request, db)
    if not mailer.enabled():
        return render(request, "pricing.html", {**ctx, "error": "Email recovery isn't available yet."}, 503)
    addr = rec.normalize_email(email)
    if not addr or consent != "yes":
        return render(request, "pricing.html", {**ctx, "error": "Enter a valid email address and tick the box to opt in."}, 400)
    if not rec.limiter.hit(f"setup:{ent.id}", *rec.SETUP_LIMIT):
        return render(request, "pricing.html", {**ctx, "error": "Too many attempts. Please try again in an hour."}, 429)
    dbm.delete_recovery_email(db, ent.id)  # one address per unlock; replacing it removes the old hash
    now = datetime.now(timezone.utc)
    db.add(RecoveryEmail(entitlement_id=ent.id, email_hash=rec.email_hash(addr), created_at=now, last_sent_at=now))
    db.commit()
    background.add_task(_send_links, addr, [pay.make_token(ent.stripe_session_id)])
    return RedirectResponse("/pricing?email_saved=1", status_code=303)


@app.post("/recovery/email/delete", dependencies=CSRF)
def recovery_email_delete(request: Request, db: Session = Depends(get_db)):
    ent = current_entitlement(request, db)
    if ent:
        dbm.delete_recovery_email(db, ent.id)
    return RedirectResponse("/pricing?email_deleted=1", status_code=303)


@app.get("/recover", response_class=HTMLResponse)
def recover_page(request: Request):
    return render(request, "recover.html", {})


@app.post("/recover", dependencies=CSRF)
def recover(request: Request, background: BackgroundTasks, email: str = Form("")):
    if not mailer.enabled():
        return render(request, "recover.html", {}, 503)
    addr = rec.normalize_email(email)
    if not addr:
        return render(request, "recover.html", {"error": "Please enter a valid email address."}, 400)
    ehash = rec.email_hash(addr)
    # Limits are keyed on the IP and on the address hash whether or not that address exists => no enumeration.
    if not rec.limiter.hit(f"ip:{_client_ip(request)}", *rec.IP_LIMIT) or not rec.limiter.hit(f"em:{ehash}", *rec.EMAIL_LIMIT):
        return render(request, "recover.html", {"error": "Too many requests. Please try again in an hour."}, 429)
    background.add_task(_lookup_and_send, addr, ehash)
    return render(request, "recover.html", {"sent": True})
