"""Parse bank/card CSV exports and pasted statement text into transactions.

Convention: returned Txn.amount > 0 means money spent (outflow); < 0 is a credit/refund/deposit.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

DATE_FORMATS = [
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y", "%m-%d-%y", "%d %b %Y",
    "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%Y%m%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
]

DATE_COLS = ["transaction date", "trans. date", "trans date", "date", "posted date", "posting date",
             "post date", "booking date", "value date", "settlement date"]
DESC_COLS = ["description", "merchant", "merchant name", "payee", "name", "transaction description",
             "details", "narrative", "memo", "original description"]
AMOUNT_COLS = ["amount", "transaction amount", "amount (usd)", "amount usd", "value", "net amount"]
DEBIT_COLS = ["debit", "debits", "withdrawal", "withdrawals", "amount debit", "debit amount", "money out", "paid out", "outflow", "charges"]
CREDIT_COLS = ["credit", "credits", "deposit", "deposits", "amount credit", "credit amount", "money in", "paid in", "inflow", "payments"]
TYPE_COLS = ["type", "transaction type", "credit/debit", "debit/credit", "cr/dr", "dr/cr", "indicator"]

MAX_ROWS = 20000


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Txn:
    date: date
    description: str
    amount: float  # > 0 = spend


def parse_date(value: str) -> date | None:
    v = (value or "").strip().strip('"')
    if not v:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


_AMT_RE = re.compile(r"^\(?[-+]?\s*[$€£]?\s*[-+]?\d[\d,]*(\.\d+)?\)?\s*(CR|DR|-)?$", re.I)


def parse_amount(value: str) -> float | None:
    v = (value or "").strip().replace("\u2212", "-")
    if not v or not _AMT_RE.match(v):
        return None
    neg = False
    if v.startswith("(") and v.endswith(")"):
        neg, v = True, v[1:-1]
    up = v.upper()
    if up.endswith("CR"):
        neg, v = not neg, v[:-2]
    elif up.endswith("DR"):
        v = v[:-2]
    elif v.endswith("-"):
        neg, v = not neg, v[:-1]
    v = v.replace("$", "").replace("€", "").replace("£", "").replace(",", "").replace(" ", "")
    if v.startswith("+"):
        v = v[1:]
    if v.startswith("-"):
        neg, v = not neg, v[1:]
    try:
        n = float(v)
    except ValueError:
        return None
    return -n if neg else n


def _find(headers: list[str], candidates: list[str]) -> int | None:
    norm = [h.strip().lower().strip('"').replace("_", " ") for h in headers]
    for c in candidates:
        if c in norm:
            return norm.index(c)
    for c in candidates:  # loose contains-match
        for i, h in enumerate(norm):
            if c in h:
                return i
    return None


def _orient(rows: list[tuple[date, str, float]], hint_spend_negative: bool | None) -> list[Txn]:
    """Flip signs so spend is positive. Decide by hint or by majority sign (most rows are purchases)."""
    if hint_spend_negative is None:
        neg = sum(1 for _, _, a in rows if a < 0)
        pos = sum(1 for _, _, a in rows if a > 0)
        hint_spend_negative = neg > pos
    sign = -1 if hint_spend_negative else 1
    return [Txn(d, desc, round(sign * a, 2)) for d, desc, a in rows if a != 0]


def _guess_delimiter(text: str) -> str:
    """Pick the delimiter that splits the most lines into the same (>1) number of fields."""
    lines = [l for l in text.splitlines()[:50] if l.strip()]
    best, best_score = ",", -1
    for d in [",", ";", "\t", "|"]:
        counts = [len(next(csv.reader([l], delimiter=d))) for l in lines]
        counts = [c for c in counts if c > 1]
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        score = counts.count(mode) * 100 + mode
        if score > best_score:
            best, best_score = d, score
    return best


def parse_csv(text: str) -> list[Txn]:
    text = text.lstrip("\ufeff")
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=_guess_delimiter(text)) if any(c.strip() for c in r)]
    if not rows:
        raise ParseError("The file is empty.")
    if len(rows) > MAX_ROWS:
        raise ParseError(f"Too many rows (max {MAX_ROWS}).")

    # Some banks put preamble lines before the header: find the first row with a date-like header.
    header_idx = None
    for i, r in enumerate(rows[:15]):
        if _find(r, DATE_COLS) is not None and (_find(r, AMOUNT_COLS + DEBIT_COLS + CREDIT_COLS) is not None):
            header_idx = i
            break
    if header_idx is None:
        return _parse_headerless(rows)

    headers = rows[header_idx]
    body = rows[header_idx + 1:]
    di = _find(headers, DATE_COLS)
    de = _find(headers, DESC_COLS)
    ai = _find(headers, AMOUNT_COLS)
    dbi = _find(headers, DEBIT_COLS)
    cri = _find(headers, CREDIT_COLS)
    ti = _find(headers, TYPE_COLS)
    if ai is not None and ai in (dbi, cri):
        # "Amount Debit"-style names matched both; prefer split columns when both exist.
        if dbi is not None and cri is not None and dbi != cri:
            ai = None
    if de is None:
        used = {di, ai, dbi, cri, ti}
        de = next((i for i in range(len(headers)) if i not in used), None)
    if di is None or de is None or (ai is None and dbi is None and cri is None):
        raise ParseError("Could not find date, description and amount columns.")

    out: list[tuple[date, str, float]] = []
    split = ai is None
    type_hint_used = False
    for r in body:
        def cell(i):
            return r[i] if i is not None and i < len(r) else ""
        d = parse_date(cell(di))
        if d is None:
            continue
        desc = cell(de).strip()
        if split:
            deb = parse_amount(cell(dbi)) if dbi is not None else None
            cre = parse_amount(cell(cri)) if cri is not None else None
            if deb:
                amt = abs(deb)
            elif cre:
                amt = -abs(cre)
            else:
                continue
        else:
            a = parse_amount(cell(ai))
            if a is None:
                continue
            t = cell(ti).strip().lower() if ti is not None else ""
            if t in ("credit", "cr", "c", "deposit"):
                amt, type_hint_used = -abs(a), True
            elif t in ("debit", "dr", "d", "withdrawal"):
                amt, type_hint_used = abs(a), True
            else:
                amt = a
        out.append((d, desc, amt))
    if not out:
        raise ParseError("No transactions with a valid date and amount were found.")
    if split or type_hint_used:
        return [Txn(d, s, round(a, 2)) for d, s, a in out if a != 0]
    return _orient(out, None)


def _parse_headerless(rows: list[list[str]]) -> list[Txn]:
    first = rows[0]
    di = next((i for i, c in enumerate(first) if parse_date(c)), None)
    if di is None:
        raise ParseError("Could not recognise the CSV layout (no header and no date column).")
    ai = next((i for i, c in reversed(list(enumerate(first))) if i != di and parse_amount(c) is not None), None)
    if ai is None:
        raise ParseError("Could not find an amount column.")
    text_cols = [i for i, c in enumerate(first) if i not in (di, ai) and parse_amount(c) is None]
    de = max(text_cols, key=lambda i: len(first[i])) if text_cols else None
    out = []
    for r in rows:
        if max(di, ai) >= len(r):
            continue
        d, a = parse_date(r[di]), parse_amount(r[ai])
        if d and a is not None:
            out.append((d, r[de].strip() if de is not None and de < len(r) else "Unknown", a))
    if not out:
        raise ParseError("No transactions found.")
    return _orient(out, None)


# ---------- pasted statement text ----------
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_LINE_RE = re.compile(
    r"^\s*(?P<date>\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,2}-\d{1,2}(?:-\d{2,4})?|"
    rf"(?:{_MONTHS})[a-z]*\.?\s+\d{{1,2}}(?:,?\s+\d{{4}})?|\d{{1,2}}\s+(?:{_MONTHS})[a-z]*\.?(?:\s+\d{{4}})?)"
    r"\s+(?:(?:\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+)?"  # optional second (post) date
    r"(?P<desc>.+?)\s+"
    r"(?P<amt>\(?[-+]?\s?\$?\s?-?\d[\d,]*\.\d{2}\)?(?:\s?(?:CR|DR))?-?)"
    r"(?:\s+\$?-?\d[\d,]*\.\d{2})?\s*$",  # optional trailing running balance
    re.I,
)


def _parse_loose_date(s: str, ref: date) -> date | None:
    s = s.strip().replace(".", "").replace(",", "")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(?i)^sept\b", "Sep", s)
    d = parse_date(s)
    if d:
        return d
    for fmt in ("%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    # no year: try with reference year, roll back a year if it lands in the future
    for fmt in ("%m/%d", "%m-%d", "%b %d", "%B %d", "%d %b", "%d %B"):
        try:
            d = datetime.strptime(f"{s} {ref.year}", f"{fmt} %Y").date()
        except ValueError:
            continue
        if (d - ref).days > 31:
            d = d.replace(year=d.year - 1)
        return d
    return None


def parse_pasted(text: str, ref: date | None = None) -> list[Txn]:
    ref = ref or date.today()
    lines = text.splitlines()
    if len(lines) > MAX_ROWS:
        raise ParseError(f"Too many lines (max {MAX_ROWS}).")
    # If it looks like CSV with a header, reuse the CSV parser.
    head = "\n".join(lines[:5]).lower()
    if ("," in head or "\t" in head) and "date" in head and ("amount" in head or "debit" in head):
        try:
            return parse_csv(text)
        except ParseError:
            pass
    out = []
    for line in lines:
        m = _LINE_RE.match(line.replace("\t", " "))
        if not m:
            continue
        d = _parse_loose_date(m.group("date"), ref)
        a = parse_amount(m.group("amt").replace(" ", ""))
        if d is None or a is None:
            continue
        out.append((d, m.group("desc").strip(), a))
    if not out:
        raise ParseError("No transaction lines recognised. Each line should look like: 01/15 NETFLIX.COM 15.49")
    return _orient(out, None)
