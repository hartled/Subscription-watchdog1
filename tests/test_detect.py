from datetime import date, timedelta
from pathlib import Path

from app.detect import add_months, detect_subscriptions, totals, upcoming
from app.parsing import Txn, parse_csv

ROOT = Path(__file__).resolve().parents[1]


def monthly(desc, amt, n, start=date(2025, 1, 5), jitter=(0, 2, -1, 1, 3, 0, -2)):
    return [Txn(add_months(start, i) + timedelta(days=jitter[i % len(jitter)]), desc, amt) for i in range(n)]


def by_name(subs):
    return {s.label: s for s in subs}


def test_monthly_with_jitter():
    subs = detect_subscriptions(monthly("SPOTIFY USA", 11.99, 6))
    s = by_name(subs)["Spotify"]
    assert s.cadence == "monthly" and s.monthly_cost == 11.99 and s.yearly_cost == 143.88


def test_weekly_biweekly_quarterly_yearly():
    start = date(2025, 1, 1)
    tx = [Txn(start + timedelta(days=7 * i), "WEEKLY PAPER CO", 4.0) for i in range(8)]
    tx += [Txn(start + timedelta(days=14 * i + (1 if i % 2 else 0)), "LAWN CARE PROS", 40.0) for i in range(6)]
    tx += [Txn(add_months(start, 3 * i), "STATE FARM", 300.0) for i in range(4)]
    tx += [Txn(date(2025, 2, 1), "AMAZON PRIME*AB12", 139.0), Txn(date(2026, 2, 3), "AMAZON PRIME*ZZ99", 139.0)]
    s = by_name(detect_subscriptions(tx))
    assert s["Weekly Paper"].cadence == "weekly"
    assert round(s["Weekly Paper"].monthly_cost, 2) == 17.39
    assert s["Lawn Care Pros"].cadence == "biweekly"
    assert s["State Farm"].cadence == "quarterly" and s["State Farm"].monthly_cost == 100.0
    assert s["Amazon Prime"].cadence == "yearly" and s["Amazon Prime"].monthly_cost == 11.58
    assert "Annual renewal (easy to forget)" in s["Amazon Prime"].flags


def test_price_hike_detected():
    tx = monthly("NETFLIX.COM", 15.49, 6) + monthly("NETFLIX.COM", 17.99, 3, start=add_months(date(2025, 1, 5), 6))
    s = by_name(detect_subscriptions(tx))["Netflix"]
    assert s.price_hike["old"] == 15.49 and s.price_hike["new"] == 17.99
    assert s.price_hike["recent"] and s.current_amount == 17.99 and s.review


def test_variable_spend_is_not_a_subscription():
    import random
    random.seed(1)
    tx = [Txn(add_months(date(2025, 1, 3), i), "WHOLEFDS MKT #1", round(random.uniform(40, 160), 2)) for i in range(10)]
    tx += [Txn(date(2025, 1, 1) + timedelta(days=random.randint(0, 300)), "SQ *COFFEE", 5.0) for _ in range(12)]
    assert detect_subscriptions(tx) == []


def test_two_plans_same_merchant_split():
    tx = monthly("APPLE.COM/BILL", 2.99, 6) + monthly("APPLE.COM/BILL", 10.99, 6, start=date(2025, 1, 20))
    subs = [s for s in detect_subscriptions(tx) if s.merchant == "Apple"]
    assert sorted(s.current_amount for s in subs) == [2.99, 10.99]
    assert len({s.slug for s in subs}) == 2


def test_one_off_outlier_ignored():
    tx = monthly("PLANET FITNESS #12", 15.0, 10) + [Txn(date(2025, 4, 25), "PLANET FITNESS #12", 49.0)]
    s = by_name(detect_subscriptions(tx))["Planet Fitness"]
    assert s.current_amount == 15.0 and s.price_hike is None and s.count == 10


def test_stopped_subscription_inactive_and_excluded():
    tx = monthly("HULU 877", 18.99, 4) + monthly("SPOTIFY", 11.99, 10)
    subs = detect_subscriptions(tx)
    hulu = by_name(subs)["Hulu"]
    assert not hulu.active
    assert totals(subs)["monthly"] == 11.99


def test_refunds_ignored_and_too_few_charges():
    tx = [Txn(date(2025, 1, 1), "RANDOM SHOP", 20.0), Txn(date(2025, 2, 1), "RANDOM SHOP", 20.0),
          Txn(date(2025, 3, 1), "RANDOM SHOP", -20.0)]
    assert detect_subscriptions(tx) == []  # unknown merchant needs 3 monthly charges


def test_known_merchant_two_monthly_charges():
    tx = [Txn(date(2025, 1, 1), "NETFLIX.COM", 15.49), Txn(date(2025, 2, 1), "NETFLIX.COM", 15.49)]
    assert by_name(detect_subscriptions(tx))["Netflix"].cadence == "monthly"


def test_upcoming_and_next_date():
    tx = monthly("SPOTIFY", 11.99, 4, start=date(2026, 1, 10), jitter=(0,))
    subs = detect_subscriptions(tx, today=date(2026, 4, 20))
    s = subs[0]
    assert s.next_date == date(2026, 5, 10)
    assert upcoming(subs, date(2026, 5, 1), 14) == [s]
    assert upcoming(subs, date(2026, 4, 20), 14) == []


def test_sample_file_end_to_end():
    subs = detect_subscriptions(parse_csv((ROOT / "sample" / "sample_statement.csv").read_text()))
    names = {s.merchant for s in subs}
    assert {"Netflix", "Spotify", "Adobe", "Apple", "Amazon Prime", "YouTube Premium", "Hulu",
            "Planet Fitness", "Patreon Membership", "State Farm Insurance"} <= names
    assert not {"Wholefds Mkt", "Blue Bottle Coffee"} & names
    t = totals(subs)
    assert t["hikes"] == 2 and t["count"] == 10
