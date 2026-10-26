"""Optional email recovery for Unlock holders: keyed email hashing, validation, rate limiting, message text."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time
from collections import defaultdict, deque

from . import payments as pay

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def normalize_email(raw: str | None) -> str | None:
    e = (raw or "").strip().lower()
    if not e or len(e) > 254 or not EMAIL_RE.match(e):
        return None
    return e


def _key() -> bytes:
    # Separate key if set; otherwise derived from UNLOCK_SECRET (which must stay stable anyway).
    k = os.getenv("EMAIL_HASH_KEY", "").strip()
    if k:
        return k.encode()
    return hmac.new(pay._secret(), b"recovery-email-hash-v1", hashlib.sha256).digest()


def email_hash(email: str) -> str:
    return hmac.new(_key(), email.encode(), hashlib.sha256).hexdigest()


def recovery_link(token: str) -> str:
    return f"{pay.env('APP_BASE_URL').rstrip('/')}/restore#code={token}"


def message(links: list[str]) -> tuple[str, str]:
    subject = "Your Subscription Watchdog recovery link"
    lines = ["Here is your Subscription Watchdog recovery link. Open it to restore your Unlock on any browser:", ""]
    lines += links
    lines += ["", "Keep it private: anyone with this link can use your Unlock.",
              "You received this because someone asked for it on our website. If that wasn't you, you can ignore it."]
    return subject, "\n".join(lines)


class RateLimiter:
    """In-memory sliding-window limiter (one Render instance; resets on restart, which is fine for abuse control)."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float, now: float | None = None) -> bool:
        """Record an attempt; return False if the key is over its limit."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 50_000:  # crude memory cap
                self._hits.clear()
            return True

    def reset(self):
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()
IP_LIMIT = (int(os.getenv("RECOVERY_IP_LIMIT", "5")), 3600)      # resend requests per IP per hour
EMAIL_LIMIT = (int(os.getenv("RECOVERY_EMAIL_LIMIT", "3")), 3600)  # resend requests per address per hour
SETUP_LIMIT = (5, 3600)                                             # opt-in saves per unlock per hour
