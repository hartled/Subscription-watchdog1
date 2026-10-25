"""Regenerate sample_statement.csv (Chase-style layout, negative = spend)."""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(7)
rows = []
def add(d, desc, amt, cat="Shopping", typ="Sale"):
    rows.append((d, desc, cat, typ, amt))

def monthly(day, start, end):
    y, m = start
    while (y, m) <= end:
        yield date(y, m, min(day, 28))
        m += 1
        if m > 12:
            y, m = y + 1, 1

for d in monthly(3, (2025, 8), (2026, 10)):
    add(d, "NETFLIX.COM 866-579-7172 CA", -(15.49 if d < date(2026, 7, 1) else 17.99), "Entertainment")
for d in monthly(12, (2025, 8), (2026, 10)):
    add(d, "Spotify USA", -11.99, "Entertainment")
for d in monthly(18, (2025, 8), (2026, 10)):
    add(d, "APPLE.COM/BILL 866-712-7753 CA", -2.99)
for d in monthly(22, (2025, 8), (2026, 10)):
    add(d, "APPLE.COM/BILL 866-712-7753 CA", -10.99)
for d in monthly(6, (2025, 8), (2026, 10)):
    add(d, "ADOBE *CREATIVE CLOUD 408-536-6000 CA", -(54.99 if d < date(2026, 3, 1) else 59.99))
for d in monthly(15, (2025, 8), (2026, 5)):
    add(d, "HULU 877-8244858 CA", -18.99, "Entertainment")
for d in monthly(9, (2025, 8), (2026, 10)):
    add(d, "GOOGLE *YouTubePremium g.co/helppay#", -13.99, "Entertainment")
for d in monthly(1, (2025, 8), (2026, 10)):
    add(d, "PATREON* MEMBERSHIP", -5.00)
for d in monthly(17, (2025, 8), (2026, 10)):
    add(d, "PLANET FITNESS CLUB #0412", -15.00, "Health & Wellness")
add(date(2026, 1, 25), "PLANET FITNESS CLUB #0412", -49.00, "Health & Wellness")
add(date(2025, 9, 10), "Amazon Prime*2K4LM8", -139.00)
add(date(2026, 9, 10), "Amazon Prime*7Q1ZX2", -139.00)
for d in [date(2025, 8, 20), date(2025, 11, 20), date(2026, 2, 20), date(2026, 5, 20), date(2026, 8, 20)]:
    add(d, "STATE FARM INSURANCE 800-956-6310", -312.40, "Bills & Utilities")
# noise
d = date(2025, 8, 2)
while d <= date(2026, 10, 20):
    add(d, "WHOLEFDS MKT #10234", -round(random.uniform(35, 160), 2), "Groceries")
    if random.random() < 0.6:
        add(d + timedelta(days=2), "SQ *BLUE BOTTLE COFFEE", -round(random.uniform(4, 14), 2), "Food & Drink")
    if random.random() < 0.3:
        add(d + timedelta(days=3), "AMAZON MKTPL*RT5Y12", -round(random.uniform(9, 90), 2))
    d += timedelta(days=7)
for d in monthly(1, (2025, 8), (2026, 10)):
    add(d, "Payment Thank You-Mobile", 1200.00, "", "Payment")
add(date(2026, 4, 2), "AMAZON MKTPL*RT5Y12 RETURN", 24.99, "Shopping", "Return")

rows.sort(key=lambda r: r[0], reverse=True)
out = Path(__file__).with_name("sample_statement.csv")
with out.open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Transaction Date", "Post Date", "Description", "Category", "Type", "Amount", "Memo"])
    for d, desc, cat, typ, amt in rows:
        w.writerow([d.strftime("%m/%d/%Y"), (d + timedelta(days=1)).strftime("%m/%d/%Y"), desc, cat, typ, f"{amt:.2f}", ""])
print(out, len(rows))
