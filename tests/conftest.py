import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

PAY_VARS = ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "UNLOCK_SECRET", "APP_BASE_URL", "STRIPE_PRICE_ID",
            "DEV_UNLOCK", "SUPPORT_EMAIL", "UNLOCK_PRICE_CENTS", "PERSISTENT_STORAGE")


def csrf_client(c):
    """Wrap TestClient.post so forms carry the CSRF token, like a real browser that loaded the page first."""
    from app.main import CSRF_COOKIE
    raw_post = c.post

    def post(url, data=None, csrf=True, **kw):
        if csrf:
            if CSRF_COOKIE not in c.cookies:
                c.get("/healthz")
            data = {**(data or {}), "csrf_token": c.cookies[CSRF_COOKIE]}
        return raw_post(url, data=data, **kw)

    c.post = post
    c.raw_post = raw_post
    return c


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in PAY_VARS:
        monkeypatch.delenv(k, raising=False)
