"""Pluggable, OFF-by-default email sending — used only for optional unlock-recovery emails.

  EMAIL_PROVIDER   none (default) | smtp | resend | postmark | console
                   console = log the message instead of sending (local testing only; refused with live Stripe keys)
  EMAIL_FROM       e.g. "Subscription Watchdog <recovery@yourdomain.com>"   (required for every real provider)
  SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_SECURITY starttls (default) | ssl | none
  RESEND_API_KEY   for EMAIL_PROVIDER=resend    (https://resend.com — API key)
  POSTMARK_TOKEN   for EMAIL_PROVIDER=postmark  (Postmark server token)

Nothing here is ever called unless the buyer explicitly asks for an email. Messages contain only the recovery link.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

import httpx

log = logging.getLogger("watchdog.mail")
PROVIDERS = ("none", "smtp", "resend", "postmark", "console")


class MailError(Exception):
    pass


def _env(k: str, d: str = "") -> str:
    return os.getenv(k, d).strip()


def provider() -> str:
    p = _env("EMAIL_PROVIDER", "none").lower() or "none"
    return p if p in PROVIDERS else "none"


def missing_config() -> list[str]:
    p = provider()
    if p == "none":
        return ["EMAIL_PROVIDER"]
    need = ["APP_BASE_URL"]  # links in the email must be absolute
    if p != "console":
        need.append("EMAIL_FROM")
    need += {"smtp": ["SMTP_HOST"], "resend": ["RESEND_API_KEY"], "postmark": ["POSTMARK_TOKEN"]}.get(p, [])
    return [k for k in need if not _env(k)]


def _live_payments() -> bool:
    return _env("STRIPE_SECRET_KEY").startswith(("sk_live_", "rk_live_"))


def enabled() -> bool:
    if provider() == "console" and _live_payments():
        return False  # never print real customers' recovery links to server logs
    return not missing_config()


def send(to: str, subject: str, body: str) -> None:
    """Send a plain-text email. Raises MailError on failure."""
    if not enabled():
        raise MailError("email is not configured")
    p, sender = provider(), _env("EMAIL_FROM", "Subscription Watchdog <no-reply@localhost>")
    try:
        if p == "console":
            log.warning("EMAIL_PROVIDER=console — not sent. To: %s | Subject: %s\n%s", to, subject, body)
        elif p == "smtp":
            _send_smtp(sender, to, subject, body)
        elif p == "resend":
            r = httpx.post("https://api.resend.com/emails", timeout=15,
                           headers={"Authorization": f"Bearer {_env('RESEND_API_KEY')}"},
                           json={"from": sender, "to": [to], "subject": subject, "text": body})
            if r.status_code >= 400:
                raise MailError(f"resend HTTP {r.status_code}")
        elif p == "postmark":
            r = httpx.post("https://api.postmarkapp.com/email", timeout=15,
                           headers={"X-Postmark-Server-Token": _env("POSTMARK_TOKEN"), "Accept": "application/json"},
                           json={"From": sender, "To": to, "Subject": subject, "TextBody": body,
                                 "MessageStream": _env("POSTMARK_STREAM", "outbound")})
            if r.status_code >= 400:
                raise MailError(f"postmark HTTP {r.status_code}")
    except (OSError, smtplib.SMTPException, httpx.HTTPError) as e:
        raise MailError(f"{p} send failed: {type(e).__name__}") from e


def _send_smtp(sender: str, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg["Message-ID"] = make_msgid(domain=(sender.rsplit("@", 1)[-1].strip("> ") or None))
    msg.set_content(body)
    host, sec = _env("SMTP_HOST"), _env("SMTP_SECURITY", "starttls").lower()
    port = int(_env("SMTP_PORT", "465" if sec == "ssl" else "587"))
    ctx = ssl.create_default_context()
    if sec == "ssl":
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=15)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
    with server:
        if sec == "starttls":
            server.starttls(context=ctx)
        if _env("SMTP_USER"):
            server.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
        server.send_message(msg)
