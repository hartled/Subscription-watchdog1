"""Minimal RFC 5545 calendar generation (all-day events)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    out, b = [], line.encode()
    while len(b) > 75:
        cut = 75
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 sequence
            cut -= 1
        out.append(b[:cut].decode())
        b = b" " + b[cut:]
    out.append(b.decode())
    return "\r\n".join(out)


def build_ics(events: list[dict]) -> str:
    """events: {uid, date, summary, description, alarm_days(optional)}"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Subscription Watchdog//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Subscription Watchdog"]
    for e in events:
        d: date = e["date"]
        lines += ["BEGIN:VEVENT", f"UID:{e['uid']}@subscription-watchdog", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
                  f"SUMMARY:{_esc(e['summary'])}", f"DESCRIPTION:{_esc(e.get('description', ''))}", "TRANSP:TRANSPARENT"]
        if e.get("alarm_days") is not None:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_esc(e['summary'])}",
                      f"TRIGGER:-P{int(e['alarm_days'])}D", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"
