import pytest
from fastapi.testclient import TestClient

from conftest import csrf_client


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    from app.main import app
    with TestClient(app) as c:
        yield csrf_client(c)


def test_full_flow(client, monkeypatch):
    assert client.get("/healthz").json() == {"ok": True}
    assert "No bank login" in client.get("/").text
    r = client.post("/sample", follow_redirects=True)
    assert r.status_code == 200 and "Netflix" in r.text and "Per year" in r.text
    api = client.get("/api/subscriptions").json()
    assert api["totals"]["count"] == 10
    r = client.get("/subscription/netflix")
    assert "netflix.com/cancelplan" in r.text
    r = client.get("/subscription/patreon-membership")
    assert "general" in r.text.lower() or "most" in r.text
    r = client.post("/reminders", data={"merchant": "Netflix", "remind_on": "2030-01-01", "note": "decide"}, follow_redirects=True)
    assert "decide" in r.text
    assert client.get("/calendar.ics", follow_redirects=False).status_code == 303  # .ics is a premium extra
    monkeypatch.setenv("DEV_UNLOCK", "1")
    client.post("/unlock/dev")
    ics = client.get("/calendar.ics")
    assert ics.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VCALENDAR" in ics.text and "Review: Netflix" in ics.text and "Netflix renews" in ics.text
    r = client.post("/delete-my-data", follow_redirects=True)
    assert "deleted" in r.text
    assert client.get("/api/subscriptions").json()["totals"]["count"] == 0


def test_upload_and_paste_and_errors(client):
    csv = b"Date,Description,Amount\n" + b"".join(
        f"2026-0{m}-03,NETFLIX.COM 4111111111111111,15.49\n".encode() for m in range(1, 7))
    r = client.post("/upload", files={"file": ("s.csv", csv, "text/csv")}, follow_redirects=True)
    assert r.status_code == 200 and "Netflix" in r.text
    assert "4111111111111111" not in r.text  # masked before storage
    r = client.post("/upload", files={"file": ("s.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 400
    r = client.post("/paste", data={"text": "01/12 SPOTIFY USA 11.99\n02/12 SPOTIFY USA 11.99\n03/12 SPOTIFY USA 11.99"}, follow_redirects=True)
    assert r.status_code == 200
    assert client.post("/paste", data={"text": "nothing here"}).status_code == 400


def test_guides_and_privacy(client):
    r = client.get("/guides")
    assert r.status_code == 200 and r.text.count('class="card guide-tile') >= 25
    assert client.get("/guides/spotify").status_code == 200
    assert client.get("/guides/generic").status_code == 200
    assert client.get("/guides/nope").status_code == 404
    assert "never ask for" in client.get("/privacy").text


def test_sessions_are_isolated(client, tmp_path):
    from app.main import app
    client.post("/sample")
    with TestClient(app) as other:
        csrf_client(other)
        assert other.get("/api/subscriptions").json()["totals"]["count"] == 0
