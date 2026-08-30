"""Parse a brokerage/retirement transaction history (Fidelity-style export).

Two things come out of this that no other document shows:
  * spending paid straight from an HSA (debit card and medical bill payments),
    which never touches the checking account and would otherwise be invisible;
  * plan mechanics — contributions, dividends, RSU/ESPP vests, rollovers and
    any outstanding 401(k) loan being repaid.

Rollovers and internal transfers are reported separately and never counted as
income or spending: moving a 401(k) to an IRA is not a cash event.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# (group, pattern) — first match wins.
ACTION_GROUPS: list[tuple[str, re.Pattern]] = [
    ("loan repayment", re.compile(r"loan\s*repay", re.I)),
    ("loan fee", re.compile(r"loan\s*(maint|serv).*fee|loan.*fee", re.I)),
    ("contribution", re.compile(r"^\s*contribution|partic\s*contr|"
                                r"employer\s*contr|co\s*contr|cur\s*yr", re.I)),
    ("withdrawal / rollover", re.compile(r"^\s*withdrawal|distribution|rollover", re.I)),
    ("HSA / medical spending", re.compile(
        r"debit\s*card|check\s*paid|bill\s*payment", re.I)),
    ("dividend / interest", re.compile(
        r"dividend\s*received|interest\s*earned|^\s*dividends?\b|^\s*interest\b", re.I)),
    ("reinvestment", re.compile(r"reinvest", re.I)),
    ("shares deposited (RSU/ESPP)", re.compile(
        r"conversion\s*shares|shares\s*deposited|espp|restricted\s*stock", re.I)),
    ("market value change", re.compile(r"change\s*on\s*market\s*value", re.I)),
    ("buy", re.compile(r"^\s*you\s*bought|purchase\s*into|^\s*buy\b", re.I)),
    ("sell", re.compile(r"^\s*you\s*sold|redemption|^\s*sell\b", re.I)),
    ("transfer", re.compile(r"transfer|journal", re.I)),
    ("fee", re.compile(r"fee|expense", re.I)),
]

# groups that are neither spending nor income — pure bookkeeping
NON_CASH = {"market value change", "reinvestment"}
INTERNAL = {"transfer", "withdrawal / rollover"}
SPENDING = {"HSA / medical spending"}


def group_action(action: str) -> str:
    for name, rx in ACTION_GROUPS:
        if rx.search(action):
            return name
    return "other"


def _month(s: str) -> str | None:
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", str(s).strip())
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(s).strip())
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _amount(raw) -> float | None:
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).replace("$", "").replace(",", "").strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = float(s.strip("()"))
    except ValueError:
        return None
    return -v if neg else v


def find_columns(header: list) -> dict[str, int] | None:
    texts = [str(c).strip().lower() for c in header]
    col: dict[str, int] = {}
    for i, t in enumerate(texts):
        if "date" in t and "settle" not in t and "date" not in col:
            col["date"] = i
        elif t == "account" and "account" not in col:
            col["account"] = i
        elif "action" in t and "action" not in col:
            col["action"] = i
        elif t == "symbol" and "symbol" not in col:
            col["symbol"] = i
        elif t == "description" and "description" not in col:
            col["description"] = i
        elif t == "type" and "type" not in col:
            col["type"] = i
        elif t.startswith("price") and "price" not in col:
            col["price"] = i
        elif t == "quantity" and "quantity" not in col:
            col["quantity"] = i
        elif t.startswith("amount") and "amount" not in col:
            col["amount"] = i
    return col if {"date", "action", "amount"} <= col.keys() else None


def normalize(rows: list[list], col: dict[str, int]) -> list[dict]:
    """Keep the transaction fields needed to merge overlapping exports."""
    out = []
    for row in rows:
        if len(row) <= max(col.values()):
            continue
        if _month(str(row[col["date"]])) is None:
            continue

        def value(name):
            i = col.get(name)
            return row[i] if i is not None and i < len(row) else ""

        raw_date = str(value("date")).strip()
        m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", raw_date)
        date = (f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
                if m else raw_date[:10])
        out.append({
            "date": date,
            "account": str(value("account")).strip()[:50],
            "action": str(value("action")).strip()[:100],
            "symbol": str(value("symbol")).strip().upper()[:24] or None,
            "description": str(value("description")).strip()[:80] or None,
            "type": str(value("type")).strip()[:24] or None,
            "price": _amount(value("price")),
            "quantity": _amount(value("quantity")),
            "amount": _amount(value("amount")),
        })
    return out


def analyze_records(records: list[dict], sale_basis_overrides: list[dict] | None = None) -> dict:
    rows = [[r.get("date"), r.get("account"), r.get("action"), r.get("amount")]
            for r in records]
    out = analyze(rows, {"date": 0, "account": 1, "action": 2, "amount": 3})
    out.update(analyze_sales(records, sale_basis_overrides))
    return out


def _account_key(value: str | None) -> str:
    """Match redacted and differently labelled versions of the same account."""
    text = str(value or "").strip().lower()
    numbers = re.findall(r"\d{4}", text)
    return f"*{numbers[-1]}" if numbers else re.sub(r"[^a-z0-9]+", "", text)


def _security_key(record: dict) -> str:
    symbol = str(record.get("symbol") or "").strip().upper()
    if symbol:
        return re.sub(r"[^A-Z0-9.]+", "", symbol)
    return re.sub(r"[^a-z0-9]+", "", str(record.get("description") or "").lower())


def _trade_value(record: dict) -> float | None:
    amount = _amount(record.get("amount"))
    if amount not in (None, 0):
        return abs(amount)
    quantity = _amount(record.get("quantity"))
    price = _amount(record.get("price"))
    return abs(quantity * price) if quantity and price else None


def _uncancelled_trades(records: list[dict]) -> tuple[list[tuple[int, dict]], int]:
    """Remove exact broker correction pairs before counting buys and sells.

    Wells Fargo represents a cancelled buy as a Buy with negative shares and a
    credit, and a cancelled sale as a Sell with positive shares and a debit.
    Treating either as a new trade creates a fake disposal or acquisition.
    """
    ordered = sorted(enumerate(records), key=lambda item: (
        str(item[1].get("date") or ""), item[0]))
    active: list[tuple[int, dict]] = []
    reversed_indices: set[int] = set()
    pairs = 0
    for idx, record in ordered:
        group = group_action(str(record.get("action") or ""))
        if group not in {"buy", "sell"}:
            if group == "reinvestment":
                active.append((idx, record))
            continue
        qty = _amount(record.get("quantity")) or 0.0
        amount = _amount(record.get("amount")) or 0.0
        is_reversal = ((group == "buy" and qty < 0 and amount >= 0)
                       or (group == "sell" and qty > 0 and amount <= 0))
        if not is_reversal:
            active.append((idx, record))
            continue
        value = _trade_value(record)
        match = None
        for pos in range(len(active) - 1, -1, -1):
            prior_idx, prior = active[pos]
            if prior_idx in reversed_indices:
                continue
            if group_action(str(prior.get("action") or "")) != group:
                continue
            if (_account_key(prior.get("account")), _security_key(prior)) != (
                    _account_key(record.get("account")), _security_key(record)):
                continue
            prior_qty = abs(_amount(prior.get("quantity")) or 0.0)
            prior_value = _trade_value(prior)
            if abs(prior_qty - abs(qty)) > 0.000001:
                continue
            if value is not None and prior_value is not None and abs(value - prior_value) > 0.02:
                continue
            match = pos
            break
        if match is not None:
            prior_idx, _ = active.pop(match)
            reversed_indices.update({prior_idx, idx})
            pairs += 1
        else:
            # Keep an unmatched correction visible; it will not be mistaken for
            # a normal acquisition or sale by analyze_sales below.
            active.append((idx, record))
    return active, pairs


def _sale_basis_override(record: dict, quantity: float,
                         overrides: list[dict]) -> dict | None:
    for override in overrides:
        if str(override.get("date") or "") != str(record.get("date") or ""):
            continue
        if str(override.get("symbol") or "").upper() != str(
                record.get("symbol") or "").upper():
            continue
        account = override.get("account")
        if account and _account_key(account) != _account_key(record.get("account")):
            continue
        wanted_qty = _amount(override.get("quantity"))
        if wanted_qty is not None and abs(abs(wanted_qty) - quantity) > 0.000001:
            continue
        wanted_proceeds = _amount(override.get("proceeds"))
        proceeds = _trade_value(record)
        if (wanted_proceeds is not None and proceeds is not None
                and abs(abs(wanted_proceeds) - proceeds) > 0.02):
            continue
        return override
    return None


def analyze_sales(records: list[dict],
                  sale_basis_overrides: list[dict] | None = None) -> dict:
    """FIFO realised gain/loss using only purchase basis present in the data.

    This is an activity-ledger analysis, not a broker tax-lot statement. A sale
    is labelled unknown when the available files do not contain enough earlier
    purchases in the same account and security to support its full basis.
    """
    trades, reversal_pairs = _uncancelled_trades(records)
    lots: dict[tuple[str, str], list[dict]] = defaultdict(list)
    sales: list[dict] = []
    trade_ledger: list[dict] = []
    for _, record in trades:
        group = group_action(str(record.get("action") or ""))
        qty = _amount(record.get("quantity")) or 0.0
        amount = _amount(record.get("amount")) or 0.0
        key = (_account_key(record.get("account")), _security_key(record))
        if not key[1]:
            continue
        if group in {"buy", "reinvestment"} and qty > 0 and amount <= 0:
            cost = _trade_value(record)
            if cost is not None:
                lots[key].append({"quantity": qty, "cost_per_share": cost / qty})
                # Reinvestments remain valid FIFO lots, but the user-facing
                # transaction ledger is limited to explicit Buy/Sell activity.
                if group == "buy":
                    trade_ledger.append({
                        "date": record.get("date"),
                        "account": record.get("account"),
                        "symbol": record.get("symbol") or record.get("description"),
                        "side": "buy",
                        "quantity": round(qty, 6),
                        "price": _amount(record.get("price")),
                        "amount": round(cost, 2),
                        "proceeds": None,
                        "cost_basis": round(cost, 2),
                        "basis_coverage_pct": None,
                        "basis_source": "trade amount",
                        "realized_gain": None,
                        "gain_pct": None,
                        "outcome": "purchased",
                        "source": record.get("source"),
                    })
            continue
        if group != "sell" or qty >= 0 or amount < 0:
            continue

        sold = abs(qty)
        remaining = sold
        matched_qty = 0.0
        matched_cost = 0.0
        queue = lots[key]
        while remaining > 0.000001 and queue:
            lot = queue[0]
            used = min(remaining, lot["quantity"])
            matched_qty += used
            matched_cost += used * lot["cost_per_share"]
            remaining -= used
            lot["quantity"] -= used
            if lot["quantity"] <= 0.000001:
                queue.pop(0)

        proceeds = _trade_value(record)
        coverage = min(1.0, matched_qty / sold) if sold else 0.0
        complete = coverage >= 0.999999 and proceeds is not None
        cost_basis = matched_cost if complete else None
        basis_source = "recorded buys" if complete else None
        override = _sale_basis_override(record, sold, sale_basis_overrides or [])
        basis_lots = []
        if override is not None:
            if override.get("cost_basis_total") is not None:
                cost_basis = abs(float(override["cost_basis_total"]))
            elif override.get("cost_per_share") is not None:
                cost_basis = abs(float(override["cost_per_share"]) * sold)
            if cost_basis is not None:
                complete = proceeds is not None
                coverage = 1.0
                basis_source = "owner supplied"
                basis_lots = override.get("lots") or []
        gain = round(proceeds - cost_basis, 2) if complete else None
        gain_pct = (gain / cost_basis if complete and cost_basis else None)
        if gain is None:
            outcome = "basis unknown" if matched_qty == 0 else "partial basis"
        elif gain > 0.005:
            outcome = "profit"
        elif gain < -0.005:
            outcome = "loss"
        else:
            outcome = "break-even"
        sale = {
            "date": record.get("date"),
            "account": record.get("account"),
            "symbol": record.get("symbol") or record.get("description"),
            "quantity": round(sold, 6),
            "price": _amount(record.get("price")),
            "proceeds": round(proceeds, 2) if proceeds is not None else None,
            "cost_basis": round(cost_basis, 2) if complete else None,
            "basis_coverage_pct": round(coverage, 6),
            "basis_source": basis_source,
            "basis_lots": basis_lots,
            "realized_gain": gain,
            "gain_pct": gain_pct,
            "outcome": outcome,
            "source": record.get("source"),
        }
        sales.append(sale)
        trade_ledger.append({**sale, "side": "sell", "amount": sale["proceeds"]})

    known = [sale for sale in sales if sale["realized_gain"] is not None]
    total_cost = round(sum(sale["cost_basis"] for sale in known), 2)
    realized = round(sum(sale["realized_gain"] for sale in known), 2)
    buys = [trade for trade in trade_ledger if trade["side"] == "buy"]
    return {
        "trades": trade_ledger,
        "trade_count": len(trade_ledger),
        "buy_count": len(buys),
        "buy_total": round(sum(trade["amount"] or 0 for trade in buys), 2),
        "sales": sales,
        "sales_count": len(sales),
        "sales_proceeds": round(sum(sale["proceeds"] or 0 for sale in sales), 2),
        "sales_with_known_basis": len(known),
        "sales_basis_coverage_pct": (len(known) / len(sales) if sales else None),
        "realized_cost_basis": total_cost,
        "realized_gain": realized if known else None,
        "realized_gain_pct": (realized / total_cost if total_cost else None),
        "profitable_sales": sum(sale["outcome"] == "profit" for sale in sales),
        "loss_sales": sum(sale["outcome"] == "loss" for sale in sales),
        "break_even_sales": sum(sale["outcome"] == "break-even" for sale in sales),
        "unknown_sales": sum(sale["realized_gain"] is None for sale in sales),
        "reversed_trade_pairs_excluded": reversal_pairs,
        "realized_basis_method": "FIFO using purchase lots present in imported activity",
    }


def _month_index(m: str) -> int:
    y, mm = m.split("-")
    return int(y) * 12 + int(mm)


def analyze(rows: list[list], col: dict[str, int]) -> dict:
    by_group: dict[str, list] = defaultdict(lambda: [0, 0.0])
    by_account: dict[str, float] = defaultdict(float)
    hsa_by_month: dict[str, float] = defaultdict(float)
    loan_by_month: dict[str, float] = defaultdict(float)
    months: set[str] = set()
    n = 0
    for r in rows:
        if len(r) <= col["amount"]:
            continue
        month = _month(str(r[col["date"]]))
        if month is None:
            continue
        amt = _amount(r[col["amount"]])
        action = str(r[col["action"]])
        g = group_action(action)
        months.add(month)
        n += 1
        by_group[g][0] += 1
        if amt is None:
            continue
        by_group[g][1] += amt
        if "account" in col and col["account"] < len(r):
            by_account[str(r[col["account"]]).strip()[:40]] += amt
        if g in SPENDING and amt < 0:
            hsa_by_month[month] += -amt
        if g == "loan repayment":
            loan_by_month[month] += amt

    ms = sorted(months)
    n_months = len(ms)
    full = ms[1:-1] if len(ms) > 2 else ms

    def g_total(name):
        return round(by_group.get(name, [0, 0.0])[1], 2)

    hsa_full = [hsa_by_month.get(m, 0.0) for m in full]
    hsa_monthly = round(sum(hsa_full) / len(full), 2) if full else 0.0
    loan_repay = g_total("loan repayment")

    # A loan that stopped being repaid well before the history ends was paid
    # off, not merely inactive — a live loan is repaid every pay period.
    loan_months = sorted(loan_by_month)
    loan_active = False
    loan_last = loan_final = None
    gap = None
    if loan_months and ms:
        loan_last = loan_months[-1]
        gap = _month_index(ms[-1]) - _month_index(loan_last)
        loan_active = gap <= 2
        loan_final = round(loan_by_month[loan_last], 2)
    # regular cadence = the median month; a payoff shows up as a big final month
    ongoing = [loan_by_month[m] for m in loan_months[:-1]]
    typical_repay = (sorted(ongoing)[len(ongoing) // 2] if ongoing else 0.0)
    return {
        "n_transactions": n, "n_months": n_months,
        "first_month": ms[0] if ms else None, "last_month": ms[-1] if ms else None,
        "full_months": full,
        "groups": [{"name": k, "n": v[0], "total": round(v[1], 2)}
                   for k, v in sorted(by_group.items(), key=lambda kv: -abs(kv[1][1]))],
        "by_account": {k: round(v, 2) for k, v in by_account.items()},
        "hsa_spending_by_month": {m: round(v, 2) for m, v in sorted(hsa_by_month.items())},
        "hsa_spending_monthly": hsa_monthly,
        "contributions_total": g_total("contribution"),
        "contributions_monthly": (round(g_total("contribution") / len(full), 2)
                                  if full else 0.0),
        "dividends_total": g_total("dividend / interest"),
        "rsu_espp_total": g_total("shares deposited (RSU/ESPP)"),
        "rollover_total": g_total("withdrawal / rollover"),
        "loan_repayment_total": loan_repay,
        "loan_repayment_monthly": round(loan_repay / len(full), 2) if full else 0.0,
        "has_plan_loan": by_group.get("loan repayment", [0])[0] > 0,
        "loan_active": loan_active,
        "loan_last_repayment_month": loan_last,
        "loan_months_since_last_repayment": gap,
        "loan_final_payment": loan_final,
        "loan_typical_monthly_repayment": round(typical_repay, 2),
        "loan_payoff_detected": bool(loan_months) and not loan_active,
        "loan_by_month": {m: round(v, 2) for m, v in sorted(loan_by_month.items())},
        "fees_total": round(g_total("fee") + g_total("loan fee"), 2),
    }


if __name__ == "__main__":
    hdr = ["Run Date", "Account", "Account Number", "Action", "Symbol",
           "Description", "Type", "Price ($)", "Quantity", "Commission ($)",
           "Fees ($)", "Accrued Interest ($)", "Amount ($)", "Settlement Date"]
    col = find_columns(hdr)
    assert col == {"date": 0, "account": 1, "action": 3, "symbol": 4,
                   "description": 5, "type": 6, "price": 7, "quantity": 8,
                   "amount": 12}, col
    assert group_action("LOAN REPAYMENT") == "loan repayment"
    assert group_action("PARTIC CONTR CURRENT PARTICIPANT") == "contribution"
    assert group_action("DEBIT CARD PURCHASE MAIN STREET PHARMACY") == "HSA / medical spending"
    assert group_action("BILL PAYMENT SOME CLINIC (Cash)") == "HSA / medical spending"
    assert group_action("DIVIDEND RECEIVED FIDELITY GOVT") == "dividend / interest"
    assert group_action("CONVERSION SHARES DEPOSITED ACME CORP") == "shares deposited (RSU/ESPP)"
    assert group_action("Withdrawals") == "withdrawal / rollover"

    def row(date, acct, action, amount):
        r = [""] * 14
        r[0], r[1], r[3], r[12] = date, acct, action, amount
        return r

    rows = [
        row("01/05/2025", "HSA", "DEBIT CARD PURCHASE PHARMACY", "-100.00"),
        row("02/05/2025", "HSA", "DEBIT CARD PURCHASE PHARMACY", "-200.00"),
        row("02/20/2025", "HSA", "BILL PAYMENT CLINIC (Cash)", "-50.00"),
        row("03/05/2025", "HSA", "DEBIT CARD PURCHASE PHARMACY", "-300.00"),
        row("02/15/2025", "401K", "Contributions", "1000.00"),
        row("02/16/2025", "401K", "Loan Repayments", "250.00"),
        row("02/17/2025", "401K", "Withdrawals", "-500000.00"),
        row("02/18/2025", "401K", "Change on Market Value", "9999.00"),
    ]
    out = analyze(rows, col)
    assert out["n_months"] == 3, out["n_months"]
    # only February is a full month; HSA spend there is 200 + 50
    assert out["hsa_spending_monthly"] == 250.0, out["hsa_spending_monthly"]
    assert out["has_plan_loan"] is True
    assert out["loan_repayment_total"] == 250.0
    # repaid in Feb, history runs to Mar -> only a 1 month gap, still live
    assert out["loan_active"] is True, out

    # repayments stopping long before the history ends means it was paid off
    paid = [
        row("08/15/2025", "401K", "Loan Repayments", "500.00"),
        row("09/04/2025", "401K", "Loan Repayments", "4290.00"),
        row("10/03/2025", "401K", "LOAN MAINT. FEE", "-0.60"),
        row("01/15/2026", "401K", "Contributions", "1000.00"),
        row("06/15/2026", "401K", "Contributions", "1000.00"),
    ]
    po = analyze(paid, col)
    assert po["loan_active"] is False, po
    assert po["loan_payoff_detected"] is True
    assert po["loan_last_repayment_month"] == "2025-09"
    assert po["loan_months_since_last_repayment"] == 9, po
    assert po["loan_final_payment"] == 4290.0
    assert po["loan_typical_monthly_repayment"] == 500.0
    assert out["rollover_total"] == -500000.0     # reported, never spending
    assert out["contributions_total"] == 1000.0

    trades = [
        {"date": "2025-01-02", "account": "IRA(*1234)", "action": "Buy",
         "symbol": "ABC", "quantity": 10, "price": 10, "amount": -100},
        {"date": "2025-02-02", "account": "IRA(*1234)", "action": "Buy",
         "symbol": "ABC", "quantity": 10, "price": 12, "amount": -120},
        {"date": "2025-03-02", "account": "IRA(*1234)", "action": "Sell",
         "symbol": "ABC", "quantity": -15, "price": 15, "amount": 225},
        # Exact sale/correction pair: neither row is a realised sale.
        {"date": "2025-03-03", "account": "IRA(*1234)", "action": "Sell",
         "symbol": "ABC", "quantity": -2, "price": 14, "amount": 28},
        {"date": "2025-03-04", "account": "IRA(*1234)", "action": "Sell",
         "symbol": "ABC", "quantity": 2, "price": None, "amount": -28},
        # No recorded purchase lot for XYZ, so basis must not be invented.
        {"date": "2025-04-02", "account": "IRA(*1234)", "action": "Sell",
         "symbol": "XYZ", "quantity": -3, "price": 20, "amount": 60},
    ]
    sale_result = analyze_records(trades)
    assert sale_result["trade_count"] == 4, sale_result["trades"]
    assert sale_result["buy_count"] == 2
    assert sale_result["buy_total"] == 220.0
    assert sale_result["sales_count"] == 2, sale_result["sales"]
    assert sale_result["reversed_trade_pairs_excluded"] == 1
    assert sale_result["sales_with_known_basis"] == 1
    assert sale_result["sales"][0]["cost_basis"] == 160.0
    assert sale_result["sales"][0]["realized_gain"] == 65.0
    assert sale_result["sales"][0]["outcome"] == "profit"
    assert sale_result["sales"][1]["outcome"] == "basis unknown"
    overridden = analyze_records(trades, [{
        "date": "2025-04-02", "account": "IRA(*1234)", "symbol": "XYZ",
        "quantity": 3, "proceeds": 60, "cost_per_share": 25,
        "lots": [{"acquired": "2024-12-01", "quantity": 3, "cost_basis": 75}],
    }])
    assert overridden["sales"][1]["cost_basis"] == 75.0
    assert overridden["sales"][1]["realized_gain"] == -15.0
    assert overridden["sales"][1]["outcome"] == "loss"
    assert overridden["sales"][1]["basis_source"] == "owner supplied"
    assert len(overridden["sales"][1]["basis_lots"]) == 1
    print("investments self-test OK")
