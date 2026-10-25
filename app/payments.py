"""One-time "Unlock" via Stripe Checkout — no Stripe SDK, just the REST API over httpx.

Everything is configured with env vars and read at call time:
  STRIPE_SECRET_KEY      sk_test_… / sk_live_…           (secret)
  STRIPE_WEBHOOK_SECRET  whsec_…                         (secret)
  UNLOCK_SECRET          random string, signs unlock codes (secret; keep stable forever)
  APP_BASE_URL           https://your-app.onrender.com   (no trailing slash)
  STRIPE_PRICE_ID        optional price_… ; otherwise inline price_data of UNLOCK_PRICE_CENTS
  UNLOCK_PRICE_CENTS     default 700 ($7.00) — also used for display, keep in sync with STRIPE_PRICE_ID
  DEV_UNLOCK             "1" = show a "simulate unlock" button (local testing only; refused with live keys)

We never receive or store card details: the buyer types them on Stripe's hosted page.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

import httpx

STRIPE_API = "https://api.stripe.com/v1"
PRODUCT_TAG = "unlock_v1"
TOKEN_PREFIX = "swu1"
SIG_TOLERANCE = 300  # seconds, same as Stripe's libraries
_DEV_SECRET = secrets.token_urlsafe(32)  # only used for DEV_UNLOCK when UNLOCK_SECRET is unset


class SignatureError(Exception):
    pass


class StripeError(Exception):
    pass


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def price_cents() -> int:
    try:
        return int(env("UNLOCK_PRICE_CENTS", "700"))
    except ValueError:
        return 700


def price_display() -> str:
    c = price_cents()
    return f"${c // 100}" if c % 100 == 0 else f"${c / 100:.2f}"


def missing_config() -> list[str]:
    missing = [k for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "UNLOCK_SECRET", "APP_BASE_URL") if not env(k)]
    if live_mode() and not env("SUPPORT_EMAIL"):
        missing.append("SUPPORT_EMAIL")  # refunds are "by request" — real buyers need somewhere to ask
    return missing


def payments_enabled() -> bool:
    return not missing_config()


def live_mode() -> bool:
    return env("STRIPE_SECRET_KEY").startswith(("sk_live_", "rk_live_"))


def dev_unlock_enabled() -> bool:
    # Never allow free unlocks on a deployment that takes real money.
    return env("DEV_UNLOCK") == "1" and not live_mode()


def mode() -> str:
    if payments_enabled():
        return "live" if live_mode() else "test"
    return "dev" if dev_unlock_enabled() else "disabled"


# ---------- unlock tokens ----------
# The token is HMAC-signed over the Stripe Checkout Session id, so a valid code can be re-verified even if
# the SQLite file is wiped (free hosting): we just re-check the session with Stripe. The DB keeps only a hash.
def _secret() -> bytes:
    s = env("UNLOCK_SECRET")
    if s:
        return s.encode()
    if dev_unlock_enabled():
        return _DEV_SECRET.encode()
    raise RuntimeError("UNLOCK_SECRET is not set")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(session_id: str) -> str:
    sig = hmac.new(_secret(), f"{TOKEN_PREFIX}.{session_id}".encode(), hashlib.sha256).digest()[:18]
    return f"{TOKEN_PREFIX}.{_b64(session_id.encode())}.{_b64(sig)}"


def verify_token(token: str | None) -> str | None:
    """Return the Checkout Session id the token was issued for, or None."""
    if not token or len(token) > 512:
        return None
    try:
        prefix, sid_b64, sig_b64 = token.strip().split(".")
        if prefix != TOKEN_PREFIX:
            return None
        session_id = _unb64(sid_b64).decode()
        expected = hmac.new(_secret(), f"{TOKEN_PREFIX}.{session_id}".encode(), hashlib.sha256).digest()[:18]
        if hmac.compare_digest(expected, _unb64(sig_b64)):
            return session_id
    except (ValueError, UnicodeDecodeError, RuntimeError):
        return None
    return None


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---------- webhook signatures ----------
def verify_webhook(payload: bytes, sig_header: str | None, secret: str | None = None,
                   tolerance: int = SIG_TOLERANCE, now: float | None = None) -> dict:
    """Verify a Stripe-Signature header (t=…,v1=…) exactly like Stripe's SDKs, then parse the event."""
    secret = secret if secret is not None else env("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise SignatureError("webhook secret not configured")
    if not sig_header:
        raise SignatureError("missing Stripe-Signature header")
    parts = [p.split("=", 1) for p in sig_header.split(",") if "=" in p]
    try:
        ts = int(next(v for k, v in parts if k.strip() == "t"))
    except (StopIteration, ValueError):
        raise SignatureError("no timestamp in signature header")
    sigs = [v.strip() for k, v in parts if k.strip() == "v1"]
    if not sigs:
        raise SignatureError("no v1 signature")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise SignatureError("signature mismatch")
    if abs((now or time.time()) - ts) > tolerance:
        raise SignatureError("timestamp outside tolerance")
    try:
        return json.loads(payload)
    except ValueError:
        raise SignatureError("invalid JSON")


def sign_payload(payload: bytes, secret: str, ts: int | None = None) -> str:
    """Build a Stripe-Signature header (used by tests and the local webhook simulator)."""
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


# ---------- Stripe REST calls ----------
def _client() -> httpx.Client:
    return httpx.Client(base_url=STRIPE_API, auth=(env("STRIPE_SECRET_KEY"), ""), timeout=20)


def create_checkout_session(nonce_hash: str) -> dict:
    base = env("APP_BASE_URL").rstrip("/")
    data = {
        "mode": "payment",
        "success_url": f"{base}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/pricing?canceled=1",
        "line_items[0][quantity]": "1",
        "metadata[product]": PRODUCT_TAG,
        "metadata[nonce_hash]": nonce_hash,
        "payment_intent_data[metadata][product]": PRODUCT_TAG,
        "payment_intent_data[description]": "Subscription Watchdog Unlock (one-time)",
    }
    if env("STRIPE_PRICE_ID"):
        data["line_items[0][price]"] = env("STRIPE_PRICE_ID")
    else:
        data.update({
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": str(price_cents()),
            "line_items[0][price_data][product_data][name]": "Subscription Watchdog Unlock",
            "line_items[0][price_data][product_data][description]":
                "One-time unlock: saved history, price-hike history, calendar (.ics) reminders.",
        })
    with _client() as c:
        r = c.post("/checkout/sessions", data=data, headers={"Idempotency-Key": f"checkout-{nonce_hash}"})
    if r.status_code >= 400:
        raise StripeError(_err(r))
    return r.json()


def retrieve_session(session_id: str) -> dict:
    with _client() as c:
        r = c.get(f"/checkout/sessions/{session_id}", params={"expand[]": "payment_intent.latest_charge"})
    if r.status_code >= 400:
        raise StripeError(_err(r))
    return r.json()


def _err(r: httpx.Response) -> str:
    try:
        return r.json()["error"].get("message", r.text)[:300]
    except Exception:
        return f"HTTP {r.status_code}"


def session_is_paid_unlock(sess: dict) -> bool:
    return (sess.get("object") == "checkout.session" and sess.get("mode") == "payment"
            and sess.get("payment_status") == "paid"
            and (sess.get("metadata") or {}).get("product") == PRODUCT_TAG)


def session_refunded(sess: dict) -> bool:
    pi = sess.get("payment_intent")
    charge = pi.get("latest_charge") if isinstance(pi, dict) else None
    return bool(isinstance(charge, dict) and charge.get("refunded"))


def payment_intent_id(sess: dict) -> str | None:
    pi = sess.get("payment_intent")
    return pi.get("id") if isinstance(pi, dict) else pi
