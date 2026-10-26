"""Optional email recovery: off by default, opt-in storage (hash only), resend without enumeration, rate limits,
deletion paths, provider plumbing. Every send is mocked — nothing leaves the machine."""
import pytest
from fastapi.testclient import TestClient

from conftest import csrf_client
from app import mailer, recovery as rec


@pytest.fixture()
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append((to, subject, body)))
    return sent


@pytest.fixture()
def email_env(monkeypatch):
    monkeypatch.setenv("DEV_UNLOCK", "1")
    monkeypatch.setenv("UNLOCK_SECRET", "unit-test-unlock-secret")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("EMAIL_FROM", "Watchdog <recovery@example.test>")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    from app.main import app
    with TestClient(app) as c:
        yield csrf_client(c)


def unlock(c):
    r = c.post("/unlock/dev", follow_redirects=False)
    assert r.status_code == 303
    return c.cookies["sw_unlock"]


def stored():
    from app import db as dbm
    from app.db import RecoveryEmail
    with dbm.SessionLocal() as db:
        return db.query(RecoveryEmail).all()


# ---------- off by default ----------
def test_disabled_by_default(client, monkeypatch, outbox):
    monkeypatch.setenv("DEV_UNLOCK", "1")
    assert mailer.provider() == "none" and not mailer.enabled()
    unlock(client)
    page = client.get("/pricing").text
    assert "not available yet" in page and 'action="/recovery/email"' not in page
    assert "/recover" not in client.get("/restore").text
    r = client.post("/recovery/email", data={"email": "a@b.co", "consent": "yes"})
    assert r.status_code == 503 and not stored()
    assert client.post("/recover", data={"email": "a@b.co"}).status_code == 503
    assert outbox == []


def test_console_provider_refused_with_live_keys(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "console")
    monkeypatch.setenv("APP_BASE_URL", "http://x")
    assert mailer.enabled()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    assert not mailer.enabled()


def test_missing_config(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    assert set(mailer.missing_config()) == {"APP_BASE_URL", "EMAIL_FROM", "RESEND_API_KEY"}
    monkeypatch.setenv("EMAIL_PROVIDER", "bogus")
    assert mailer.provider() == "none"


# ---------- opt in ----------
def test_opt_in_stores_only_hash_and_sends_link(client, email_env, outbox):
    token = unlock(client)
    assert 'action="/recovery/email"' in client.get("/pricing").text
    r = client.post("/recovery/email", data={"email": "  Buyer@Example.COM ", "consent": "yes"}, follow_redirects=False)
    assert r.status_code == 303 and "email_saved" in r.headers["location"]
    rows = stored()
    assert len(rows) == 1 and rows[0].email_hash == rec.email_hash("buyer@example.com")
    # the address itself is nowhere in the database file
    from app import db as dbm
    assert b"buyer@example.com" not in dbm.database_path().read_bytes().lower()
    assert len(outbox) == 1
    to, subject, body = outbox[0]
    assert to == "buyer@example.com" and f"http://testserver/restore#code={token}" in body
    assert "4242" not in body and "card" not in body.lower()
    assert "An email is on file" in client.get("/pricing").text


def test_opt_in_requires_consent_valid_email_and_unlock(client, email_env, outbox):
    assert client.post("/recovery/email", data={"email": "a@b.co", "consent": "yes"}, follow_redirects=False).status_code == 303
    assert not stored()  # not unlocked => redirected, nothing stored
    unlock(client)
    assert client.post("/recovery/email", data={"email": "a@b.co"}).status_code == 400
    assert client.post("/recovery/email", data={"email": "not-an-email", "consent": "yes"}).status_code == 400
    assert client.post("/recovery/email", data={"email": "a@b.co", "consent": "yes"}, csrf=False).status_code == 403
    assert not stored() and outbox == []


def test_replacing_email_keeps_one_row(client, email_env, outbox):
    unlock(client)
    client.post("/recovery/email", data={"email": "one@example.com", "consent": "yes"})
    client.post("/recovery/email", data={"email": "two@example.com", "consent": "yes"})
    rows = stored()
    assert len(rows) == 1 and rows[0].email_hash == rec.email_hash("two@example.com")


# ---------- resend ----------
def test_resend_same_response_whether_or_not_email_exists(client, email_env, outbox):
    token = unlock(client)
    client.post("/recovery/email", data={"email": "buyer@example.com", "consent": "yes"})
    outbox.clear()
    other = csrf_client(TestClient(client.app))
    a = other.post("/recover", data={"email": "BUYER@example.com"})
    b = other.post("/recover", data={"email": "nobody@example.com"})
    assert a.status_code == b.status_code == 200
    strip = lambda t: t.replace(other.cookies.get("sw_csrf", ""), "")
    assert strip(a.text) == strip(b.text)
    assert [m[0] for m in outbox] == ["buyer@example.com"] and token in outbox[0][2]


def test_resend_skips_refunded(client, email_env, outbox):
    unlock(client)
    client.post("/recovery/email", data={"email": "buyer@example.com", "consent": "yes"})
    from app import db as dbm
    from app.db import Entitlement
    with dbm.SessionLocal() as db:
        for e in db.query(Entitlement):
            e.status = "refunded"
        db.commit()
    outbox.clear()
    assert client.post("/recover", data={"email": "buyer@example.com"}).status_code == 200
    assert outbox == []


def test_resend_rate_limited_per_email_and_ip(client, email_env, outbox):
    for _ in range(3):
        assert client.post("/recover", data={"email": "x@example.com"}).status_code == 200
    assert client.post("/recover", data={"email": "x@example.com"}).status_code == 429  # per-address cap
    assert client.post("/recover", data={"email": "y@example.com"}).status_code == 200
    assert client.post("/recover", data={"email": "z@example.com"}).status_code == 429  # per-IP cap (5/h)


def test_rate_limiter_window():
    lim = rec.RateLimiter()
    assert all(lim.hit("k", 2, 60, now=t) for t in (0, 1))
    assert not lim.hit("k", 2, 60, now=2)
    assert lim.hit("k", 2, 60, now=61)


def test_recover_rejects_bad_input_and_csrf(client, email_env, outbox):
    assert client.post("/recover", data={"email": "nope"}).status_code == 400
    assert client.post("/recover", data={"email": "a@b.co"}, csrf=False).status_code == 403
    assert outbox == []


# ---------- deletion ----------
def test_delete_my_recovery_email(client, email_env, outbox):
    unlock(client)
    client.post("/recovery/email", data={"email": "buyer@example.com", "consent": "yes"})
    assert stored()
    r = client.post("/recovery/email/delete", follow_redirects=False)
    assert r.status_code == 303 and not stored()
    outbox.clear()
    client.post("/recover", data={"email": "buyer@example.com"})
    assert outbox == []


def test_delete_my_data_removes_recovery_email(client, email_env, outbox):
    unlock(client)
    client.post("/recovery/email", data={"email": "buyer@example.com", "consent": "yes"})
    client.post("/sample")
    client.post("/delete-my-data")
    assert not stored()


def test_refund_webhook_removes_recovery_email(client, email_env, outbox, monkeypatch):
    import json
    from app import payments as pay
    from app.main import upsert_entitlement
    from app import db as dbm
    from app.db import RecoveryEmail
    from datetime import datetime, timezone
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_t")
    with dbm.SessionLocal() as db:
        ent = upsert_entitlement(db, "cs_test_r", payment_intent="pi_r")
        db.add(RecoveryEmail(entitlement_id=ent.id, email_hash=rec.email_hash("b@example.com"),
                             created_at=datetime.now(timezone.utc)))
        db.commit()
    body = json.dumps({"id": "evt_r", "type": "charge.refunded",
                       "data": {"object": {"refunded": True, "payment_intent": "pi_r"}}}).encode()
    r = client.raw_post("/stripe/webhook", content=body, headers={"stripe-signature": pay.sign_payload(body, "whsec_t")})
    assert r.status_code == 200 and not stored()


# ---------- provider plumbing (all mocked) ----------
def test_smtp_send_uses_starttls_and_login(monkeypatch, email_env):
    import smtplib
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self, context=None): calls.append(("starttls",))
        def login(self, u, p): calls.append(("login", u))
        def send_message(self, msg): calls.append(("send", msg["To"], msg.get_content()))
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    mailer.send("to@example.com", "s", "http://testserver/restore#code=abc")
    assert calls[0] == ("connect", "smtp.example.test", 587) and ("starttls",) in calls and ("login", "u") in calls
    assert calls[-1][1] == "to@example.com" and "restore#code=abc" in calls[-1][2]


def test_resend_and_postmark_http(monkeypatch, email_env):
    import httpx
    seen = []

    def fake_post(url, **kw):
        seen.append((url, kw.get("headers"), kw.get("json")))
        return httpx.Response(200, json={})
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    mailer.send("a@example.com", "s", "b")
    monkeypatch.setenv("EMAIL_PROVIDER", "postmark")
    monkeypatch.setenv("POSTMARK_TOKEN", "pm_test")
    mailer.send("a@example.com", "s", "b")
    assert seen[0][0] == "https://api.resend.com/emails" and seen[0][1]["Authorization"] == "Bearer re_test"
    assert seen[1][0] == "https://api.postmarkapp.com/email" and seen[1][1]["X-Postmark-Server-Token"] == "pm_test"
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(500))
    with pytest.raises(mailer.MailError):
        mailer.send("a@example.com", "s", "b")
