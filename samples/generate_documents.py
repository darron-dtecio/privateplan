"""Generate the sample source documents for PrivatePlan.

Every figure is invented.  The point is that the column shapes match what the
real parsers expect, so a new user can watch ingestion actually work.
"""

import csv
import pathlib
import random
from datetime import date, timedelta

random.seed(20260829)  # deterministic: regenerating must not churn the repo

OUT = pathlib.Path("samples/documents")
OUT.mkdir(parents=True, exist_ok=True)

MONTHS = [(y, m) for y in (2025, 2026) for m in range(1, 13)]
MONTHS = [ym for ym in MONTHS if (2025, 2) <= ym <= (2026, 7)]  # 18 months


def d(y, m, day):
    day = min(day, 28)
    return f"{m:02d}/{day:02d}/{y}"


def jitter(base, pct=0.12):
    return round(base * (1 + random.uniform(-pct, pct)), 2)


# --------------------------------------------------------------- checking ----
checking = [["DATE", "DESCRIPTION", "AMOUNT", "CHECK #", "STATUS"]]
for y, m in MONTHS:
    # income: two paychecks each, both households
    checking.append([d(y, m, 5), "ACME CORP EDIPAYMENT PAYROLL", f"{jitter(4180):.2f}", "", "posted"])
    checking.append([d(y, m, 20), "ACME CORP EDIPAYMENT PAYROLL", f"{jitter(4180):.2f}", "", "posted"])
    checking.append([d(y, m, 15), "NORTHWIND LLC DIRECT DEP", f"{jitter(2640):.2f}", "", "posted"])
    # housing
    checking.append([d(y, m, 1), "BILL PAY MORTGAGE SERVICING CO", "-2960.00", "", "posted"])
    # saving (must NOT count as spending)
    checking.append([d(y, m, 6), "ONLINE TRANSFER TO BROKERAGE", "-750.00", "", "posted"])
    checking.append([d(y, m, 6), "FID BKG SVC LLC MONEYLINE", "-1150.00", "", "posted"])
    # recurring bills
    checking.append([d(y, m, 8), "VERIZON WIRELESS AUTOPAY", f"-{jitter(245):.2f}", "", "posted"])
    checking.append([d(y, m, 9), "CITY WATER AND SEWER", f"-{jitter(96):.2f}", "", "posted"])
    checking.append([d(y, m, 10), "REGIONAL ELECTRIC UTILITY", f"-{jitter(324):.2f}", "", "posted"])
    checking.append([d(y, m, 12), "STATE FARM INSURANCE PREM", f"-{jitter(390):.2f}", "", "posted"])
    checking.append([d(y, m, 14), "AMERICAN EXPRESS ACH PMT", f"-{jitter(940):.2f}", "", "posted"])
    checking.append([d(y, m, 18), "NETFLIX.COM SUBSCRIPTION", "-22.99", "", "posted"])
    checking.append([d(y, m, 19), "SPOTIFY USA SUBSCRIPTION", "-17.99", "", "posted"])
    checking.append([d(y, m, 22), "LAWN AND LANDSCAPE SERVICE", f"-{jitter(260):.2f}", "", "posted"])
    checking.append([d(y, m, 24), "ATM WITHDRAWAL MAIN ST", "-200.00", "", "posted"])
    checking.append([d(y, m, 26), "AUTO LOAN PAYMENT", "-540.00", "", "posted"])

# two genuine one-offs, well above the recurring run rate
checking.append([d(2025, 9, 18), "SUMMIT HVAC REPLACEMENT", "-9800.00", "", "posted"])
checking.append([d(2026, 3, 4), "AUTO REPAIR TRANSMISSION", "-4400.00", "", "posted"])

# ------------------------------------------------------------------- card ----
card = [["DATE", "DESCRIPTION", "AMOUNT", "CARD MEMBER", "CATEGORY"]]
MERCHANTS = [
    ("SAFEWAY #1842", 148, 6), ("COSTCO WHSE #0431", 210, 2),
    ("TRADER JOES #221", 92, 3), ("STARBUCKS STORE 09112", 12, 6),
    ("TST* CORNER BAKERY DOWNTOWN", 38, 2), ("DOORDASH ORDER", 54, 3),
    ("CHEVRON 00934521", 68, 4), ("SHELL OIL 574102", 61, 2),
    ("AMAZON.COM*RT4G92", 74, 5), ("TARGET T-1290", 88, 3),
    ("CVS PHARMACY #4471", 46, 2), ("MAIN STREET PHARMACY", 34, 1),
    ("PETCO 1123", 61, 1), ("HOME DEPOT #6612", 122, 1),
    ("UNITED AIRLINES 0162", 420, 1), ("MARRIOTT HOTELS", 285, 1),
]
for y, m in MONTHS:
    for name, base, times in MERCHANTS:
        for i in range(times):
            card.append([d(y, m, 2 + i * 4), name, f"{jitter(base, 0.25):.2f}",
                         "M CHEN", ""])

# -------------------------------------------------------------- brokerage ----
act = [["Run Date", "Account", "Action", "Symbol", "Description", "Type",
        "Quantity", "Price ($)", "Commission ($)", "Fees ($)",
        "Accrued Interest ($)", "Amount ($)", "Settlement Date"]]
for y, m in MONTHS:
    act.append([d(y, m, 6), "401K(*7788)", "CONTRIBUTION EMPLOYEE PRETAX", "FXAIX",
                "FIDELITY 500 INDEX FUND", "Cash", "7.412", "199.72", "", "", "",
                f"{jitter(1480):.2f}", d(y, m, 6)])
    act.append([d(y, m, 6), "401K(*7788)", "CONTRIBUTION EMPLOYER MATCH", "FXAIX",
                "FIDELITY 500 INDEX FUND", "Cash", "2.850", "199.72", "", "", "",
                f"{jitter(569):.2f}", d(y, m, 6)])
    act.append([d(y, m, 12), "HSA(*3391)", "DEBIT CARD PURCHASE MAIN STREET PHARMACY",
                "", "", "Cash", "", "", "", "", "", f"-{jitter(78):.2f}", d(y, m, 12)])
    act.append([d(y, m, 21), "HSA(*3391)", "BILL PAYMENT FAMILY DENTAL",
                "", "", "Cash", "", "", "", "", "", f"-{jitter(140):.2f}", d(y, m, 21)])
    if m in (3, 6, 9, 12):
        act.append([d(y, m, 28), "IRA(*1234)", "DIVIDEND RECEIVED", "BND",
                    "VANGUARD TOTAL BOND MARKET ETF", "Cash", "", "", "", "", "",
                    f"{jitter(640):.2f}", d(y, m, 28)])
        act.append([d(y, m, 28), "BROKERAGE(*1234)", "ADVISORY FEE QUARTERLY",
                    "", "PROGRAM FEE", "Cash", "", "", "", "", "",
                    f"-{jitter(620, 0.03):.2f}", d(y, m, 28)])
for y, m, sh in ((2025, 11, 240.0), (2026, 2, 210.0), (2026, 5, 210.0)):
    act.append([d(y, m, 15), "BROKERAGE(*1234)", "CONVERSION SHARES DEPOSITED ACME CORP",
                "ACME", "ACME CORP COMMON STOCK", "Cash", f"{sh:.3f}", "118.50",
                "", "", "", "0.00", d(y, m, 15)])
act.append([d(2025, 4, 2), "IRA(*1234)", "TRANSFER OF ASSETS ROLLOVER IN", "",
            "ROLLOVER FROM PRIOR EMPLOYER PLAN", "Cash", "", "", "", "", "",
            "42000.00", d(2025, 4, 2)])

# -------------------------------------------------------------- positions ----
positions = [["Account Number", "Account Name", "Symbol", "Description",
              "Quantity", "Last Price", "Current Value", "Cost Basis Total",
              "Total Gain/Loss Dollar", "Type"]]
POS = [
    ("*7788", "401k Plan", "FXAIX", "FIDELITY 500 INDEX FUND", 1902.44, 199.72, 380000, None),
    ("*7788", "401k Plan", "VUG", "VANGUARD GROWTH ETF", 316.28, 417.34, 132000, None),
    ("*7788", "401k Plan", "BND", "VANGUARD TOTAL BOND MARKET ETF", 1373.63, 72.80, 100000, None),
    ("*5502", "Spouse 401k Plan", "VTSAX", "VANGUARD TOTAL STOCK MKT IDX ADM", 2077.51, 139.60, 290000, None),
    ("*1234", "401k R/O IRA", "BND", "VANGUARD TOTAL BOND MARKET ETF", 1263.74, 72.80, 92000, None),
    ("*1234", "401k R/O IRA", "VXUS", "VANGUARD TOTAL INTL STOCK ETF", 950.36, 69.45, 66000, None),
    ("*4417", "Roth IRA", "VOO", "VANGUARD S&P 500 ETF", 106.72, 580.95, 62000, None),
    ("*4417", "Roth IRA", "SCHD", "SCHWAB US DIVIDEND EQUITY ETF", 1141.55, 28.03, 32000, None),
    ("*1234", "Brokerage", "VTI", "VANGUARD TOTAL STOCK MARKET ETF", 456.19, 317.85, 145000, 98000),
    ("*1234", "Brokerage", "SPY", "SPDR S&P 500 ETF TRUST", 86.94, 632.62, 55000, 41000),
    ("*1234", "Brokerage", "AAPL", "APPLE INC", 88.71, 248.00, 22000, 12000),
    ("*1234", "Brokerage", "MSFT", "MICROSOFT CORP", 34.15, 527.09, 18000, 19000),
    ("*1234", "Brokerage", "SPAXX", "FIDELITY GOVERNMENT MONEY MARKET", 8000.0, 1.00, 8000, 8000),
]
for acct, name, sym, desc, qty, px, val, cost in POS:
    gain = "" if cost is None else f"{val - cost:.2f}"
    positions.append([acct, name, sym, desc, f"{qty:.3f}", f"{px:.2f}",
                      f"{val:.2f}", "" if cost is None else f"{cost:.2f}",
                      gain, "Margin" if acct == "*1234" else "Cash"])

# ---------------------------------------------------------------- vesting ----
vest = [["Award Date", "Award ID", "Award Type", "Vest Date", "Vest Shares",
         "Award Price", "Is Vested", "Tax Withheld Shares", "Tax Withheld Value"]]
VESTS = [
    ("2022-11-15", "RSU-2022-A", "2023-11-15", 240, "Yes", 74, 8214.0),
    ("2022-11-15", "RSU-2022-A", "2024-11-15", 240, "Yes", 74, 8658.0),
    ("2023-11-15", "RSU-2023-A", "2025-11-15", 210, "Yes", 65, 7702.5),
    ("2023-11-15", "RSU-2023-A", "2026-11-15", 210, "No", "", ""),
    ("2024-02-15", "RSU-2024-A", "2027-02-15", 185, "No", "", ""),
    ("2024-02-15", "RSU-2024-A", "2027-05-15", 185, "No", "", ""),
    ("2024-11-15", "RSU-2024-B", "2027-11-15", 195, "No", "", ""),
    ("2025-02-15", "RSU-2025-A", "2028-02-15", 160, "No", "", ""),
    ("2025-11-15", "RSU-2025-B", "2028-11-15", 160,
     "vests only if the performance multiplier is met", "", ""),
    ("2026-02-15", "RSU-2026-A", "2029-02-15", 140, "No", "", ""),
]
for award, aid, vdate, shares, status, wsh, wval in VESTS:
    vest.append([award, aid, "RSU", vdate, f"{shares}", "", status,
                 wsh if wsh != "" else "", f"{wval:.2f}" if wval != "" else ""])


def write(name, rows):
    path = OUT / name
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    print(f"{name:28s} {len(rows) - 1:5d} rows")


write("checking_ledger.csv", checking)
write("card_ledger.csv", card)
write("brokerage_activity.csv", act)
write("positions.csv", positions)
write("vesting_schedule.csv", vest)
