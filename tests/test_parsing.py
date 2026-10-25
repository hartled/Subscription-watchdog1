from datetime import date

import pytest

from app.normalize import mask_digits, normalize_merchant
from app.parsing import ParseError, parse_amount, parse_csv, parse_date, parse_pasted


def test_parse_amount_variants():
    assert parse_amount("$1,234.50") == 1234.50
    assert parse_amount("-15.49") == -15.49
    assert parse_amount("(12.00)") == -12.00
    assert parse_amount("10.00 CR") == -10.00
    assert parse_amount("10.00-") == -10.00
    assert parse_amount("abc") is None
    assert parse_amount("") is None


def test_parse_date_variants():
    assert parse_date("2026-01-05") == date(2026, 1, 5)
    assert parse_date("01/05/2026") == date(2026, 1, 5)
    assert parse_date("1/5/26") == date(2026, 1, 5)
    assert parse_date("05 Jan 2026") == date(2026, 1, 5)
    assert parse_date("nope") is None


def test_negative_is_spend_layout():  # Chase-style
    csv = "Transaction Date,Post Date,Description,Category,Type,Amount\n" \
          "01/03/2026,01/04/2026,NETFLIX.COM,Entertainment,Sale,-15.49\n" \
          "01/10/2026,01/10/2026,Payment Thank You,,Payment,500.00\n" \
          "01/12/2026,01/13/2026,Spotify USA,Entertainment,Sale,-11.99\n"
    t = parse_csv(csv)
    amounts = {x.description: x.amount for x in t}
    assert amounts["NETFLIX.COM"] == 15.49
    assert amounts["Payment Thank You"] == -500.00


def test_positive_is_spend_layout():  # Amex-style
    csv = "Date,Description,Amount\n01/03/2026,NETFLIX.COM,15.49\n01/04/2026,SPOTIFY,11.99\n01/09/2026,AUTOPAY PAYMENT,-300.00\n"
    t = parse_csv(csv)
    assert [x.amount for x in t] == [15.49, 11.99, -300.00]


def test_debit_credit_split_layout():  # Capital One / many banks
    csv = "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n" \
          "2026-01-03,2026-01-04,1234,NETFLIX.COM,Entertainment,15.49,\n" \
          "2026-01-05,2026-01-05,1234,CAPITAL ONE AUTOPAY,Payment,,250.00\n"
    t = parse_csv(csv)
    assert t[0].amount == 15.49 and t[1].amount == -250.00


def test_withdrawal_deposit_with_preamble_and_semicolons():
    csv = "Account: Checking ****1234\nExported 2026-02-01\n" \
          "Date;Payee;Withdrawal;Deposit;Balance\n" \
          "02/01/2026;HULU 877-8244858;18.99;;1000.00\n" \
          "02/02/2026;PAYROLL;;2000.00;3000.00\n"
    t = parse_csv(csv)
    assert t[0].description.startswith("HULU") and t[0].amount == 18.99
    assert t[1].amount == -2000.00


def test_type_column_credit_debit():
    csv = "Date,Description,Amount,Credit/Debit\n2026-01-01,DROPBOX,11.99,Debit\n2026-01-02,REFUND,5.00,Credit\n"
    t = parse_csv(csv)
    assert t[0].amount == 11.99 and t[1].amount == -5.00


def test_headerless_csv():
    csv = "01/03/2026,NETFLIX.COM,-15.49\n02/03/2026,NETFLIX.COM,-15.49\n02/05/2026,PAYROLL,2000.00\n02/07/2026,STORE,-3.00\n"
    t = parse_csv(csv)
    assert t[0].description == "NETFLIX.COM" and t[0].amount == 15.49


def test_bad_csv_raises():
    with pytest.raises(ParseError):
        parse_csv("foo,bar\nbaz,qux\n")
    with pytest.raises(ParseError):
        parse_csv("")


def test_pasted_statement_lines():
    text = """
    Statement period 01/01/2026 - 01/31/2026
    01/03 NETFLIX.COM 866-579-7172 CA 15.49
    01/05 01/06 SPOTIFY USA $11.99
    Jan 9, 2026 Adobe Creative Cloud 54.99
    01/15 PAYMENT THANK YOU 500.00 CR
    2026-01-20 Patreon* Membership 5.00 1,234.56
    Total fees for this period 0.00 ?
    """
    t = parse_pasted(text, ref=date(2026, 2, 1))
    d = {x.description: x for x in t}
    assert d["NETFLIX.COM 866-579-7172 CA"].amount == 15.49
    assert d["NETFLIX.COM 866-579-7172 CA"].date == date(2026, 1, 3)
    assert d["SPOTIFY USA"].amount == 11.99
    assert d["Adobe Creative Cloud"].date == date(2026, 1, 9)
    assert d["PAYMENT THANK YOU"].amount == -500.00
    assert d["Patreon* Membership"].amount == 5.00  # trailing running balance ignored


def test_pasted_year_rollover():
    t = parse_pasted("12/28 NETFLIX.COM 15.49\n01/02 SPOTIFY 11.99", ref=date(2026, 1, 10))
    assert t[0].date == date(2025, 12, 28) and t[1].date == date(2026, 1, 2)


def test_pasted_nothing_raises():
    with pytest.raises(ParseError):
        parse_pasted("hello world")


@pytest.mark.parametrize("raw,expected", [
    ("NETFLIX.COM 866-579-7172 CA", "Netflix"),
    ("APPLE.COM/BILL 866-712-7753 CA", "Apple"),
    ("GOOGLE *YouTubePremium g.co/helppay#", "YouTube Premium"),
    ("Amazon Prime*2K4LM8", "Amazon Prime"),
    ("SQ *BLUE BOTTLE COFFEE", "Blue Bottle Coffee"),
    ("PAYPAL *ACMESOFTWARE 4029357733", "Acmesoftware"),
    ("TST* JOES PIZZA 0042 NEW YORK NY", "Joes Pizza New"),
    ("WHOLEFDS MKT #10234", "Wholefds Mkt"),
    ("PATREON* MEMBERSHIP", "Patreon Membership"),
])
def test_normalize_merchant(raw, expected):
    assert normalize_merchant(raw) == expected


def test_mask_digits():
    assert "4111111111111111" not in mask_digits("CARD 4111111111111111 NETFLIX")
    assert mask_digits("NETFLIX 2026") == "NETFLIX 2026"
