"""Unlock / Stripe: webhook signatures, idempotency, gating, checkout flow (Stripe API mocked), CSRF, restore."""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import csrf_client
from app import payments as pay

WHSEC = "whsec_test_secret"


@pytest.fixture()
def stripe_env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WHSEC)
    monkeypatch.setenv("UNLOCK_SECRET", "unit-test-unlock-secret")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    from app.main import app
    with TestClient(app) as c:
        yield csrf_client(c)


def new_client():
    from app.main import app
    return csrf_client(TestClient(app))


def session_obj(sid="cs_test_123", nonce_hash="n" * 64, paid=True, pi="pi_123", refunded=False):
    return {"id": sid, "object": "checkout.session", "mode": "payment",
            "payment_status": "paid" if paid else "unpaid", "metadata": {"product": pay.PRODUCT_TAG, "nonce_hash": nonce_hash},
            "payment_intent": {"id": pi, "latest_charge": {"refunded": refunded}} if refunded is not None else pi}


def send(client, event, secret=WHSEC, ts=None):
    body = json.dumps(event).encode()
    return client.raw_post("/stripe/webhook", content=body,
                           headers={"stripe-signature": pay.sign_payload(body, secret, ts), "content-type": "application/json"})


def completed(eid="evt_1", **kw):
    obj = session_obj(**kw)
    obj["payment_intent"] = kw.get("pi", "pi_123")
    return {"id": eid, "type": "checkout.session.completed", "data": {"object": obj}}


def entitlements():
    from app import db as dbm
    from app.db import Entitlement
    with dbm.SessionLocal() as db:
        return db.query(Entitlement).all()


# ---------- signature verification ----------
def test_verify_webhook_unit():
    body = b'{"id":"evt_x","type":"ping"}'
    hdr = pay.sign_payload(body, WHSEC)
    assert pay.verify_webhook(body, hdr, WHSEC)["id"] == "evt_x"
    with pytest.raises(pay.SignatureError):
        pay.verify_webhook(body, hdr, "whsec_other")
    with pytest.raises(pay.SignatureError):
        pay.verify_webhook(body + b" ", hdr, WHSEC)  # tampered body
    with pytest.raises(pay.SignatureError):
        pay.verify_webhook(body, pay.sign_payload(body, WHSEC, ts=int(time.time()) - 3600), WHSEC)  # replay
    with pytest.raises(pay.SignatureError):
        pay.verify_webhook(body, None, WHSEC)
    with pytest.raises(pay.SignatureError):
        pay.verify_webhook(body, "t=123", WHSEC)
    # multiple v1 signatures (secret rotation) — any valid one passes
    ts = int(time.time())
    good = pay.sign_payload(body, WHSEC, ts).split("v1=")[1]
    assert pay.verify_webhook(body, f"t={ts},v1=deadbeef,v1={good}", WHSEC)


def test_webhook_rejects_bad_signature(client, stripe_env):
    r = send(client, completed(), secret="whsec_wrong")
    assert r.status_code == 400
    r = client.raw_post("/stripe/webhook", content=b"{}")
    assert r.status_code == 400
    assert entitlements() == []


def test_webhook_without_secret_configured_rejects(client):
    assert send(client, completed()).status_code == 400


# ---------- idempotency ----------
def test_webhook_idempotent(client, stripe_env):
    assert send(client, completed("evt_1")).json() == {"received": True}
    assert send(client, completed("evt_1")).json()["duplicate"] is True  # Stripe retry, same event
    assert send(client, completed("evt_2")).status_code == 200            # different event, same session
    ents = entitlements()
    assert len(ents) == 1 and ents[0].stripe_session_id == "cs_test_123" and ents[0].status == "paid"
    assert ents[0].token_hash == pay.sha256(pay.make_token("cs_test_123"))


def test_webhook_ignores_unpaid_and_other_products(client, stripe_env):
    send(client, completed("evt_u", paid=False))
    ev = completed("evt_o", sid="cs_test_other")
    ev["data"]["object"]["metadata"]["product"] = "something_else"
    send(client, ev)
    assert entitlements() == []


def test_refund_revokes(client, stripe_env, monkeypatch):
    send(client, completed("evt_1", pi="pi_abc"))
    token = pay.make_token("cs_test_123")
    client.cookies.set("sw_unlock", token)
    client.post("/sample")
    assert client.get("/calendar.ics").headers["content-type"].startswith("text/calendar")
    send(client, {"id": "evt_r", "type": "charge.refunded", "data": {"object": {"refunded": True, "payment_intent": "pi_abc"}}})
    assert entitlements()[0].status == "refunded"
    assert client.get("/calendar.ics", follow_redirects=False).status_code == 303


# ---------- gating ----------
def test_free_features_stay_free(client):
    r = client.post("/sample", follow_redirects=True)
    assert r.status_code == 200 and "Per year" in r.text and "▲" in r.text  # price-hike badges visible free
    assert client.get("/subscription/netflix").status_code == 200
    assert client.get("/guides/netflix").status_code == 200
    alerts = client.get("/alerts").text
    assert "Latest price hikes" in alerts and "Full price-change history" in alerts and "paid more" not in alerts
    r = client.post("/reminders", data={"merchant": "Netflix", "remind_on": "2030-01-01", "note": "free reminder"}, follow_redirects=True)
    assert "free reminder" in r.text
    assert client.get("/api/subscriptions").json()["totals"]["hikes"] >= 1


def test_premium_gated(client):
    client.post("/sample")
    r = client.get("/calendar.ics", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/pricing")
    assert client.post("/history/save", follow_redirects=False).headers["location"].startswith("/pricing")
    client.cookies.set("sw_unlock", "swu1.forged.token")
    assert client.get("/calendar.ics", follow_redirects=False).status_code == 303


def test_disabled_state_without_keys(client):
    r = client.get("/pricing")
    assert "coming soon" in r.text and "Simulate unlock" not in r.text
    assert client.post("/checkout").status_code == 503
    assert client.post("/unlock/dev").status_code == 404


def test_dev_unlock(client, monkeypatch):
    monkeypatch.setenv("DEV_UNLOCK", "1")
    assert "Simulate unlock" in client.get("/pricing").text
    client.post("/sample")
    r = client.post("/unlock/dev", follow_redirects=True)
    assert "Unlocked" in r.text and "no payment" in r.text and "/restore#code=swu1." in r.text
    assert client.get("/calendar.ics").status_code == 200
    a = client.get("/alerts").text
    assert "paid <strong>" in a and "Unlock</span>" not in a


def test_dev_unlock_refused_with_live_key(client, monkeypatch):
    monkeypatch.setenv("DEV_UNLOCK", "1")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_xxx")
    assert client.post("/unlock/dev").status_code == 404


def test_live_mode_requires_support_email(monkeypatch):
    for k, v in {"STRIPE_SECRET_KEY": "sk_live_x", "STRIPE_WEBHOOK_SECRET": "w", "UNLOCK_SECRET": "u", "APP_BASE_URL": "https://x"}.items():
        monkeypatch.setenv(k, v)
    assert "SUPPORT_EMAIL" in pay.missing_config() and not pay.payments_enabled()
    monkeypatch.setenv("SUPPORT_EMAIL", "help@example.com")
    assert pay.payments_enabled() and pay.mode() == "live"


# ---------- checkout flow with mocked Stripe ----------
def test_checkout_success_flow(client, stripe_env, monkeypatch):
    captured = {}

    def fake_create(nonce_hash):
        captured["nonce_hash"] = nonce_hash
        return {"id": "cs_test_flow", "url": "https://checkout.stripe.com/c/pay/cs_test_flow"}

    monkeypatch.setattr(pay, "create_checkout_session", fake_create)
    monkeypatch.setattr(pay, "retrieve_session", lambda sid: session_obj(sid, captured["nonce_hash"], pi="pi_f"))
    assert "TEST MODE" in client.get("/pricing").text
    r = client.post("/checkout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://checkout.stripe.com/")
    # someone else with the session id (but not the buyer's browser cookie) can't claim it
    other = new_client()
    assert other.get("/checkout/success?session_id=cs_test_flow").status_code == 400
    r = client.get("/checkout/success?session_id=cs_test_flow")
    assert r.status_code == 200 and "Save your recovery link" in r.text
    token = client.cookies["sw_unlock"]
    assert pay.verify_token(token) == "cs_test_flow"
    assert client.get("/checkout/success?session_id=cs_test_flow").status_code == 200  # refresh is fine
    send(client, completed("evt_late", sid="cs_test_flow", nonce_hash=captured["nonce_hash"], pi="pi_f"))  # webhook after
    assert len(entitlements()) == 1


def test_tab_closed_webhook_then_claim(client, stripe_env, monkeypatch):
    monkeypatch.setattr(pay, "create_checkout_session",
                        lambda nh: {"id": "cs_test_tab", "url": "https://checkout.stripe.com/x", "nh": nh})
    client.post("/checkout", follow_redirects=False)
    nonce = client.cookies["sw_checkout"]
    send(client, completed("evt_t", sid="cs_test_tab", nonce_hash=pay.sha256(nonce)))
    r = client.get("/pricing", follow_redirects=False)
    assert r.status_code == 303 and "cs_test_tab" in r.headers["location"]
    assert "Save your recovery link" in client.get(r.headers["location"]).text


def test_db_wipe_reverifies_with_stripe(client, stripe_env, monkeypatch):
    """Free hosting can wipe SQLite; a valid signed code is re-checked with Stripe and still works."""
    monkeypatch.setattr(pay, "retrieve_session", lambda sid: session_obj(sid, refunded=False))
    client.cookies.set("sw_unlock", pay.make_token("cs_test_wiped"))
    client.post("/sample")
    assert client.get("/calendar.ics").status_code == 200
    assert entitlements()[0].stripe_session_id == "cs_test_wiped"
    monkeypatch.setattr(pay, "retrieve_session", lambda sid: session_obj(sid, refunded=True))
    c2 = new_client()
    c2.cookies.set("sw_unlock", pay.make_token("cs_test_refunded"))
    c2.post("/sample")
    assert c2.get("/calendar.ics", follow_redirects=False).status_code == 303


# ---------- restore + saved history ----------
def test_saved_history_and_restore(client, monkeypatch):
    monkeypatch.setenv("DEV_UNLOCK", "1")
    client.post("/sample")
    client.post("/unlock/dev")
    token = client.cookies["sw_unlock"]
    assert client.post("/history/save", follow_redirects=False).headers["location"] == "/pricing?saved=1"
    count = client.get("/api/subscriptions").json()["totals"]["count"]
    other = new_client()  # new device: empty
    assert other.get("/api/subscriptions").json()["totals"]["count"] == 0
    assert other.post("/restore", data={"code": "swu1.bad.code"}).status_code == 400
    r = other.post("/restore", data={"code": token}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard"
    assert other.get("/api/subscriptions").json()["totals"]["count"] == count
    # saved sessions survive the normal retention purge
    from app import db as dbm
    from app.db import UserSession
    from datetime import datetime, timedelta, timezone
    with dbm.SessionLocal() as db:
        for s in db.query(UserSession):
            s.last_seen = datetime.now(timezone.utc) - timedelta(days=30)
        db.commit()
        dbm.purge_expired(db)
        assert db.query(UserSession).count() == 1
    # stop saving → back to normal retention; delete-my-data unlinks it
    client.post("/history/stop")
    client.post("/delete-my-data")
    assert entitlements()[0].saved_session_id is None
    assert client.get("/calendar.ics").status_code == 200  # purchase itself remains


def test_saved_history_unavailable_without_persistent_storage(client, monkeypatch):
    monkeypatch.setenv("DEV_UNLOCK", "1")
    monkeypatch.setenv("PERSISTENT_STORAGE", "0")
    client.post("/unlock/dev")
    page = client.get("/pricing").text
    assert "coming soon — needs permanent storage" in page and "Save my history" not in page
    assert "Free beta hosting" in page
    client.post("/history/save")
    assert entitlements()[0].saved_session_id is None


# ---------- CSRF ----------
def test_csrf_required_on_forms(client):
    assert client.post("/sample", csrf=False).status_code == 403
    client.get("/")
    assert client.raw_post("/sample", data={"csrf_token": "wrong-token-value-xxxxxxxx"}).status_code == 403
    assert client.post("/sample", follow_redirects=False).status_code == 303
    assert 'name="csrf_token"' in client.get("/").text


# ---------- regressions found in the live Stripe test-mode run ----------
def _capture_post(monkeypatch):
    sent = {}

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, path, data=None, headers=None):
            sent.update(path=path, data=data, headers=headers)
            return httpx.Response(200, json={"id": "cs_test_x", "url": "https://checkout.stripe.com/c/pay/cs_test_x"})
    monkeypatch.setattr(pay, "_client", lambda: FakeClient())
    return sent


def test_checkout_session_params_include_tax_code(stripe_env, monkeypatch):
    """Accounts with Managed Payments / Stripe Tax reject inline prices without a product tax code."""
    sent = _capture_post(monkeypatch)
    pay.create_checkout_session("h" * 64)
    d = sent["data"]
    assert d["mode"] == "payment" and d["line_items[0][price_data][currency]"] == "usd"
    assert d["line_items[0][price_data][unit_amount]"] == "700"
    assert d["line_items[0][price_data][product_data][tax_code]"] == "txcd_10103000"
    monkeypatch.setenv("STRIPE_TAX_CODE", "txcd_10000000")
    pay.create_checkout_session("i" * 64)
    assert sent["data"]["line_items[0][price_data][product_data][tax_code]"] == "txcd_10000000"


def test_stripe_error_carries_status():
    e = pay._err(httpx.Response(404, json={"error": {"message": "No such checkout.session", "code": "resource_missing"}}))
    assert isinstance(e, pay.StripeError) and e.status == 404 and e.code == "resource_missing"
    assert pay._err(httpx.Response(500, text="oops")).status == 500


def test_success_unknown_session_is_400_not_pending(client, stripe_env, monkeypatch):
    def missing(sid):
        raise pay.StripeError("No such checkout.session", 404, "resource_missing")
    monkeypatch.setattr(pay, "retrieve_session", missing)
    client.cookies.set("sw_checkout", "some-nonce")
    assert client.get("/checkout/success?session_id=cs_test_nope").status_code == 400

    def down(sid):
        raise pay.StripeError("server error", 500)
    monkeypatch.setattr(pay, "retrieve_session", down)
    assert client.get("/checkout/success?session_id=cs_test_nope").status_code == 502
