"""Recurring-charge detection, price-hike and 'possibly forgotten' flags."""
from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median

from .normalize import KNOWN_SUBSCRIPTIONS, normalize_merchant, slugify
from .parsing import Txn

# name, nominal days, tolerance days, min occurrences
CADENCES = [
    ("weekly", 7, 2, 4),
    ("biweekly", 14, 3, 3),
    ("monthly", 30.44, 6, 3),
    ("quarterly", 91.3, 12, 2),
    ("yearly", 365.25, 20, 2),
]
# months covered by one charge
MONTHS_PER = {"weekly": 12 / 52.1775, "biweekly": 12 / 26.0888, "monthly": 1, "quarterly": 3, "yearly": 12}
AMOUNT_TOL = 0.05       # amounts within 5% are "the same price"
SEGMENT_TOL = 0.03
SMALL_MONTHLY = 10.0
RECENT_DAYS = 120


@dataclass
class Subscription:
    merchant: str
    slug: str
    cadence: str
    interval_days: float
    charges: list[Txn]
    current_amount: float
    monthly_cost: float
    yearly_cost: float
    first_date: date
    last_date: date
    next_date: date
    active: bool
    price_hike: dict | None = None
    flags: list[str] = field(default_factory=list)
    known: bool = False
    label: str = ""

    @property
    def review(self) -> bool:
        """Worth a second look: two+ 'forgotten' signals, or a recent price increase."""
        if not self.active:
            return False
        return len(self.flags) >= 2 or bool(self.price_hike and self.price_hike["recent"])

    @property
    def count(self) -> int:
        return len(self.charges)


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def next_after(d: date, cadence: str) -> date:
    if cadence == "monthly":
        return add_months(d, 1)
    if cadence == "quarterly":
        return add_months(d, 3)
    if cadence == "yearly":
        return add_months(d, 12)
    return d + timedelta(days=7 if cadence == "weekly" else 14)


def _segments(amounts: list[float]) -> list[list[float]]:
    segs: list[list[float]] = []
    for a in amounts:
        if segs and abs(a - median(segs[-1])) <= SEGMENT_TOL * max(median(segs[-1]), 1):
            segs[-1].append(a)
        else:
            segs.append([a])
    return segs


def _classify(intervals: list[int], n: int, known: bool):
    if not intervals:
        return None
    med = median(intervals)
    for name, days, tol, min_n in CADENCES:
        if abs(med - days) > tol:
            continue
        need = min_n - 1 if known and name in ("monthly", "weekly", "biweekly") else min_n
        if n < max(need, 2):
            return None
        ok = sum(1 for i in intervals if abs(i - days) <= tol)
        if ok / len(intervals) >= 0.7:
            return name, days, tol
    return None


def _dedupe_same_day(charges: list[Txn]) -> list[Txn]:
    seen, out = set(), []
    for t in charges:
        k = (t.date, round(t.amount, 2))
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def detect_subscriptions(txns: list[Txn], today: date | None = None) -> list[Subscription]:
    spend = [t for t in txns if t.amount > 0]
    if not spend:
        return []
    as_of = max(max(t.date for t in spend), today or date.min)
    data_end = max(t.date for t in spend)
    groups: dict[str, list[Txn]] = defaultdict(list)
    for t in spend:
        groups[normalize_merchant(t.description)].append(t)

    subs: list[Subscription] = []
    for merchant, charges in groups.items():
        charges = sorted(_dedupe_same_day(charges), key=lambda t: t.date)
        known = merchant in KNOWN_SUBSCRIPTIONS
        # A merchant can carry two different plans (e.g. Apple: iCloud + Apple Music). Split by
        # price clusters when several stable amounts recur on their own cadence.
        for cluster in _price_clusters(charges):
            sub = _analyse(merchant, cluster, known, data_end, as_of)
            if sub:
                subs.append(sub)
    # disambiguate slugs
    names = [s.merchant for s in subs]
    for s in subs:
        s.label = s.merchant if names.count(s.merchant) == 1 else f"{s.merchant} (${s.current_amount:.2f} plan)"
    seen: dict[str, int] = {}
    for s in subs:
        if s.slug in seen:
            seen[s.slug] += 1
            s.slug = f"{s.slug}-{seen[s.slug]}"
        else:
            seen[s.slug] = 1
    subs.sort(key=lambda s: (not s.active, -s.monthly_cost))
    return subs


def _price_clusters(charges: list[Txn]) -> list[list[Txn]]:
    """Return [charges] unless the merchant has several interleaved price tiers that each recur."""
    if len(charges) < 4:
        return [charges]
    buckets: list[list[Txn]] = []
    for t in sorted(charges, key=lambda t: t.amount):
        if buckets and abs(t.amount - buckets[-1][-1].amount) <= 0.02 * max(t.amount, 1):
            buckets[-1].append(t)
        else:
            buckets.append([t])
    big = [b for b in buckets if len(b) >= 2]
    if len(big) < 2:
        return [charges]
    # interleaved (overlapping date ranges) => separate plans; sequential => price change
    big.sort(key=lambda b: min(t.date for t in b))
    overlapping = all(
        min(t.date for t in big[i + 1]) < max(t.date for t in big[i]) for i in range(len(big) - 1)
    )
    if not overlapping:
        return [charges]
    return [sorted(b, key=lambda t: t.date) for b in big]


def _analyse(merchant, charges, known, data_end, as_of) -> Subscription | None:
    if len(charges) < 2:
        return None
    if len(charges) >= 5:  # drop one-off outliers (e.g. a gym's annual fee) that sit between regular charges
        segs0 = _segments([t.amount for t in charges])
        keep, i = [], 0
        for j, seg in enumerate(segs0):
            if len(seg) > 1 or j == len(segs0) - 1:
                keep.extend(charges[i:i + len(seg)])
            i += len(seg)
        charges = keep
    intervals = [(b.date - a.date).days for a, b in zip(charges, charges[1:])]
    if any(i == 0 for i in intervals):
        return None
    cls = _classify(intervals, len(charges), known)
    if not cls:
        return None
    cadence, days, tol = cls
    amounts = [t.amount for t in charges]
    med = median(amounts)
    stable = all(abs(a - med) <= AMOUNT_TOL * max(med, 1) for a in amounts)
    segs = _segments(amounts)
    price_hike = None
    if not stable:
        allowed = min(3, 1 + len(amounts) // 3)
        if cadence in ("yearly", "quarterly") and len(amounts) == 2:
            allowed = 2
        if len(segs) > allowed:
            return None  # amounts too variable: probably regular shopping, not a subscription
    if len(segs) >= 2:
        prev, last = median(segs[-2]), median(segs[-1])
        if last > prev and (last - prev) >= 0.25:
            idx = len(amounts) - len(segs[-1])
            changed_on = charges[idx].date
            price_hike = {
                "old": round(prev, 2), "new": round(last, 2),
                "pct": round((last - prev) / prev * 100, 1) if prev else None,
                "date": changed_on, "recent": (data_end - changed_on).days <= RECENT_DAYS,
            }
    current = charges[-1].amount
    monthly = round(current / MONTHS_PER[cadence], 2)
    active = (data_end - charges[-1].date).days <= days + tol
    nxt = next_after(charges[-1].date, cadence)
    while active and nxt < as_of:  # roll the expected renewal forward to "today"
        nxt = next_after(nxt, cadence)
    sub = Subscription(
        merchant=merchant, slug=slugify(merchant), cadence=cadence, interval_days=days,
        charges=charges, current_amount=round(current, 2), monthly_cost=monthly,
        yearly_cost=round(monthly * 12, 2), first_date=charges[0].date, last_date=charges[-1].date,
        next_date=nxt, active=active, price_hike=price_hike, known=known,
    )
    sub.flags = forgotten_flags(sub)
    return sub


def forgotten_flags(s: Subscription) -> list[str]:
    """We can't know usage, so we surface heuristics that correlate with forgotten subscriptions."""
    if not s.active:
        return ["Looks stopped (no recent charge)"]
    flags = []
    if s.price_hike:
        flags.append("Price increased" + (" recently" if s.price_hike["recent"] else ""))
    span = (s.last_date - s.first_date).days
    if s.count >= 6 or span >= 180:
        flags.append("Long-running")
    if s.monthly_cost <= SMALL_MONTHLY:
        flags.append("Small charge (easy to overlook)")
    if s.cadence == "yearly":
        flags.append("Annual renewal (easy to forget)")
    return flags


def totals(subs: list[Subscription]) -> dict:
    active = [s for s in subs if s.active]
    m = round(sum(s.monthly_cost for s in active), 2)
    return {
        "monthly": m, "yearly": round(m * 12, 2), "count": len(active),
        "hikes": sum(1 for s in active if s.price_hike),
        "flagged": sum(1 for s in active if s.review),
        "hike_extra_yearly": round(sum(
            (s.price_hike["new"] - s.price_hike["old"]) / MONTHS_PER[s.cadence] * 12
            for s in active if s.price_hike), 2),
    }


def upcoming(subs: list[Subscription], today: date, days: int = 14) -> list[Subscription]:
    return sorted([s for s in subs if s.active and today <= s.next_date <= today + timedelta(days=days)],
                  key=lambda s: s.next_date)
