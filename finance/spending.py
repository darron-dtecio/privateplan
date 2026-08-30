"""Turn a checking-account transaction ledger into monthly spending.

Raw outflow is NOT spending: money moved to a brokerage, paid to yourself, or
sent between your own accounts is saving, and it usually dwarfs real
consumption. This module classifies every row into income / transfer /
spending, nets transfers in both directions, and reports what is actually
consumed per month.

Credit-card payments ARE counted as spending: the underlying purchases never
appear in checking, so the payment is the only visible proxy for them.

Expects columns DATE, DESCRIPTION, AMOUNT (negative = outflow); tolerates
extra columns. Descriptions arrive already redacted by redact.py.
"""

from __future__ import annotations

import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# (category, pattern). First match wins, so order matters: the most specific
# rules come first and TRANSFER/INCOME rules precede the spending catch-alls.
RULES: list[tuple[str, re.Pattern]] = [
    # --- money that is not consumption -------------------------------------
    ("transfer", re.compile(
        r"fid\s*bkg|fidelity|vanguard|schwab|merrill|wells\s*fargo\s*(inv|adv)|"
        r"inst\s*xfer|instant\s*xfer|online\s*transfer|recurring\s*transfer|"
        r"transfer\s*(to|from)\b|\bxfer\b|brokerage|e\*?trade|"
        r"share\s*builder|robinhood|coinbase", re.I)),
    ("income", re.compile(
        r"edipayment|direct\s*dep|payroll|\bdeposit\b|tax\s*ref|castaxrfd|"
        r"casttaxrfd|treas\s*310|interest\s*paid|dividend|refund|reversal|"
        r"cash\s*back\s*reward|zelle\s*from|venmo\s*from", re.I)),
    # --- housing -------------------------------------------------------------
    ("mortgage", re.compile(r"pennymac|mortgage|loan\s*serv|rocket\s*mtg|"
                            r"wells\s*fargo\s*home|chase\s*home", re.I)),
    ("property tax / insurance", re.compile(
        r"county\s*tax|property\s*tax|tax\s*collector|state\s*farm|allstate|"
        r"geico|farmers\s*ins|insurance|\bins\s*prem", re.I)),
    # --- utilities & services -------------------------------------------------
    ("utilities", re.compile(
        r"edison|so\s*cal\s*gas|socalgas|water|sewer|waste|disposal|"
        r"electric|\bgas\s*co\b|utility|\bpg&?e\b", re.I)),
    ("phone / internet / TV", re.compile(
        r"verizon|at&?t|t-?mobile|spectrum|comcast|xfinity|cox\s*comm|"
        r"frontier|starlink|\bphone\b|internet|directv|dish\s*net", re.I)),
    ("subscriptions", re.compile(
        r"recurring\s*payment|netflix|spotify|hulu|disney|apple\.com|"
        r"amazon\s*prime|prime\s*video|amazon\s*digital|adobe|microsoft\s*365|"
        r"msbill|youtube|patreon|audible|subscription|zwift|equifax|"
        r"\bnyt\b|wsj|sirius", re.I)),
    ("shopping / retail", re.compile(
        r"amazon|amzn|ebay|etsy|best\s*buy|home\s*depot|lowes|ikea|"
        r"nordstrom|macy|kohl|wayfair|chewy|\brei\b", re.I)),
    ("fitness / wellness", re.compile(
        r"pilates|yoga|gym|fitness|peloton|lifetime\s*ath|24\s*hour\s*fit|"
        r"crossfit|golf|spa\b", re.I)),
    # --- card payments (proxy for card spending) --------------------------------
    ("credit card payment", re.compile(
        r"american\s*express|amex|citi\s*card|chase\s*card|discover|"
        r"capital\s*one|card\s*(online\s*)?payment|bank\s*card|visa\s*payment|"
        r"mastercard", re.I)),
    # --- everyday ----------------------------------------------------------------
    ("pets / veterinary", re.compile(
        r"\bvca\b|animal\s*hosp|veterinar|\bvet\b|petco|petsmart|chewy", re.I)),
    ("home improvement / repairs", re.compile(
        r"air\s*condition|\bhvac\b|furnace|tile\b|futon|furniture|"
        r"flooring|window\s*(co|world)|solar|remodel|contractor|lumber", re.I)),
    ("home & yard services", re.compile(
        r"pool|landscap|gardener|lawn|pest|termite|orkin|cleaning|maid|"
        r"handyman|plumb|roofing|wood", re.I)),
    ("groceries", re.compile(
        r"grocer|safeway|kroger|ralphs|vons|albertsons|trader\s*joe|"
        r"whole\s*foods|sprouts|costco|sam'?s\s*club|walmart|target", re.I)),
    ("dining", re.compile(r"restaurant|starbucks|coffee|pizza|grill|cafe|"
                          r"doordash|ubereats|grubhub|taco|sushi|bar\s*&|"
                          r"^tst\*|toast\s*pos|kitchen|tavern|baker|brewing|"
                          r"taqueria|bistro|deli\b", re.I)),
    ("travel", re.compile(
        r"airlines|air\s*lines|\bunited\b|southwest\s*air|delta\s*air|alaska\s*air|"
        r"marriott|courtyard|hilton|hyatt|airbnb|vrbo|hertz|avis|"
        r"enterprise\s*rent|expedia|booking\.com|priceline|hotel|resort", re.I)),
    ("clothing", re.compile(
        r"vuori|\bgap\s*online|old\s*navy|lululemon|voler|patagonia|"
        r"north\s*face|vans\s*#|nordstrom|clothing|apparel|shopjoole", re.I)),
    ("fuel / auto", re.compile(r"chevron|shell\s*(oil|service)|arco|76\s*gas|mobil|"
                               r"exxon|gas\s*station|\bdmv\b|auto\s*repair|tire|"
                               r"jiffy\s*lube|car\s*wash|safelite|auto\s*glass|"
                               r"cdjr|dealership", re.I)),
    ("medical", re.compile(r"pharmacy|cvs|walgreens|medical|dental|dentist|"
                           r"clinic|hospital|physician|optometr|health|"
                           r"body\s*sculpt|plastic\s*surg|dermatolog|surger|"
                           r"orthodon|\bmd\b|eyebuydirect", re.I)),
    ("cash / checks", re.compile(r"^check\b|atm\s*withdraw|cash\s*withdraw|"
                                 r"withdrawal", re.I)),
    ("bill pay (other)", re.compile(r"bill\s*pay", re.I)),
    ("card purchases", re.compile(r"^purchase|debit\s*card|pos\s*purchase|"
                                  r"point\s*of\s*sale", re.I)),
]

NON_SPENDING = {"transfer", "income"}

# Card issuers we can recognise in a checking description, so a payment to a
# card can be cancelled out when we also hold that card's own statement.
ISSUERS: dict[str, re.Pattern] = {
    "amex": re.compile(r"american\s*express|amex", re.I),
    "citi": re.compile(r"citi\s*card|citibank", re.I),
    "chase": re.compile(r"chase\s*card|chase\s*credit", re.I),
    "discover": re.compile(r"discover", re.I),
    "capitalone": re.compile(r"capital\s*one", re.I),
}
# rows on a card statement that are the payment arriving, not a purchase
CARD_PAYMENT = re.compile(r"payment\s*-?\s*thank\s*you|^\s*payment\b|autopay|"
                          r"online\s*payment", re.I)


def issuer_of(description: str) -> str | None:
    for name, rx in ISSUERS.items():
        if rx.search(description):
            return name
    return None


def classify(description: str) -> str:
    for cat, rx in RULES:
        if rx.search(description):
            return cat
    return "other / uncategorized"


def _month(date_str: str) -> str | None:
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", date_str.strip())
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_str.strip())
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _amount(raw) -> float | None:
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def analyze(rows: list[list], col: dict[str, int]) -> dict:
    """rows: data rows (no header). col: {'date':i,'desc':i,'amount':i}."""
    by_month_cat: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_month_in: dict[str, float] = defaultdict(float)
    cat_totals: dict[str, float] = defaultdict(float)
    cat_counts: dict[str, int] = defaultdict(int)
    card_pay: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    one_offs: list[dict] = []
    desc_months: dict[str, set] = defaultdict(set)
    transfers_net = 0.0
    n = 0

    for r in rows:
        if len(r) <= max(col.values()):
            continue
        month = _month(str(r[col["date"]]))
        amt = _amount(r[col["amount"]])
        if month is None or amt is None:
            continue
        desc = re.sub(r"\s{2,}", " ", str(r[col["desc"]])).strip()
        cat = classify(desc)
        n += 1
        if cat == "transfer":
            transfers_net += amt
            continue
        if amt > 0:
            by_month_in[month] += amt
            if cat != "income":       # a refund inside a spending category
                by_month_cat[month][cat] -= amt
                cat_totals[cat] -= amt
            continue
        if cat == "income":           # e.g. a reversed deposit
            continue
        spend = -amt
        by_month_cat[month][cat] += spend
        cat_totals[cat] += spend
        cat_counts[cat] += 1
        desc_months[_desc_key(desc)].add(month)
        if cat == "credit card payment":
            iss = issuer_of(desc)
            if iss:
                card_pay[iss][month] += spend
        elif spend >= ONE_OFF_THRESHOLD:
            one_offs.append({"month": month, "category": cat,
                             "amount": round(spend, 2), "description": desc[:48]})

    months = sorted(by_month_cat)
    if not months:
        return {"months": [], "n_transactions": n, "one_offs": [],
                "one_off_monthly": 0.0, "typical_monthly": 0.0}

    # partial first/last months would drag the average down
    full = months[1:-1] if len(months) > 2 else months
    monthly_total = {m: round(sum(by_month_cat[m].values()), 2) for m in months}
    totals_full = [monthly_total[m] for m in full]
    mortgage_by_month = {m: by_month_cat[m].get("mortgage", 0.0) for m in months}

    avg = statistics.mean(totals_full)
    med = statistics.median(totals_full)
    avg_mortgage = statistics.mean([mortgage_by_month[m] for m in full])

    # last 12 full months, to catch a spending level that has drifted
    recent = full[-12:]
    avg_recent = statistics.mean([monthly_total[m] for m in recent])
    avg_mortgage_recent = statistics.mean([mortgage_by_month[m] for m in recent])

    # per-category monthly averages must use the same full-month basis as the
    # headline average, or the two will not add up
    full_set = set(full)
    cat_full: dict[str, float] = defaultdict(float)
    for m in full:
        for c, v in by_month_cat[m].items():
            cat_full[c] += v
    cats = sorted(cat_full.items(), key=lambda kv: -kv[1])
    result = {
        "months": months, "n_months": len(months), "n_full_months": len(full),
        "n_transactions": n,
        "first_month": months[0], "last_month": months[-1],
        "monthly_total": monthly_total,
        "monthly_income": {m: round(by_month_in.get(m, 0.0), 2) for m in months},
        "categories": [{"name": c, "total": round(cat_totals[c], 2),
                        "monthly": round(t / max(len(full), 1), 2),
                        "n": cat_counts.get(c, 0)} for c, t in cats],
        "avg_monthly": round(avg, 2),
        "median_monthly": round(med, 2),
        "avg_monthly_recent12": round(avg_recent, 2),
        "avg_monthly_mortgage": round(avg_mortgage, 2),
        "avg_monthly_mortgage_recent12": round(avg_mortgage_recent, 2),
        "avg_monthly_ex_mortgage": round(avg - avg_mortgage, 2),
        "transfers_net": round(transfers_net, 2),
        "card_payments_by_issuer": {i: dict(d) for i, d in card_pay.items()},
        "by_month_categories": {m: {k: round(v, 2) for k, v in by_month_cat[m].items()}
                                for m in months},
    }
    return add_oneoff_metrics(result, one_offs, desc_months)


# A single large purchase — a new HVAC system, an elective procedure — is real
# money but it is not the monthly run rate. Averaging it in overstates the
# retirement baseline for the next thirty years, so it is reported separately
# as an annualised allowance.
ONE_OFF_THRESHOLD = 2_500.0
# Categories that are large every single month by nature — a mortgage payment
# is not a windfall expense no matter how big it is.
ALWAYS_RECURRING = {"mortgage", "credit card payment", "property tax / insurance"}


def _desc_key(description: str) -> str:
    """Collapse statement noise so repeats of the same payee group together."""
    return re.sub(r"[\d#*]+", "", description).strip().lower()[:24]


def add_oneoff_metrics(result: dict, one_offs: list[dict],
                       desc_months: dict[str, set] | None = None) -> dict:
    """Split the average into a recurring run rate plus a lumpy allowance.

    A transaction only counts as one-off if it is both large AND infrequent:
    anything whose payee shows up in a quarter of the months on file is part
    of the run rate, however big it is.
    """
    months = result["months"]
    full = set(months[1:-1] if len(months) > 2 else months)
    n_full = max(len(full), 1)
    recur_cut = max(3, len(months) // 4)
    if desc_months is not None:
        one_offs = [o for o in one_offs
                    if len(desc_months.get(_desc_key(o["description"]), ())) < recur_cut]
    one_offs = [o for o in one_offs if o["category"] not in ALWAYS_RECURRING]
    in_window = [o for o in one_offs if o["month"] in full]
    total = sum(o["amount"] for o in in_window)
    result["one_offs"] = sorted(one_offs, key=lambda o: -o["amount"])[:25]
    result["one_off_count"] = len(one_offs)
    result["one_off_total"] = round(total, 2)
    result["one_off_monthly"] = round(total / n_full, 2)
    result["typical_monthly"] = round(result["avg_monthly"] - total / n_full, 2)

    # A parallel view with the lumpy items stripped out, so planning can run on
    # the recurring run rate instead of an average distorted by a new furnace.
    ex = {m: dict(cats) for m, cats in result["by_month_categories"].items()}
    for o in one_offs:
        m, c = o["month"], o["category"]
        if m in ex and c in ex[m]:
            ex[m][c] = round(ex[m][c] - o["amount"], 2)
            if abs(ex[m][c]) < 0.01:
                del ex[m][c]
    rec = _summarize(ex, {c["name"]: c["n"] for c in result["categories"]})
    result["recurring"] = {
        "avg_monthly": rec["avg_monthly"],
        "median_monthly": rec["median_monthly"],
        "avg_monthly_recent12": rec["avg_monthly_recent12"],
        "avg_monthly_mortgage": rec["avg_monthly_mortgage"],
        "avg_monthly_ex_mortgage": rec["avg_monthly_ex_mortgage"],
        "monthly_total": rec["monthly_total"],
        "categories": rec["categories"],
    }
    return result


def _summarize(by_month_cat: dict[str, dict[str, float]],
               cat_counts: dict[str, int] | None = None) -> dict:
    """Recompute headline figures from a month -> category -> amount map."""
    months = sorted(by_month_cat)
    full = months[1:-1] if len(months) > 2 else months
    monthly_total = {m: round(sum(by_month_cat[m].values()), 2) for m in months}
    totals_full = [monthly_total[m] for m in full] or [0.0]
    mortgage_by_month = {m: by_month_cat[m].get("mortgage", 0.0) for m in months}
    cat_full: dict[str, float] = defaultdict(float)
    cat_all: dict[str, float] = defaultdict(float)
    for m in months:
        for c, v in by_month_cat[m].items():
            cat_all[c] += v
            if m in full:
                cat_full[c] += v
    avg = statistics.mean(totals_full)
    avg_mortgage = (statistics.mean([mortgage_by_month[m] for m in full])
                    if full else 0.0)
    recent = full[-12:]
    return {
        "months": months, "n_months": len(months), "n_full_months": len(full),
        "first_month": months[0] if months else None,
        "last_month": months[-1] if months else None,
        "monthly_total": monthly_total,
        "categories": [{"name": c, "total": round(cat_all[c], 2),
                        "monthly": round(t / max(len(full), 1), 2),
                        "n": (cat_counts or {}).get(c, 0)}
                       for c, t in sorted(cat_full.items(), key=lambda kv: -kv[1])],
        "avg_monthly": round(avg, 2),
        "median_monthly": round(statistics.median(totals_full), 2),
        "avg_monthly_recent12": round(
            statistics.mean([monthly_total[m] for m in recent]) if recent else avg, 2),
        "avg_monthly_mortgage": round(avg_mortgage, 2),
        "avg_monthly_mortgage_recent12": round(
            statistics.mean([mortgage_by_month[m] for m in recent]) if recent
            else avg_mortgage, 2),
        "avg_monthly_ex_mortgage": round(avg - avg_mortgage, 2),
        "by_month_categories": {m: {k: round(v, 2) for k, v in by_month_cat[m].items()}
                                for m in months},
    }


def analyze_card(rows: list[list], col: dict[str, int], label: str) -> dict:
    """A card statement: positive = purchase, negative = payment or refund."""
    by_month_cat: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    cat_counts: dict[str, int] = defaultdict(int)
    payments: dict[str, float] = defaultdict(float)
    one_offs: list[dict] = []
    desc_months: dict[str, set] = defaultdict(set)
    refunds = 0.0
    n = 0
    for r in rows:
        if len(r) <= max(col.values()):
            continue
        month = _month(str(r[col["date"]]))
        amt = _amount(r[col["amount"]])
        if month is None or amt is None:
            continue
        desc = re.sub(r"\s{2,}", " ", str(r[col["desc"]])).strip()
        n += 1
        if CARD_PAYMENT.search(desc):
            payments[month] += -amt        # payments arrive as negatives
            continue
        cat = classify(desc)
        if cat in NON_SPENDING:            # a card statement has no transfers
            cat = "other / uncategorized"
        by_month_cat[month][cat] += amt    # refunds (negative) net down the category
        if amt > 0:
            cat_counts[cat] += 1
            desc_months[_desc_key(desc)].add(month)
            if amt >= ONE_OFF_THRESHOLD:
                one_offs.append({"month": month, "category": cat,
                                 "amount": round(amt, 2), "description": desc[:48]})
        else:
            refunds += -amt
    out = add_oneoff_metrics(_summarize(by_month_cat, cat_counts), one_offs,
                             desc_months)
    out.update({"label": label, "n_transactions": n,
                "payments_by_month": {m: round(v, 2) for m, v in sorted(payments.items())},
                "payments_total": round(sum(payments.values()), 2),
                "refunds_total": round(refunds, 2)})
    return out


def combine(bank: dict, cards: list[dict], extra: list[dict] | None = None) -> dict:
    """Merge a checking ledger with card statements and other spending feeds.

    For every month a card statement covers, the payment to that card is
    removed from checking (it is a transfer to the issuer) and the card's own
    purchases are added in its place. Months the statement does not cover keep
    the payment as the best available proxy, so coverage gaps never silently
    drop spending.
    """
    merged: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for m, cats in bank["by_month_categories"].items():
        for c, v in cats.items():
            merged[m][c] += v

    counts = {c["name"]: c["n"] for c in bank["categories"]}
    reconciliation = []
    for card in cards:
        issuer = card.get("issuer")
        covered = set(card["by_month_categories"]) | set(card.get("payments_by_month", {}))
        bank_pay = (bank.get("card_payments_by_issuer") or {}).get(issuer, {})
        offset = 0.0
        for m in covered:
            paid = bank_pay.get(m, 0.0)
            if paid and m in merged:
                merged[m]["credit card payment"] -= paid
                if abs(merged[m]["credit card payment"]) < 0.01:
                    del merged[m]["credit card payment"]
                offset += paid
        for m, cats in card["by_month_categories"].items():
            for c, v in cats.items():
                merged[m][c] += v
        for c in card["categories"]:
            counts[c["name"]] = counts.get(c["name"], 0) + c["n"]
        bank_total = round(sum(bank_pay.get(m, 0.0) for m in covered), 2)
        reconciliation.append({
            "label": card.get("label", issuer or "card"),
            "issuer": issuer,
            "months_covered": len(card["by_month_categories"]),
            "first_month": card.get("first_month"), "last_month": card.get("last_month"),
            "bank_payments_in_window": bank_total,
            "card_payments_recorded": card.get("payments_total", 0.0),
            "delta": round(bank_total - card.get("payments_total", 0.0), 2),
            "card_net_charges": round(
                sum(sum(c.values()) for c in card["by_month_categories"].values()), 2),
            "payments_removed_from_checking": round(offset, 2),
            "uncovered_months": sorted(set(bank_pay) - covered),
        })

    for src in extra or []:
        cat = src.get("category", "other / uncategorized")
        for m, v in src.get("by_month", {}).items():
            merged[m][cat] += v
        counts[cat] = counts.get(cat, 0) + src.get("n", 0)

    # one-offs survive the merge, except those inside a card's payment proxy
    # (the proxy is removed, so its lumps are replaced by the card's own)
    merged_oneoffs = [o for o in bank.get("one_offs", [])
                      if o["category"] != "credit card payment"]
    for card in cards:
        merged_oneoffs += card.get("one_offs", [])

    out = add_oneoff_metrics(_summarize(merged, counts), merged_oneoffs)
    out.update({
        "n_transactions": bank.get("n_transactions", 0)
        + sum(c.get("n_transactions", 0) for c in cards),
        "transfers_net": bank.get("transfers_net"),
        "monthly_income": bank.get("monthly_income"),
        "reconciliation": reconciliation,
        "sources": ["checking"] + [c.get("label", "card") for c in cards]
        + [s.get("label", "other") for s in (extra or [])],
    })
    return out


def find_columns(header: list) -> dict[str, int] | None:
    texts = [str(c).strip().lower() for c in header]
    col = {}
    for i, t in enumerate(texts):
        if "date" in t and "date" not in col:
            col["date"] = i
        elif ("description" in t or "payee" in t or "memo" in t) and "desc" not in col:
            col["desc"] = i
        elif ("amount" in t or "debit" in t) and "amount" not in col:
            col["amount"] = i
    return col if {"date", "desc", "amount"} <= col.keys() else None


def normalize(rows: list[list], col: dict[str, int]) -> list[dict]:
    """Retain a minimal ledger so overlapping bank exports can be merged."""
    out = []
    for row in rows:
        if len(row) <= max(col.values()):
            continue
        month = _month(str(row[col["date"]]))
        amount = _amount(row[col["amount"]])
        if month is None or amount is None:
            continue
        raw = str(row[col["date"]]).strip()
        m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", raw)
        date = (f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
                if m else raw[:10])
        out.append({"date": date,
                    "description": re.sub(r"\s{2,}", " ",
                                          str(row[col["desc"]])).strip()[:160],
                    "amount": round(amount, 2)})
    return out


def analyze_records(records: list[dict]) -> dict:
    rows = [[r.get("date"), r.get("description"), r.get("amount")]
            for r in records]
    return analyze(rows, {"date": 0, "desc": 1, "amount": 2})


if __name__ == "__main__":
    hdr = ["DATE", "DESCRIPTION", "AMOUNT", "CHECK #", "STATUS"]
    assert find_columns(hdr) == {"date": 0, "desc": 1, "amount": 2}
    assert classify("BILL PAY PENNYMAC LOAN SE") == "mortgage"
    assert classify("FID BKG SVC LLC MONEYLINE 250915") == "transfer"
    assert classify("ACME CORP EDIPAYMENT") == "income"
    assert classify("AMERICAN EXPRESS ACH PMT") == "credit card payment"
    assert classify("PURCHASE") == "card purchases"
    assert classify("SO CAL EDISON CO DIRECTPAY") == "utilities"
    assert classify("RECURRING PAYMENT") == "subscriptions"
    assert classify("SOMETHING UNKNOWN LLC") == "other / uncategorized"
    # transfers must never count as spending, in either direction
    rows = [
        ["01/15/2025", "ACME CORP EDIPAYMENT", "5000.00"],
        ["01/16/2025", "FID BKG SVC LLC MONEYLINE", "-20000.00"],
        ["01/17/2025", "BILL PAY PENNYMAC LOAN SE", "-4000.00"],
        ["01/18/2025", "PURCHASE", "-100.00"],
        ["02/15/2025", "ACME CORP EDIPAYMENT", "5000.00"],
        ["02/17/2025", "BILL PAY PENNYMAC LOAN SE", "-4000.00"],
        ["02/18/2025", "PURCHASE", "-200.00"],
        ["03/17/2025", "BILL PAY PENNYMAC LOAN SE", "-4000.00"],
        ["03/18/2025", "PURCHASE", "-300.00"],
    ]
    out = analyze(rows, {"date": 0, "desc": 1, "amount": 2})
    assert out["n_months"] == 3
    assert out["monthly_total"]["2025-01"] == 4100.0, out["monthly_total"]
    assert out["transfers_net"] == -20000.0
    # only the middle month is "full"
    assert out["avg_monthly"] == 4200.0, out["avg_monthly"]
    assert out["avg_monthly_mortgage"] == 4000.0
    assert out["avg_monthly_ex_mortgage"] == 200.0
    names = [c["name"] for c in out["categories"]]
    assert "transfer" not in names and "income" not in names
    # category monthly averages must reconcile with the headline average
    assert abs(sum(c["monthly"] for c in out["categories"])
               - out["avg_monthly"]) < 0.01, out["categories"]
    assert classify("AMAZON MARKETPLACE") == "shopping / retail"
    assert classify("PRIME VIDEO CHANNELS") == "subscriptions"
    assert classify("CLR*CLUBPILATES71420") == "fitness / wellness"
    assert classify("ORKIN LLC 002") == "home & yard services"
    assert issuer_of("AMERICAN EXPRESS ACH PMT 251010") == "amex"
    assert issuer_of("CITI CARD ONLINE PAYMENT") == "citi"

    # ---- card statement + reconciliation ----------------------------------
    bank_rows = [
        ["01/10/2025", "AMERICAN EXPRESS ACH PMT", "-1000.00"],
        ["01/12/2025", "PURCHASE", "-100.00"],
        ["02/10/2025", "AMERICAN EXPRESS ACH PMT", "-2000.00"],
        ["02/12/2025", "PURCHASE", "-100.00"],
        ["03/10/2025", "AMERICAN EXPRESS ACH PMT", "-3000.00"],
        ["03/12/2025", "PURCHASE", "-100.00"],
    ]
    bank = analyze(bank_rows, {"date": 0, "desc": 1, "amount": 2})
    assert bank["card_payments_by_issuer"]["amex"]["2025-02"] == 2000.0

    card_rows = [
        ["02/03/2025", "AMAZON MARKETPLACE", "900.00"],
        ["02/04/2025", "NETFLIX.COM", "20.00"],
        ["02/05/2025", "AMAZON MARKETPLACE", "-50.00"],   # refund nets down
        ["02/20/2025", "MOBILE PAYMENT - THANK YOU", "-2000.00"],
    ]
    card = analyze_card(card_rows, {"date": 0, "desc": 1, "amount": 2}, "AmEx")
    card["issuer"] = "amex"
    assert card["payments_total"] == 2000.0
    assert card["by_month_categories"]["2025-02"]["shopping / retail"] == 850.0

    comb = combine(bank, [card])
    feb = comb["by_month_categories"]["2025-02"]
    # the $2,000 payment is gone; the card's own $870 of purchases replaces it
    assert "credit card payment" not in feb, feb
    assert round(sum(feb.values()), 2) == 970.0, feb   # 100 debit + 870 card
    # months the statement does not cover keep the payment proxy
    assert comb["by_month_categories"]["2025-01"]["credit card payment"] == 1000.0
    rec = comb["reconciliation"][0]
    assert rec["bank_payments_in_window"] == 2000.0
    assert rec["delta"] == 0.0
    assert rec["uncovered_months"] == ["2025-01", "2025-03"], rec["uncovered_months"]

    # extra feeds (HSA) simply add
    comb2 = combine(bank, [card], extra=[{"label": "HSA", "category": "medical",
                                          "by_month": {"2025-02": 300.0}, "n": 3}])
    assert comb2["by_month_categories"]["2025-02"]["medical"] == 300.0

    # ---- one-off separation ------------------------------------------------
    lumpy = [
        ["01/05/2025", "PURCHASE", "-100.00"],
        ["02/05/2025", "PURCHASE", "-100.00"],
        ["02/09/2025", "SUMMIT HVAC REPLACEMENT", "-28000.00"],
        ["03/05/2025", "PURCHASE", "-100.00"],
    ]
    lo = analyze(lumpy, {"date": 0, "desc": 1, "amount": 2})
    # February is the only full month: 28,100 total, of which 28,000 is one-off
    assert lo["avg_monthly"] == 28100.0, lo["avg_monthly"]
    assert lo["one_off_monthly"] == 28000.0, lo["one_off_monthly"]
    assert lo["typical_monthly"] == 100.0, lo["typical_monthly"]
    assert lo["one_offs"][0]["category"] == "home improvement / repairs"

    # a large payment that recurs every month is the run rate, not a one-off
    recurring = [["%02d/05/2025" % m, "BILL PAY PENNYMAC LOAN SE", "-4000.00"]
                 for m in range(1, 13)]
    recurring.append(["06/09/2025", "SUMMIT HVAC", "-28000.00"])
    ro = analyze(recurring, {"date": 0, "desc": 1, "amount": 2})
    assert [o["description"] for o in ro["one_offs"]] == ["SUMMIT HVAC"], ro["one_offs"]
    assert ro["one_off_total"] == 28000.0, ro["one_off_total"]
    # the recurring view strips the lump but keeps every mortgage payment
    assert ro["recurring"]["avg_monthly"] == 4000.0, ro["recurring"]["avg_monthly"]
    assert ro["recurring"]["avg_monthly_mortgage"] == 4000.0
    assert ro["recurring"]["avg_monthly_ex_mortgage"] == 0.0
    assert classify("SUMMIT HVAC 123 SPRINGFIELD IL") == "home improvement / repairs"
    assert classify("UNITED AIRLINES HOUSTON TX") == "travel"
    assert classify("AplPay VCA ANIMAL HOLOS ANGELES CA") == "pets / veterinary"
    assert classify("TST* CORNER BAKERY DOWNTOWN") == "dining"
    assert classify("ATHENIX BODY SCULPTIIRVINE CA") == "medical"
    assert classify("SAFELITE AUTO GLASS") == "fuel / auto"
    print("spending self-test OK")
