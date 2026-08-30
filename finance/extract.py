"""Parse documents in finance_data/inbox/ into structured extraction JSON.

Every parsed page/table/grid passes through redact.py BEFORE anything is
stored or extracted, so identity PII (SSN, names, addresses, phones, emails,
DOB, employee/account numbers) never persists. Financial values do persist
and appear in diagnostics — that's intentional (user policy 2026-08-01).

Per-document output: finance_data/extracted/<slug>.json (redacted).
Merged best-values:   finance_data/extracted/summary.json (redacted).
Diagnostics:          finance_data/extracted/diagnostics.json + stdout.
After merging, the extraction is auto-applied to profile.json (autoprofile).
"""

from __future__ import annotations

import csv as csvmod
import hashlib
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
from common import EXTRACTED, INBOX, diag, field

import investments
import redact
import spending
import sources  # pipeline/sources.py (sniff_format)
import vesting


MAX_PAGES = 40
AMOUNT = r"\$?\s*(-?[\d]{1,3}(?:,\d{3})*(?:\.\d{1,2})?)"
DATE = r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def _amt(s) -> float | None:
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return float(str(s).replace(",", "").replace("$", "").strip().rstrip("-"))
    except (ValueError, AttributeError):
        return None


# ------------------------------------------------------------------ parsing --
def parse_pdf_pages(content: bytes) -> tuple[list[str], list[list[list[str]]]]:
    import pdfplumber
    pages, tables = [], []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages[:MAX_PAGES]:
            pages.append(page.extract_text() or "")
            for t in page.extract_tables():
                tables.append([[c or "" for c in row] for row in t[:120]])
    return pages, tables


# Row caps must clear a multi-year transaction ledger; a 25-month checking
# export runs well over a thousand rows and silently truncating it produces a
# spending average computed from a fraction of the history.
MAX_ROWS = 20_000


def parse_xlsx_grids(content: bytes) -> dict[str, list[list]]:
    import openpyxl
    import ir_ingest
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    # 64, not 40: equity-plan transaction exports run past 50 columns (one tax
    # column per jurisdiction) and the totals sit at the far right, so a
    # narrower cut silently drops the summary columns worth reading.
    return ir_ingest.workbook_to_grids(wb, max_rows=MAX_ROWS, max_cols=64)


def parse_csv_grid(content: bytes) -> dict[str, list[list]]:
    text = content.decode("utf-8-sig", errors="replace")
    rows = [r for _, r in zip(range(MAX_ROWS), csvmod.reader(io.StringIO(text)))]
    return {"csv": rows}


# ------------------------------------------------------------- classification --
DOC_KEYWORDS = {
    "escrow_analysis": ["escrow account disclosure", "annual escrow account review",
                        "escrow account history", "projected escrow account activity",
                        "escrow shortage"],
    "ss_statement": ["social security statement", "your estimated benefits",
                     "retirement benefits", "ssa.gov", "full retirement age",
                     "earnings record"],
    "paystub": ["earnings statement", "pay period", "net pay", "gross pay",
                "pay date", "direct deposit", "ytd", "basis of pay", "pay stub"],
    "mortgage": ["mortgage statement", "escrow", "principal balance",
                 "unpaid principal", "loan number", "amount due",
                 "interest rate", "servicer"],
}


# A bank ledger and a brokerage activity export look alike structurally; the
# difference is that a bank ledger has no security columns. Only the former
# is spending data.
LEDGER_SECURITY_HINT = re.compile(r"symbol|cusip|ticker|shares|market\s*value", re.I)


CARD_HEADER_HINT = re.compile(r"card\s*member|card\s*no|merchant", re.I)


def is_bank_ledger(grids: dict[str, list[list]]) -> tuple[bool, dict | None]:
    for grid in grids.values():
        for row in grid[:15]:
            col = spending.find_columns(row)
            if col and not any(LEDGER_SECURITY_HINT.search(str(c)) for c in row):
                return True, col
    return False, None


def is_card_ledger(grids: dict[str, list[list]]) -> tuple[bool, dict | None]:
    """A card statement looks like a bank ledger but carries card-holder
    columns and (unlike checking) records purchases as positive amounts."""
    for grid in grids.values():
        for row in grid[:15]:
            col = spending.find_columns(row)
            if col and any(CARD_HEADER_HINT.search(str(c)) for c in row):
                return True, col
    return False, None


def is_investment_history(grids: dict[str, list[list]]) -> tuple[bool, dict | None]:
    for grid in grids.values():
        for row in grid[:15]:
            col = investments.find_columns(row)
            if col:
                return True, col
    return False, None


def _ledger_body(grids, col, monthfn) -> list[list]:
    return [r for g in grids.values() for r in g
            if len(r) > max(col.values()) and monthfn(str(r[col["date"]])) is not None]


def classify_doc(text: str, fmt: str) -> str:
    if fmt in ("xlsx", "csv"):
        return "workbook"
    low = text.lower()
    # some PDFs extract with the spaces stripped out of headings, so score
    # against both forms and take whichever matches
    flat = re.sub(r"\s+", "", low)
    scores = {k: sum(low.count(w) + flat.count(re.sub(r"\s+", "", w)) for w in words)
              for k, words in DOC_KEYWORDS.items()}
    # An escrow analysis is also full of mortgage vocabulary, so its own
    # distinctive headings win outright rather than competing on volume.
    if scores.get("escrow_analysis", 0) >= 2:
        return "escrow_analysis"
    best = max(scores, key=scores.get)
    return best if scores[best] >= 2 else "unknown"


# ---------------------------------------------------------- line-scan helpers --
def _line_amounts(line: str) -> list[float]:
    vals = [_amt(m) for m in re.findall(AMOUNT, line)]
    return [v for v in vals if v is not None]


def scan_lines(pages: list[str], label_re: str, fname: str, name: str,
               want: str = "first", conf: float = 0.85,
               min_v: float = 0.0, max_v: float = 1e9,
               stop_re: str | None = None) -> dict:
    """Find a labeled amount: first line matching label_re that carries amounts.

    want: 'first' | 'last' (position of the amount on the line — paystubs put
    current-period first and YTD last).
    stop_re: paystubs print two columns side by side on one line, so this cuts
    the line where an unrelated label starts; without it "last" can pick up a
    figure belonging to the neighbouring column.
    """
    rx = re.compile(label_re, re.I)
    stop = re.compile(stop_re, re.I) if stop_re else None
    for pno, page in enumerate(pages, 1):
        for line in page.splitlines():
            m = rx.search(line)
            if not m:
                continue
            # amounts only after the label, so digits inside the label
            # (e.g. "401(k)") are never mistaken for values
            tail = line[m.end():]
            if stop:
                s = stop.search(tail)
                if s:
                    tail = tail[:s.start()]
            vals = [v for v in _line_amounts(tail) if min_v <= abs(v) <= max_v]
            if not vals:
                continue
            v = vals[0] if want == "first" else vals[-1]
            return field(v, source={"file": fname, "page": pno,
                                    "method": f"line:{name}"}, confidence=conf)
    return field(source={"file": fname, "method": f"line:{name}"})


def scan_date(pages: list[str], label_re: str, fname: str, name: str) -> dict:
    """Labeled date; handles 08/01/2026 and 'September 1, 2026' forms."""
    rx_num = re.compile(label_re + r"\D{0,30}?" + DATE, re.I)
    rx_word = re.compile(label_re + r"\W{0,10}([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", re.I)
    for pno, page in enumerate(pages, 1):
        m = rx_num.search(page)
        if m:
            return field(m.group(m.lastindex), source={"file": fname, "page": pno,
                         "method": f"date:{name}"}, confidence=0.8)
        m = rx_word.search(page)
        if m and m.group(m.lastindex - 2).lower() in MONTHS:
            mm = MONTHS[m.group(m.lastindex - 2).lower()]
            return field(f"{int(m.group(m.lastindex)):04d}-{mm:02d}",
                         source={"file": fname, "page": pno,
                                 "method": f"date:{name}"}, confidence=0.8)
    return field(source={"file": fname, "method": f"date:{name}"})


def _to_ym(d: str | None) -> str | None:
    if not d:
        return None
    if re.fullmatch(r"\d{4}-\d{2}", str(d)):
        return d
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", str(d))
    if not m:
        return None
    mm, _, yy = m.groups()
    y = int(yy)
    if y < 100:
        y += 2000
    return f"{y:04d}-{int(mm):02d}"


# ------------------------------------------------------------------ paystub --
# A paystub labels its state withholding line with the state's own code
# ("CA W/H", "NY State Tax").  That is a useful hint about where someone
# lives, and a bad source of truth: withholding state is not residence state
# for remote workers, and two-letter tokens are everywhere in prose.  So this
# only ever proposes -- it is stored at low confidence with needs_review set,
# which makes the intake form show it flagged rather than silently adopt it.
_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
}
_STATE_WH = re.compile(
    r"\b([A-Z]{2})\b\s*(?:STATE\s*)?(?:INC(?:OME)?\s*)?(?:TAX|W/?H|SDI|SUI)\b",
    re.I)


def detect_state(pages: list[str], fname: str) -> dict:
    """Propose a state of residence from a paystub withholding label."""
    counts: dict[str, int] = {}
    for page in pages:
        for match in _STATE_WH.finditer(page):
            code = match.group(1).upper()
            if code in _STATE_CODES:
                counts[code] = counts.get(code, 0) + 1
    if not counts:
        return field(None, source={"file": fname, "method": "state:withholding-label"},
                     confidence=0.0)
    best = max(counts, key=counts.get)
    return field(best, raw=f"{best} withholding label x{counts[best]}",
                 source={"file": fname, "method": "state:withholding-label"},
                 confidence=0.5)


def extract_paystub(pages: list[str], tables, fname: str) -> dict[str, dict]:
    f: dict[str, dict] = {}
    f["base_salary_current"] = scan_lines(pages, r"base\s*salary|regular\s*salary",
                                          fname, "base_salary", "first", 0.85,
                                          500, 100_000)
    f["base_salary_ytd"] = scan_lines(pages, r"base\s*salary|regular\s*salary",
                                      fname, "base_salary_ytd", "last", 0.7,
                                      1_000, 2_000_000)
    f["gross_current"] = scan_lines(pages, r"total\s*earnings|gross\s*(pay|earnings|wages)",
                                    fname, "gross", "first", 0.85, 500, 200_000)
    f["gross_ytd"] = scan_lines(pages, r"total\s*earnings|gross\s*(pay|earnings|wages)",
                                fname, "gross_ytd", "last", 0.7, 1_000, 5_000_000,
                                stop_re=r"net\s*pay|net\s*check|take\s*home")
    f["net_pay"] = scan_lines(pages, r"net\s*(pay|check|amount)(?!\s*(this|distribution))"
                              r"|take\s*home", fname,
                              "net_pay", "first", 0.85, 100, 100_000)
    f["fed_withholding"] = scan_lines(pages, r"fed(eral)?\s*(inc(ome)?)?\s*(tax|w/?h)|fitw",
                                      fname, "fed_wh", "first", 0.75, 1, 50_000)
    f["state_withholding"] = scan_lines(pages, r"(state|[a-z]{2})\s*(inc(ome)?)?\s*"
                                        r"(w/?h\s*tax|tax|w/?h)|sitw",
                                        fname, "state_wh", "first", 0.75, 1, 50_000)
    f["state_hint"] = detect_state(pages, fname)
    f["ca_sdi"] = scan_lines(pages, r"ca\s*disability|casdi|\bsdi\b", fname,
                             "ca_sdi", "first", 0.7, 1, 10_000)
    f["oasdi"] = scan_lines(pages, r"social\s*security|oasdi|fica(?!.*med)", fname,
                            "oasdi", "first", 0.75, 1, 20_000)
    f["medicare"] = scan_lines(pages, r"medicare", fname, "medicare", "first",
                               0.75, 1, 20_000)
    f["k401_current"] = scan_lines(pages, r"401\s*\(?k\)?\s*(pre\s*tax)?"
                                   r"(?!\s*employer)(?!\s*match)", fname,
                                   "k401", "first", 0.75, 1, 10_000)
    f["k401_ytd"] = scan_lines(pages, r"401\s*\(?k\)?\s*(pre\s*tax)?(?!\s*employer)",
                               fname, "k401_ytd", "last", 0.6, 100, 100_000)
    f["k401_match_current"] = scan_lines(pages, r"401k?\s*employer\s*match|"
                                         r"employer\s*match|company\s*match", fname,
                                         "k401_match", "first", 0.75, 1, 10_000)
    f["hsa"] = scan_lines(pages, r"\*?\s*hsa\s*(ee|employee)?(?!\s*er\b)(?!\s*lump)"
                          r"|health\s*sav", fname, "hsa", "first", 0.7, 1, 10_000)
    for name, rx in (("total_taxes", r"total\s*taxes"),
                     ("total_benefits", r"total\s*benefits"),
                     ("total_retirement", r"total\s*retirement"),
                     ("total_other", r"total\s*other")):
        f[name] = scan_lines(pages, rx, fname, name, "first", 0.75, 1, 100_000)

    # pay period: labeled dates or combined "Period Beg/End: d1 - d2"
    f["period_start"] = scan_date(pages, r"(period\s*(beg(in(ning)?)?|start)|pay\s*period)",
                                  fname, "period_start")
    f["period_end"] = scan_date(pages, r"(period\s*end(ing)?|thru|through)",
                                fname, "period_end")
    if f["period_end"]["value"] is None:
        rx = re.compile(r"period[^\n]{0,25}?" + DATE + r"\s*[-–]\s*" + DATE, re.I)
        for pno, page in enumerate(pages, 1):
            m = rx.search(page)
            if m:
                f["period_start"] = field(m.group(1), source={"file": fname, "page": pno,
                                          "method": "date:period_combined"}, confidence=0.85)
                f["period_end"] = field(m.group(2), source={"file": fname, "page": pno,
                                        "method": "date:period_combined"}, confidence=0.85)
                break

    # derive frequency + annual salary (base salary preferred over total
    # earnings, which can include one-time stock vests)
    freq, ppy = None, None
    s, e = _to_days(f["period_start"]["value"]), _to_days(f["period_end"]["value"])
    if s and e and e > s:
        freq, ppy = _freq_from_days(e - s + 1)
    if freq is None:
        freq, ppy = "biweekly", 26  # most common; flagged for review
    f["pay_frequency"] = field(freq, source={"file": fname, "method": "derived:period_len"},
                               confidence=0.8 if s and e else 0.4)
    base = f["base_salary_current"]["value"] or f["gross_current"]["value"]
    if base and ppy:
        f["salary_annual"] = field(round(base * ppy), source={"file": fname,
                                   "method": "derived:base*periods"}, confidence=0.75)
        k = f["k401_current"]["value"]
        if k:
            f["k401_pct"] = field(round(k / base * 100, 1), source={"file": fname,
                                  "method": "derived:k401/base"}, confidence=0.75)
        m = f["k401_match_current"]["value"]
        if m:
            # dollars/yr is unambiguous; a "50% match" plan and a "5% of salary"
            # plan can produce the same paystub line, so percent alone is unsafe
            f["employer_match_annual"] = field(round(m * ppy),
                                               source={"file": fname,
                                                       "method": "derived:match*periods"},
                                               confidence=0.8)
            f["employer_match_pct"] = field(round(m / base * 100, 1),
                                            source={"file": fname,
                                                    "method": "derived:match/base"},
                                            confidence=0.75)
        h = f["hsa"]["value"]
        if h and h < base * 0.2:  # skip employer lump sums mis-picked as per-period
            f["other_pretax_annual"] = field(round(h * ppy), source={"file": fname,
                                             "method": "derived:hsa*periods"},
                                             confidence=0.6)

    # cross-check: prefer stub's own totals; fall back to summing lines
    gc, net = f["gross_current"]["value"], f["net_pay"]["value"]
    ok = None
    if gc and net:
        if all(f[k]["value"] is not None for k in
               ("total_taxes", "total_retirement", "total_benefits", "total_other")):
            ded = sum(f[k]["value"] for k in ("total_taxes", "total_retirement",
                                              "total_benefits", "total_other"))
        else:
            ded = sum((f[k]["value"] or 0) for k in
                      ("fed_withholding", "state_withholding", "ca_sdi", "oasdi",
                       "medicare", "k401_current", "hsa"))
        ok = abs((gc - ded) - net) <= max(gc * 0.05, 50)
        if ok:
            for k in ("gross_current", "net_pay"):
                f[k]["confidence"] = 0.95
                f[k]["needs_review"] = False
    f["_crosscheck"] = field("PASS" if ok else ("FAIL" if ok is not None else "SKIP"),
                             source={"file": fname, "method": "crosscheck:gross-ded=net"},
                             confidence=1.0 if ok else 0.0)
    return f


def _to_days(d: str | None) -> int | None:
    if not d:
        return None
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", str(d))
    if not m:
        return None
    mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yy < 100:
        yy += 2000
    return yy * 372 + mm * 31 + dd  # ordinal-ish, good enough for span length


def _freq_from_days(days: int) -> tuple[str | None, int | None]:
    if 5 <= days <= 8:
        return "weekly", 52
    if 12 <= days <= 14:
        return "biweekly", 26
    if 15 <= days <= 16:
        return "semimonthly", 24
    if 27 <= days <= 32:
        return "monthly", 12
    return None, None


# ------------------------------------------------------------------ mortgage --
def extract_mortgage(pages: list[str], tables, fname: str) -> dict[str, dict]:
    f: dict[str, dict] = {}
    f["principal_balance"] = scan_lines(
        pages, r"(unpaid|outstanding|current)\s*principal\s*balance", fname,
        "principal", "first", 0.85, 1_000, 5_000_000)
    rx = re.compile(r"interest\s*rate\D{0,30}?(\d{1,2}\.\d{1,4})\s*%", re.I)
    f["interest_rate"] = field(source={"file": fname, "method": "regex:rate"})
    for pno, page in enumerate(pages, 1):
        m = rx.search(page)
        if m:
            f["interest_rate"] = field(round(float(m.group(1)) / 100, 6),
                                       source={"file": fname, "page": pno,
                                               "method": "regex:rate"}, confidence=0.9)
            break
    f["pi_payment"] = scan_lines(pages, r"principal\s*(&|and|/)\s*interest|p\s*&\s*i",
                                 fname, "pi", "first", 0.8, 100, 50_000)
    f["principal_component"] = scan_lines(pages, r"^\s*principal\s*:", fname,
                                          "principal_comp", "first", 0.75, 10, 50_000)
    f["interest_component"] = scan_lines(pages, r"^\s*interest\s*:", fname,
                                         "interest_comp", "first", 0.75, 10, 50_000)
    f["escrow_payment"] = scan_lines(pages, r"escrow(?!\s*balance)", fname,
                                     "escrow", "first", 0.7, 10, 20_000)
    f["escrow_balance"] = scan_lines(pages, r"escrow\s*balance", fname,
                                     "escrow_bal", "first", 0.7, 0, 100_000)
    f["total_payment"] = scan_lines(pages, r"(regular\s*monthly\s*payment|"
                                    r"total\s*(monthly\s*)?(payment|amount\s*due)|"
                                    r"amount\s*due)", fname,
                                    "total_pmt", "first", 0.75, 100, 60_000)
    f["next_due"] = scan_date(pages, r"(payment\s*)?due\s*(date|by)", fname, "due")
    f["maturity_date"] = scan_date(pages, r"maturity", fname, "maturity")
    for k in ("next_due", "maturity_date"):
        ym = _to_ym(f[k]["value"])
        if ym:
            f[k]["value"] = ym

    tp, pi, es = (f["total_payment"]["value"], f["pi_payment"]["value"],
                  f["escrow_payment"]["value"])
    pc, ic = f["principal_component"]["value"], f["interest_component"]["value"]
    if pi is None and pc and ic:
        f["pi_payment"] = field(round(pc + ic, 2), source={"file": fname,
                                "method": "derived:principal+interest"}, confidence=0.85)
        pi = pc + ic
    if pi is None and tp and es:
        f["pi_payment"] = field(round(tp - es, 2), source={"file": fname,
                                "method": "derived:total-escrow"}, confidence=0.75)
        pi = tp - es
    ok = None
    if tp and pi and es:
        ok = abs((pi + es) - tp) <= max(tp * 0.05, 25)
        if ok:
            for k in ("pi_payment", "escrow_payment"):
                f[k]["confidence"] = max(f[k]["confidence"], 0.92)
                f[k]["needs_review"] = False
    f["_crosscheck"] = field("PASS" if ok else ("FAIL" if ok is not None else "SKIP"),
                             source={"file": fname, "method": "crosscheck:pi+escrow=total"},
                             confidence=1.0 if ok else 0.0)
    return f


# ------------------------------------------------------------ escrow analysis --
def extract_escrow(pages: list[str], tables, fname: str) -> dict[str, dict]:
    """Servicer escrow analysis: what the escrow portion actually pays for.

    Property tax and hazard insurance behave very differently over thirty
    years — in California an assessment rises ~2%/yr under Prop 13 while
    insurance premiums have been climbing far faster — so they are pulled out
    as separate line items rather than one blended escrow figure.
    """
    # pdfplumber runs label words together in this layout, so match on text
    # with all whitespace removed and keep an index back to the amounts.
    flat = re.sub(r"[ \t]+", "", "\n".join(pages))
    f: dict[str, dict] = {}

    def grab(name: str, pattern: str, conf: float = 0.9, last: bool = False):
        m = re.search(pattern, flat, re.I)
        val = None
        if m:
            nums = [_amt(g) for g in m.groups() if g]
            nums = [n for n in nums if n is not None]
            if nums:
                val = nums[-1] if last else nums[0]
        f[name] = field(val, source={"file": fname, "method": f"escrow:{name}"},
                        confidence=conf if val is not None else 0.0)

    # annual escrow items
    grab("escrow_property_tax_annual", r"(?:county|property|real\s*estate)tax:?\$?"
         + AMOUNT.replace(r"\$?\s*", ""))
    grab("escrow_insurance_annual", r"(?:hazard|homeowners?|hoi)ins(?:urance)?:?\$?"
         + AMOUNT.replace(r"\$?\s*", ""))
    grab("escrow_total_annual", r"totalpaymentsfromescrow:?\$?"
         + AMOUNT.replace(r"\$?\s*", ""))
    # the payment table lists current then new; take the new (right-hand) figure
    two = r"\$?" + AMOUNT.replace(r"\$?\s*", "") + r"\s*\$?" + AMOUNT.replace(r"\$?\s*", "")
    grab("pi_payment", r"principalandinterest" + two, last=True)
    grab("escrow_payment", r"escrowpayment" + two, last=True)
    grab("escrow_reserve_payment", r"escrowreservepayment" + two, last=True)
    grab("total_payment", r"totalpaymentamount" + two, last=True)
    grab("escrow_shortage", r"escrowshortageand/?orescrowreserve\$?"
         + AMOUNT.replace(r"\$?\s*", ""), conf=0.8)

    m = re.search(r"startmakingthe'?newmonthlypaymentamount'?on([a-z]+\d{1,2},\d{4})",
                  flat, re.I)
    if m:
        f["escrow_effective_date"] = field(m.group(1), source={
            "file": fname, "method": "escrow:effective_date"}, confidence=0.8)

    # consistency: the two escrow items should add to the stated total
    tax = f["escrow_property_tax_annual"]["value"]
    ins = f["escrow_insurance_annual"]["value"]
    tot = f["escrow_total_annual"]["value"]
    ok = None
    if tax and ins and tot:
        ok = abs((tax + ins) - tot) <= max(tot * 0.02, 25)
        if ok:
            for k in ("escrow_property_tax_annual", "escrow_insurance_annual"):
                f[k]["confidence"] = 0.95
                f[k]["needs_review"] = False
    f["_crosscheck"] = field("PASS" if ok else ("FAIL" if ok is not None else "SKIP"),
                             source={"file": fname,
                                     "method": "crosscheck:tax+ins=escrow_total"},
                             confidence=1.0 if ok else 0.0)
    return f


# -------------------------------------------------------------- SSA statement --
_SS_EXCLUDE = re.compile(r"survivor|disab|child|family|medicare|spouse", re.I)
_SS_PAIR = re.compile(r"(?<![\d,.$])(6[2-9]|70)\b\D{0,40}?\$\s*([\d,]+)")


def extract_ss_statement(pages: list[str], tables, fname: str) -> dict[str, dict]:
    pairs: dict[int, list[float]] = {}

    def consider(age_s: str, amt_s: str, context_line: str):
        age, amt = int(age_s), _amt(amt_s)
        if amt is None or not (62 <= age <= 70) or not (100 <= amt <= 15_000):
            return
        # exclusion words checked on the SAME LINE only — SSA PDFs interleave
        # unrelated axis/label text between sections
        if _SS_EXCLUDE.search(context_line):
            return
        pairs.setdefault(age, []).append(amt)

    for page in pages:
        for line in page.splitlines():
            for m in _SS_PAIR.finditer(line):
                consider(m.group(1), m.group(2), line)
    for t in tables:
        for row in t:
            joined = " ".join(str(c) for c in row)
            for m in _SS_PAIR.finditer(joined):
                consider(m.group(1), m.group(2), joined)

    f: dict[str, dict] = {}
    by_age = {}
    for age, vals in sorted(pairs.items()):
        by_age[str(age)] = max(set(vals), key=vals.count)
    for age in (62, 67, 70):
        v = by_age.get(str(age))
        f[f"benefit_{age}"] = field(v, source={"file": fname, "method": "regex:age-amount"},
                                    confidence=0.85 if v is not None else 0.0)
    f["benefit_by_age"] = field(by_age or None,
                                source={"file": fname, "method": "regex:age-amount"},
                                confidence=0.75 if by_age else 0.0)
    vals = [by_age.get(str(a)) for a in (62, 67, 70)]
    if all(vals) and not (vals[0] < vals[1] < vals[2]):
        for age in (62, 67, 70):
            f[f"benefit_{age}"]["confidence"] = 0.4
            f[f"benefit_{age}"]["needs_review"] = True
    return f


# ------------------------------------------------- workbook / positions files --
HEADER_ROLES = [
    ("symbol", re.compile(r"^symbol$|^ticker$", re.I)),
    ("desc", re.compile(r"description", re.I)),
    ("qty", re.compile(r"^shares$|quantity", re.I)),
    ("value", re.compile(r"^(market|current)\s*value", re.I)),
    # Realised gain in dollars, when the broker computes it. This is the most
    # trustworthy source: cost = value - gain, with no per-share ambiguity.
    ("gain", re.compile(r"unrealized\s*gain(/|\s*&\s*|\s+or\s+|\s*)loss\s*\(?\$", re.I)),
    ("acct_name", re.compile(r"account\s*name|^account$", re.I)),
    ("acct_no", re.compile(r"account\s*number", re.I)),
    ("tax_term", re.compile(r"tax\s*term", re.I)),
    ("trade_date", re.compile(r"trade\s*date", re.I)),
]
# Brokerage exports mark aggregate rows with the literal "Detail": in the
# account column it means "summed across accounts", in the tax-term/trade-date
# column it means "summed across tax lots". Both duplicate rows shown below
# them, so they must not be added twice.
ROLLUP = "detail"
TRANSACTION_HINT = re.compile(r"^activity$|^transaction|^amount\s*\d?$", re.I)
TOTALISH = re.compile(r"^\s*(sub)?total|^\s*grand|^\s*pending", re.I)
SYMBOLISH = re.compile(r"^[A-Z]{1,5}(\*{1,2}|[./][A-Z]{1,2})?$")

ACCOUNT_VOCAB = re.compile(r"401\s*\(?k\)?|403b|\bira\b|roth|brokerage|savings|"
                           r"checking|\bhsa\b|\bcash\b|money\s*market|"
                           r"vanguard|fidelity|schwab|e\*?trade|merrill|balance", re.I)
EXPENSE_VOCAB = re.compile(r"mortgage|utilit|grocer|insurance|property\s*tax|"
                           r"gas|food|dining|travel|subscript|total\s*(monthly\s*)?"
                           r"(expense|spend)", re.I)


# What was paid for a position. Exports routinely carry several of these side by
# side — one sheet here has "Cost Basis" (per share), "Original Cost" and
# "Total Cost1" — so first-match is wrong: a per-share figure against a total
# market value overstates the gain by the share count. Lower rank wins, and
# anything naming itself per-share is rejected outright.
COST_COLUMNS = [
    (0, re.compile(r"cost\s*basis\s*total|total\s*cost|total\s*book\s*(value|cost)", re.I)),
    (1, re.compile(r"original\s*cost|book\s*value", re.I)),
    (2, re.compile(r"cost\s*basis", re.I)),
]
COST_PER_SHARE = re.compile(r"average|avg|per\s*share|/\s*share|unit\s*cost|price", re.I)


def _cost_overrides() -> list[dict]:
    """Owner-supplied cost basis for positions the broker reports as N/A.

    Some lots carry no basis at all — shares transferred in from another
    institution, or old ESPP/RSU grants — and the statement shows N/A for every
    cost field. Without this the position is either excluded from performance or
    drags a partial-coverage caveat behind it forever.
    """
    data = common.load_json(common.FIN_DATA / "cost_basis_overrides.json") or {}
    return data.get("overrides") or []


def _sale_cost_overrides() -> list[dict]:
    """Owner-confirmed basis for completed sales missing broker lot detail."""
    data = common.load_json(common.FIN_DATA / "cost_basis_overrides.json") or {}
    return data.get("sale_overrides") or []


def _override_cost(overrides: list[dict], symbol: str, account: str,
                   qty: float | None) -> float | None:
    for o in overrides:
        if (o.get("symbol") or "").upper() != (symbol or "").upper():
            continue
        acct = o.get("account")
        if acct and acct.strip().lower() != (account or "").strip().lower():
            continue
        if o.get("cost_total") is not None:
            return round(float(o["cost_total"]), 2)
        if o.get("cost_per_share") is not None and qty:
            return round(float(o["cost_per_share"]) * float(qty), 2)
    return None


def _header_map(row: list) -> dict | None:
    texts = [str(c).strip() if c is not None else "" for c in row]
    roles = {}
    for ci, t in enumerate(texts):
        if not t:
            continue
        for role, rx in HEADER_ROLES:
            if role not in roles and rx.search(t):
                roles[role] = ci
    best_rank = None
    for ci, t in enumerate(texts):
        if not t or COST_PER_SHARE.search(t):
            continue
        for rank, rx in COST_COLUMNS:
            if rx.search(t):
                if best_rank is None or rank < best_rank:
                    best_rank, roles["cost_basis"] = rank, ci
                break
    if any(t and TRANSACTION_HINT.search(t) for t in texts):
        return {"transactions": True}
    if "value" in roles and ("symbol" in roles or "desc" in roles):
        return roles
    return None


def _norm(v) -> str:
    return str(v).strip() if v is not None else ""


# Statements label accounts as "401k R/O IRA(*1234)" in some sheets while
# positions tables show only "*1234". Harvesting that mapping is what lets us
# tell a rollover IRA (tax-deferred, RMDs) from a taxable brokerage account.
ACCOUNT_ALIAS = re.compile(r"([A-Za-z0-9 ./&'-]{2,40}?)\s*\(\*?(\d{3,6})\)")


def _scan_aliases(grids: dict[str, list[list]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for grid in grids.values():
        for row in grid:
            for c in row:
                if not isinstance(c, str) or "(" not in c:
                    continue
                for m in ACCOUNT_ALIAS.finditer(c):
                    name = re.sub(r"^\W+", "", m.group(1)).strip()
                    if len(name) >= 2:
                        out.setdefault("*" + m.group(2), name[:40])
    return out


# --------------------------------------------------------- advisory fee rows --
# Activity ledgers are skipped for balances, but they carry the one thing no
# positions export does: what the advisor actually billed. A charge names its
# own account, so harvesting it is evidence rather than the guesswork the fee
# module refuses to do.
FEE_ACTIVITY = re.compile(r"advisory\s*fee|advisor\w*\s*fee|management\s*fee|"
                          r"program\s*fee|wrap\s*fee|\bpim\b.*fee", re.I)
# Brokers state the billing period in the description ("PIM QUARTERLY FEE").
# It matters: one charge tells you nothing about the annual rate unless you
# know how many of them a year holds.
FEE_PERIOD = [(re.compile(r"quarter", re.I), "quarterly", 4),
              (re.compile(r"month", re.I), "monthly", 12),
              (re.compile(r"semi[-\s]*annual|half[-\s]*year", re.I), "semiannual", 2),
              (re.compile(r"annual|yearly", re.I), "annual", 1)]
TXN_ROLES = [
    ("date", re.compile(r"^(trade\s*|process\s*|posting\s*|run\s*)?date$", re.I)),
    ("account", re.compile(r"^account", re.I)),
    ("activity", re.compile(r"^activity$|^transaction\s*type$|^type$|"
                            r"^description$", re.I)),
    ("desc", re.compile(r"^description$|^security$", re.I)),
    ("amount", re.compile(r"^amount", re.I)),
    ("symbol", re.compile(r"^symbol$|^ticker$", re.I)),
    ("quantity", re.compile(r"^quantity$|^shares$", re.I)),
    ("price", re.compile(r"^price", re.I)),
]


def _txn_header(row: list) -> dict | None:
    """Column map for an activity ledger — needs at minimum what/when/how much."""
    roles: dict[str, int] = {}
    for ci, cell in enumerate(row):
        t = _norm(cell)
        if not t:
            continue
        for role, rx in TXN_ROLES:
            if role not in roles and rx.search(t):
                roles[role] = ci
    if "amount" in roles and "date" in roles and ("activity" in roles or "desc" in roles):
        return roles
    return None


def _fee_period(text: str) -> tuple[str | None, int | None]:
    for rx, name, n in FEE_PERIOD:
        if rx.search(text):
            return name, n
    return None, None


# Cash the account earned, as opposed to cash it was charged. Kept alongside
# the fees so the two can be put on the same axis: a fee is only expensive or
# cheap relative to what the account produced.
INCOME_ACTIVITY = re.compile(r"^dividend$|^interest$|"
                             r"^capital\s*gain|^distribution", re.I)


def scan_activity(grids: dict[str, list[list]], fname: str) -> dict[str, list[dict]]:
    """Fee charges and income from any activity/transaction sheet.

    One pass over the ledger classifies each row as a fee the account paid or
    cash it earned. Everything else (buys, sells, transfers) is position
    movement the positions export already accounts for.
    """
    fees: list[dict] = []
    income: list[dict] = []
    records: list[dict] = []
    for sheet, grid in grids.items():
        colmap = None
        for row in grid:
            cells = list(row)
            hm = _txn_header(cells)
            if hm:
                colmap = hm
                continue
            if not colmap:
                continue

            def cell(role):
                ci = colmap.get(role)
                return _norm(cells[ci]) if ci is not None and ci < len(cells) else ""

            activity, desc = cell("activity"), cell("desc")
            amount = _amt(cell("amount"))
            iso, raw_date = _repair_ledger_date(_to_iso_date(cell("date")))
            if not iso:
                continue
            records.append({"account": cell("account")[:50] or sheet,
                            "date": iso, "action": activity[:100],
                            "symbol": cell("symbol")[:24].upper() or None,
                            "description": desc[:80] or None,
                            "quantity": _amt(cell("quantity")),
                            "price": _amt(cell("price")), "amount": amount,
                            "source": fname, "sheet": sheet})
            is_fee = bool(FEE_ACTIVITY.search(f"{activity} {desc}"))
            if not is_fee and not INCOME_ACTIVITY.search(activity):
                continue
            if not amount:
                continue
            rec = {"account": cell("account")[:50] or sheet,
                   "date": iso, "amount": round(abs(amount), 2),
                   "activity": activity[:40],
                   "date_repaired_from": raw_date,
                   "source": fname, "sheet": sheet}
            if is_fee:
                billed, per_year = _fee_period(f"{activity} {desc}")
                fees.append({**rec, "description": desc[:60],
                             "billed": billed, "periods_per_year": per_year})
            else:
                income.append({**rec, "symbol": cell("symbol")[:12] or None})
    return {"fees": fees, "income": income, "records": records}


def _to_iso_date(s: str) -> str | None:
    s = (s or "").strip()[:10]
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _repair_ledger_date(iso: str | None) -> tuple[str | None, str | None]:
    """(date, original-if-repaired) for a row in a transaction ledger.

    A posted transaction cannot be in the future. Exports still produce them:
    one cell in the Wells Fargo activity workbook is a real date rather than
    text, and whatever wrote it auto-completed "11/6" to the current year, so
    2025-11-06 arrived as 2026-11-06 sitting between two 2025 rows. Rolling the
    year back until the date is in the past recovers the only reading that can
    be true, and the original is kept so the repair is visible rather than
    silent.
    """
    if not iso:
        return iso, None
    today = datetime.now().date().isoformat()
    if iso <= today:
        return iso, None
    orig, year = iso, int(iso[:4])
    while iso > today and year > 1990:
        year -= 1
        iso = f"{year:04d}{orig[4:]}"
    return iso, orig


def _snapshot_date(fname: str) -> str | None:
    """Best available as-of date for broker position exports."""
    named = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-_ ](\d{1,2})[-_ ](\d{4})",
        fname, re.I)
    if named:
        try:
            return datetime.strptime("-".join(named.groups()), "%b-%d-%Y").date().isoformat()
        except ValueError:
            pass
    compact = re.search(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", fname)
    if compact:
        try:
            return datetime.strptime("".join(compact.groups()), "%m%d%y").date().isoformat()
        except ValueError:
            pass
    return None


def extract_workbook(grids: dict[str, list[list]], fname: str) -> dict:
    """Positions/holdings exports plus generic label->number sheets.

    Handles brokerage tax-lot exports: rows marked "Detail" in the account
    column (cross-account roll-up) or the tax-term column (per-account
    position roll-up) duplicate the rows beneath them, so exactly one level
    is kept. Section subtotals and the reported portfolio total are captured
    and used to reconcile what we summed.
    """
    rows_by_group: dict[tuple, list[dict]] = {}
    snapshot_date = _snapshot_date(fname)
    overrides = _cost_overrides()
    expenses, label_accounts = [], []
    section_totals: list[dict] = []
    reported_total = None
    skipped_txn = 0

    aliases = _scan_aliases(grids)
    activity = scan_activity(grids, fname)

    for sheet, grid in grids.items():
        # a transaction ledger has no balances to harvest — skip the whole
        # sheet so the label->number fallback can't mine junk from it
        if any((_header_map(r) or {}).get("transactions") for r in grid):
            skipped_txn += 1
            continue
        colmap, section = None, None
        for ri, row in enumerate(grid):
            cells = list(row)
            nonempty = [c for c in cells if c not in (None, "")]
            if not nonempty:
                continue
            hm = _header_map(cells)
            if hm:
                colmap = None if hm.get("transactions") else hm
                continue
            first = _norm(nonempty[0])

            if TOTALISH.search(first):
                val = None
                if colmap and colmap.get("value") is not None:
                    ci = colmap["value"]
                    val = _amt(cells[ci]) if ci < len(cells) else None
                if val is None:  # total rows can sit outside any column layout
                    nums = [n for n in (_amt(c) for c in nonempty[1:]) if n is not None]
                    val = max(nums) if nums else None
                if val is not None:
                    if re.search(r"total\s+portfolio|grand\s+total", first, re.I):
                        reported_total = val
                    else:
                        section_totals.append({"label": first[:40], "value": round(val, 2)})
                continue

            if len(nonempty) == 1 and isinstance(nonempty[0], str):
                section = first[:40]
                continue

            if colmap:
                def cell(role):
                    ci = colmap.get(role)
                    return cells[ci] if ci is not None and ci < len(cells) else None

                value = _amt(cell("value"))
                if value is None or abs(value) < 0.01:
                    continue
                sym_raw = _norm(cell("symbol"))
                sym = sym_raw if SYMBOLISH.match(sym_raw) else None
                desc = _norm(cell("desc"))[:60] or None
                acct = (_norm(cell("acct_name")) or _norm(cell("acct_no"))
                        or section or sheet)[:50]
                lot = (_norm(cell("tax_term")) or _norm(cell("trade_date"))).lower()
                qty = _amt(cell("qty"))
                cost = _amt(cell("cost_basis"))
                gain = _amt(cell("gain"))
                if gain is not None:
                    # The broker already reconciled this; trust it over any
                    # cost column whose per-share/total basis is ambiguous.
                    cost = round(value - gain, 2)
                elif cost is not None and qty and abs(qty) > 1:
                    # Last-resort scale check for a column that named itself a
                    # total but holds a per-share figure. Only rescale when
                    # multiplying by the share count clearly explains the
                    # market value and the raw number clearly does not.
                    if cost and abs(value / cost) > 5 and 0.2 < abs(value / (cost * qty)) < 5:
                        cost = round(cost * qty, 2)
                if cost is None and sym:
                    cost = _override_cost(overrides, sym, acct, qty)
                rows_by_group.setdefault((acct, sym or desc or "?"), []).append(
                    {"account": acct, "symbol": sym, "description": desc,
                     "quantity": _amt(cell("qty")), "value": round(value, 2),
                     "cost_basis": round(cost, 2) if cost is not None else None,
                     "sheet": sheet, "snapshot_date": snapshot_date,
                     "is_rollup": lot == ROLLUP})
                continue

            # fallback: label -> number pairs (budget / net-worth style sheets)
            for ci, c in enumerate(cells):
                if not isinstance(c, str) or not c.strip():
                    continue
                nums = [x for x in cells[ci + 1:ci + 4]
                        if isinstance(x, (int, float)) and abs(x) >= 50]
                if not nums:
                    continue
                label = c.strip()[:60]
                if ACCOUNT_VOCAB.search(label):
                    label_accounts.append({"label": label, "value": float(nums[0]),
                                           "sheet": sheet, "row": ri + 1})
                elif EXPENSE_VOCAB.search(label):
                    expenses.append({"label": label, "value": float(nums[0]),
                                     "sheet": sheet, "row": ri + 1})
                break

    # ---- collapse roll-up levels -------------------------------------------
    holdings, accounts = [], {}
    n_dropped = 0
    for (acct, _key), group in rows_by_group.items():
        if acct.lower() == ROLLUP:       # summed across accounts: shown below too
            n_dropped += len(group)
            continue
        rollups = [r for r in group if r["is_rollup"]]
        keep = rollups if rollups else group
        n_dropped += len(group) - len(keep)
        # Collapse the surviving rows into one position per account+symbol.
        # Without this a holding split across tax lots stays as many rows, and
        # the per-symbol dedupe downstream keeps only the largest — one Microsoft
        # position of 94 lots collapsed to a single lot and lost $403k.
        merged = dict(keep[0])
        merged.pop("is_rollup", None)
        merged["value"] = round(sum(r["value"] for r in keep), 2)
        merged["lots"] = len(keep)
        for field, cover in (("quantity", "qty_value"), ("cost_basis", "cost_value")):
            vals = [r for r in keep if r.get(field) is not None]
            merged[field] = round(sum(r[field] for r in vals), 6) if vals else None
            # how much market value the summed field actually accounts for, so
            # a partially-reported cost or share count can be spotted later
            merged[cover] = round(sum(r["value"] for r in vals), 2) if vals else None
        holdings.append(merged)
        accounts[acct] = accounts.get(acct, 0.0) + merged["value"]

    captured = round(sum(accounts.values()), 2)
    reconciliation = None
    if reported_total:
        delta = round(reported_total - captured, 2)
        reconciliation = {"reported_total": reported_total, "captured": captured,
                          "delta": delta,
                          "pct": round(delta / reported_total, 4) if reported_total else 0}
        # sections we could not parse (e.g. annuities with a different layout):
        # trust the statement's own total rather than under-report the portfolio
        if abs(delta) > max(reported_total * 0.01, 500):
            accounts["Other positions (per statement total)"] = delta

    account_candidates = ([{"label": k, "value": round(v, 2), "sheet": "positions",
                            "snapshot_date": snapshot_date}
                           for k, v in accounts.items()] + label_accounts)
    return {"account_candidates": account_candidates, "holding_candidates": holdings,
            "expense_candidates": expenses, "n_sheets": len(grids),
            "n_transaction_sheets_skipped": skipped_txn,
            "n_rollup_rows_dropped": n_dropped, "account_aliases": aliases,
            "fee_charges": activity["fees"], "income_activity": activity["income"],
            "activity_records": activity["records"],
            "section_totals": section_totals, "reconciliation": reconciliation}


def _record_key(r: dict) -> tuple:
    return (str(r.get("account") or "").strip().lower(), r.get("date"),
            str(r.get("action") or "").strip().lower(),
            str(r.get("symbol") or "").strip().upper(),
            str(r.get("description") or "").strip().lower(),
            round(float(r.get("quantity") or 0), 6),
            round(float(r.get("amount") or 0), 2))


def _merge_record_exports(exports: list[list[dict]], keyfn) -> list[dict]:
    """Union overlapping exports while preserving true repeated rows."""
    merged: dict[tuple, list[dict]] = {}
    for records in exports:
        local: dict[tuple, list[dict]] = {}
        for rec in records:
            local.setdefault(keyfn(rec), []).append(rec)
        for key, copies in local.items():
            current = merged.setdefault(key, [])
            if len(copies) > len(current):
                current.extend(copies[len(current):])
    return [rec for copies in merged.values() for rec in copies]


def _account_key(value: str) -> str:
    text = str(value or "").strip().lower()
    numbers = re.findall(r"\d{4}", text)
    return f"*{numbers[-1]}" if numbers else re.sub(r"[^a-z0-9]+", "", text)


def _security_key(symbol: str | None, description: str | None) -> str:
    if symbol:
        return re.sub(r"[^A-Z0-9.]+", "", symbol.upper())
    return re.sub(r"[^a-z0-9]+", "", str(description or "").lower())


def _roll_forward_holdings(holdings: list[dict], accounts: list[dict],
                           records: list[dict]) -> dict:
    """Apply deduped post-snapshot activity to position and cash balances."""
    by_account: dict[str, list[dict]] = {}
    snapshot: dict[str, str] = {}
    for holding in holdings:
        key = _account_key(holding.get("account"))
        by_account.setdefault(key, []).append(holding)
        date = holding.get("snapshot_date")
        if date and (key not in snapshot or date > snapshot[key]):
            snapshot[key] = date

    applied = skipped = 0
    shares: dict[str, float] = {}
    cash_delta: dict[str, float] = {}
    for rec in sorted(records, key=lambda r: (r.get("date") or "", _record_key(r))):
        acct = _account_key(rec.get("account"))
        if acct not in by_account or not snapshot.get(acct):
            continue
        if not rec.get("date") or rec["date"] <= snapshot[acct]:
            skipped += 1
            continue
        group = investments.group_action(rec.get("action") or "")
        qty = float(rec.get("quantity") or 0)
        if group == "sell" and qty > 0:
            qty = -qty
        if group in {"buy", "reinvestment", "shares deposited (RSU/ESPP)",
                     "contribution"} and qty < 0:
            qty = -qty
        amount = float(rec.get("amount") or 0)
        security = _security_key(rec.get("symbol"), rec.get("description"))
        position_groups = {"buy", "sell", "reinvestment",
                           "shares deposited (RSU/ESPP)", "contribution"}
        if qty and security and group in position_groups:
            position = next((h for h in by_account[acct]
                             if _security_key(h.get("symbol"), h.get("description")) == security),
                            None)
            if position is None and qty > 0:
                value = abs(amount) or abs(qty * float(rec.get("price") or 0))
                position = {"account": rec.get("account") or acct,
                            "symbol": rec.get("symbol"),
                            "description": rec.get("description"),
                            "quantity": 0.0, "value": 0.0, "cost_basis": 0.0,
                            "sheet": "activity roll-forward", "lots": 1,
                            "snapshot_date": snapshot[acct]}
                holdings.append(position)
                by_account[acct].append(position)
            if position is not None:
                old_qty = float(position.get("quantity") or 0)
                old_value = float(position.get("value") or 0)
                old_cost = position.get("cost_basis")
                if group == "sell" and old_qty > 0:
                    remain = max(0.0, (old_qty + qty) / old_qty)
                    position["value"] = round(old_value * remain, 2)
                    if old_cost is not None:
                        position["cost_basis"] = round(float(old_cost) * remain, 2)
                else:
                    added = abs(amount) or abs(qty * float(rec.get("price") or 0))
                    position["value"] = round(old_value + added, 2)
                    if old_cost is not None:
                        position["cost_basis"] = round(float(old_cost) + added, 2)
                position["quantity"] = round(max(0.0, old_qty + qty), 6)
                position["qty_value"] = position["value"]
                if position.get("cost_basis") is not None:
                    position["cost_value"] = position["value"]
                shares[rec.get("symbol") or rec.get("description") or security] = (
                    shares.get(rec.get("symbol") or rec.get("description") or security, 0.0)
                    + qty)

        # A contribution/deposit that directly acquires a security is external
        # funding, not a debit from an existing sweep position.
        adjust_cash = not (qty and group in {"contribution",
                                              "shares deposited (RSU/ESPP)"})
        if amount and adjust_cash:
            cash = next((h for h in by_account[acct]
                         if ("bank deposit sweep" in str(h.get("description") or "").lower()
                             or "held in money market" in str(h.get("description") or "").lower()
                             or str(h.get("symbol") or "").endswith("**"))), None)
            if cash is not None:
                cash["value"] = round(float(cash.get("value") or 0) + amount, 2)
                cash_delta[acct] = cash_delta.get(acct, 0.0) + amount
        applied += 1

    # Position-backed account rows should tie to the adjusted positions.
    totals = {acct: round(sum(float(h.get("value") or 0) for h in hs), 2)
              for acct, hs in by_account.items()}
    for account in accounts:
        key = _account_key(account.get("label"))
        if account.get("sheet") == "positions" and key in totals:
            account["value"] = totals[key]
    holdings[:] = [h for h in holdings
                   if not (h.get("quantity") == 0 and abs(float(h.get("value") or 0)) < 0.01)]
    return {"snapshot_dates": snapshot, "records_applied": applied,
            "pre_snapshot_skipped": skipped,
            "share_deltas": {k: round(v, 6) for k, v in shares.items()},
            "cash_deltas": {k: round(v, 2) for k, v in cash_delta.items()}}


# --------------------------------------------------------------------- merge --
CANONICAL = ["salary_annual", "pay_frequency", "k401_pct", "employer_match_pct",
             "employer_match_annual",
             "other_pretax_annual", "gross_current", "gross_ytd", "net_pay",
             "fed_withholding", "state_withholding", "state_hint",
             "oasdi", "medicare",
             "k401_current", "hsa",
             "principal_balance", "interest_rate", "pi_payment",
             "escrow_payment", "total_payment", "next_due", "maturity_date",
             "escrow_property_tax_annual", "escrow_insurance_annual",
             "escrow_total_annual", "escrow_reserve_payment",
             "benefit_62", "benefit_67", "benefit_70", "benefit_by_age"]


def _ss_role(filename: str) -> str:
    """Resolve statement ownership from private, gitignored source metadata."""
    roles = common.load_json(common.FIN_DATA / "source_roles.json") or {}
    return roles.get(filename, "self")


def _structure_profile(doc: dict) -> dict:
    """Stable, non-PII schema used to validate parser selection."""
    field_names = sorted(k for k in doc.get("fields", {}) if not k.startswith("_"))
    features = sorted(k for k in (
        "bank_records", "investment_records", "activity_records", "spending",
        "card", "investments", "vesting", "holding_candidates",
        "account_candidates", "fee_charges") if doc.get(k))
    record_keys = set()
    for key in ("bank_records", "investment_records", "activity_records"):
        for record in (doc.get(key) or [])[:10]:
            record_keys.update(record.keys())
    shape = {"format": doc.get("format"), "pipeline": doc.get("type"),
             "fields": field_names, "features": features,
             "record_keys": sorted(record_keys)}
    encoded = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    return {**shape, "fingerprint": hashlib.sha256(encoded).hexdigest()[:16]}


def _compare_structure(doc: dict, slug: str) -> dict:
    """Compare a newly parsed source with existing parser/schema outcomes."""
    profile = _structure_profile(doc)
    same, compatible = [], []
    for prior_path in EXTRACTED.glob("*.json"):
        if prior_path.name in {"summary.json", f"{slug}.json"}:
            continue
        prior = common.load_json(prior_path) or {}
        prior_profile = (prior.get("pipeline_check") or {}).get("structure")
        if not prior_profile:
            prior_profile = _structure_profile(prior)
        if (prior_profile.get("format"), prior_profile.get("pipeline")) == (
                profile["format"], profile["pipeline"]):
            compatible.append(prior.get("file") or prior_path.stem)
            if prior_profile.get("fingerprint") == profile["fingerprint"]:
                same.append(prior.get("file") or prior_path.stem)
    dedupe = {
        "bank_ledger": "canonical transaction key",
        "card_ledger": "statement month plus transaction key",
        "investment_history": "account/date/action/security/quantity/amount key",
        "workbook": "account/security position key plus activity transaction key",
        "paystub": "pay-period end date",
        "ss_statement": "household member and benefit age",
        "mortgage": "latest statement fields",
        "escrow_analysis": "latest statement fields",
        "vesting_schedule": "award date/type plus vest date, shares, strike and condition",
    }.get(doc.get("type"), "document identity")
    return {"structure": profile, "same_structure_as": sorted(same),
            "compatible_pipeline_docs": sorted(compatible),
            "pipeline_confirmed": bool(same or compatible),
            "dedupe_strategy": dedupe}


def merge(per_doc: dict[str, dict]) -> dict:
    fields: dict[str, dict] = {}
    accounts, holdings, expenses = [], [], []
    aliases: dict[str, str] = {}
    fee_charges: dict[tuple, dict] = {}
    income_activity: dict[tuple, dict] = {}
    bank_exports: list[list[dict]] = []
    investment_exports: list[list[dict]] = []
    activity_exports: list[list[dict]] = []
    investment_activity_exports: list[list[dict]] = []
    payroll_history: list[dict] = []
    spouse_ss: dict = {}
    source_documents: list[dict] = []
    bank = None
    cards: list[dict] = []
    inv = None
    vest_schedules: list[dict] = []
    for slug, doc in per_doc.items():
        source = doc.get("_source") or {}
        check = doc.get("pipeline_check") or {}
        source_documents.append({
            "name": doc.get("file"), "format": doc.get("format"),
            "pipeline": doc.get("type"), "sha256": source.get("sha256"),
            "extracted_at": source.get("extracted_at"),
            "duplicate_of": doc.get("duplicate_of"),
            "structure_fingerprint": (check.get("structure") or {}).get("fingerprint"),
            "same_structure_as": check.get("same_structure_as") or [],
            "pipeline_confirmed": check.get("pipeline_confirmed", False),
            "dedupe_strategy": check.get("dedupe_strategy"),
        })
        if doc.get("duplicate_of"):
            continue
        if doc.get("type") == "ss_statement" and doc.get("ss_role") == "spouse":
            schedule = ((doc.get("fields", {}).get("benefit_by_age") or {})
                        .get("value") or {})
            if not schedule:
                schedule = {
                    age: doc.get("fields", {}).get(f"benefit_{age}", {}).get("value")
                    for age in (62, 67, 70)
                    if doc.get("fields", {}).get(f"benefit_{age}", {}).get("value") is not None
                }
            if len(schedule) > len(spouse_ss.get("schedule", {})):
                spouse_ss = {"schedule": schedule, "source": doc.get("file")}
        if doc.get("type") == "paystub":
            payroll = {k: v.get("value") for k, v in doc.get("fields", {}).items()
                       if k in {"period_start", "period_end", "base_salary_current",
                                "gross_ytd", "net_pay", "k401_current", "k401_ytd",
                                "k401_match_current", "k401_pct", "employer_match_pct"}
                       and v.get("value") is not None}
            payroll["source"] = doc.get("file")
            payroll_history.append(payroll)
        if doc.get("bank_records"):
            bank_exports.append(doc["bank_records"])
        if doc.get("investment_records"):
            investment_exports.append(doc["investment_records"])
        if doc.get("activity_records"):
            activity_exports.append(doc["activity_records"])
            if doc.get("type") in {"investment_history", "workbook"}:
                investment_activity_exports.append(doc["activity_records"])
        if doc.get("spending") and doc["spending"].get("n_months"):
            # prefer the ledger covering the most months
            if bank is None or doc["spending"]["n_months"] > bank["n_months"]:
                bank = doc["spending"]
        if doc.get("card") and doc["card"].get("n_months"):
            cards.append(doc["card"])
        if doc.get("investments") and doc["investments"].get("n_months"):
            if inv is None or doc["investments"]["n_months"] > inv["n_months"]:
                inv = doc["investments"]
        if doc.get("vesting") and doc["vesting"].get("n_future"):
            schedule = dict(doc["vesting"])
            schedule["source_file"] = doc.get("file")
            vest_schedules.append(schedule)
        for k, v in doc.get("fields", {}).items():
            if k.startswith("_"):
                continue
            if doc.get("type") == "ss_statement" and doc.get("ss_role") == "spouse":
                continue
            if k not in fields or (v.get("confidence", 0) > fields[k].get("confidence", 0)):
                if v.get("value") is not None:
                    fields[k] = v
        accounts += doc.get("account_candidates", [])
        holdings += doc.get("holding_candidates", [])
        expenses += doc.get("expense_candidates", [])
        # aliases can come from a different file than the positions themselves
        for num, name in (doc.get("account_aliases") or {}).items():
            aliases.setdefault(num, name)
        # the same row can appear in overlapping activity exports
        for fc in doc.get("fee_charges") or []:
            fee_charges.setdefault(
                (fc["account"].strip().lower(), fc["date"], fc["amount"]), fc)
        for ic in doc.get("income_activity") or []:
            income_activity.setdefault(
                (ic["account"].strip().lower(), ic["date"], ic["amount"],
                 ic.get("symbol") or ""), ic)
    if bank_exports:
        bank_records = _merge_record_exports(
            bank_exports,
            lambda r: (r.get("date"),
                       str(r.get("description") or "").strip().lower(),
                       round(float(r.get("amount") or 0), 2)))
        bank = spending.analyze_records(bank_records)
    activity_records = _merge_record_exports(activity_exports, _record_key)
    # Activity sheets from every broker belong in one deduplicated analysis.
    # Previously only Fidelity-shaped investment_history exports fed `inv`, so
    # Wells Fargo buys and sells were retained for position roll-forward but
    # omitted from the dashboard's investment-activity totals.
    investment_activity_records = _merge_record_exports(
        investment_activity_exports, _record_key)
    if investment_activity_records:
        inv = investments.analyze_records(
            investment_activity_records, _sale_cost_overrides())
    elif investment_exports:
        investment_records = _merge_record_exports(investment_exports, _record_key)
        inv = investments.analyze_records(
            investment_records, _sale_cost_overrides())
    if inv is not None:
        source_rows = sum(len(export) for export in investment_activity_exports)
        inv["activity_source_rows"] = source_rows
        inv["activity_unique_rows"] = len(investment_activity_records)
        inv["activity_duplicate_rows_excluded"] = max(
            0, source_rows - len(investment_activity_records))
    rollforward = _roll_forward_holdings(holdings, accounts, activity_records)
    vest = vesting.merge_schedules(vest_schedules)
    # Combine the spending feeds: card statements replace the payment proxy in
    # checking for the months they cover, and HSA outflows add spending that
    # never passes through checking at all.
    spend = bank
    if bank and (cards or (inv and inv.get("hsa_spending_monthly"))):
        extra = []
        if inv and inv.get("hsa_spending_by_month"):
            extra.append({"label": "HSA (Custodian)", "category": "medical",
                          "by_month": inv["hsa_spending_by_month"],
                          "n": sum(1 for _ in inv["hsa_spending_by_month"])})
        spend = spending.combine(bank, cards, extra=extra)
    elif bank is None and cards:
        spend = cards[0]

    return {"generated": common.now_iso(), "fields": fields,
            "source_documents": source_documents,
            "account_candidates": accounts, "holding_candidates": holdings,
            "expense_candidates": expenses, "account_aliases": aliases,
            "fee_charges": sorted(fee_charges.values(), key=lambda c: c["date"]),
            "income_activity": sorted(income_activity.values(),
                                      key=lambda c: c["date"]),
            "spending": spend, "investments": inv, "vesting": vest,
            "activity_rollforward": rollforward,
            "spouse_social_security": spouse_ss or None,
            "payroll_history": sorted(payroll_history,
                                      key=lambda r: str(r.get("period_end") or ""))}


# ---------------------------------------------------------------------- main --
def report(fname: str, doc_type: str, fields: dict, extra: str = "") -> list[str]:
    lines = [f"[extract] {fname}  type={doc_type}{extra}"]
    for k, v in fields.items():
        if k.startswith("_"):
            continue
        found = v.get("value") is not None
        meth = (v.get("source") or {}).get("method", "")
        shown = v.get("value")
        if isinstance(shown, dict):
            shown = f"{len(shown)} entries"
        lines.append(f"  {k:<22}: {'= ' + str(shown) if found else 'MISSING'}"
                     f"  (conf={v.get('confidence', 0):.2f}, {meth})")
    cc = fields.get("_crosscheck")
    if cc:
        lines.append(f"  cross-check {(cc.get('source') or {}).get('method', '')}: {cc.get('value')}")
    return lines


def selftest() -> int:
    """Synthetic checks for the activity-ledger scanner (no finance_data reads)."""
    today = datetime.now().date()
    # a posted transaction cannot be in the future: roll the year back until it
    # is in the past — one step is usually enough, more when the day-of-year is
    # also still ahead of today
    future = f"{today.year + 1:04d}-11-06"
    fixed_date, original = _repair_ledger_date(future)
    assert original == future and fixed_date <= today.isoformat(), fixed_date
    assert fixed_date.endswith("-11-06")
    # the newest date that works, not an arbitrarily old one
    assert int(fixed_date[:4]) >= today.year - 1
    # a date already in the past is left exactly as it was
    assert _repair_ledger_date("2025-11-06") == ("2025-11-06", None)
    assert _repair_ledger_date(fixed_date) == (fixed_date, None)   # idempotent
    assert _repair_ledger_date(None) == (None, None)

    grid = [["Account Activity For The Period"],
            ["Date", "Account", "Activity", "Symbol", "CUSIP", "Description",
             "Quantity", "Price 1", "Amount 2"],
            ["07/10/2026", "IRA(*1234)", "Advisory Fee", "", "", "PIM\tQUARTERLY FEE",
             "", "", "-5457.93"],
            [f"11/06/{today.year + 1}", "IRA(*1234)", "Advisory Fee", "", "",
             "PIM\tQUARTERLY FEE", "", "", "-2709.26"],
            ["07/31/2026", "IRA(*1234)", "Dividend", "BND", "000000000",
             "TOTAL BOND ETF\t073126", "", "", "179.86"],
            ["07/31/2026", "IRA(*1234)", "Interest", "", "", "SWEEP", "", "", "37.63"],
            ["07/15/2026", "IRA(*1234)", "Buy", "ACME", "000000000", "ACME CORP",
             "10", "500", "-5000"]]
    act = scan_activity({"sheet1": grid}, "test.xlsx")
    assert len(act["fees"]) == 2, act["fees"]
    fee = act["fees"][0]
    assert fee["billed"] == "quarterly" and fee["periods_per_year"] == 4
    assert fee["amount"] == 5457.93 and fee["account"] == "IRA(*1234)"
    # the impossible date is repaired in the stored record, with the original kept
    assert act["fees"][1]["date"] == fixed_date, act["fees"][1]
    assert act["fees"][1]["date_repaired_from"] == f"{today.year + 1:04d}-11-06"
    # Income is harvested and all activity is retained for the roll-forward.
    assert {i["activity"] for i in act["income"]} == {"Dividend", "Interest"}
    assert [i["symbol"] for i in act["income"]] == ["BND", None]
    assert len(act["records"]) == 5
    assert act["records"][-1]["quantity"] == 10
    # a positions export has no activity rows to find
    assert scan_activity({"s": [["Symbol", "Market Value"], ["MSFT", "1000"]]},
                         "p.xlsx") == {"fees": [], "income": [], "records": []}
    assert _snapshot_date("Portfolio_Positions_Aug-01-2026.csv") == "2026-08-01"
    assert _snapshot_date("sac_Portfolio_Positions_080126.xlsx") == "2026-08-01"
    hs = [{"account": "IRA(*1234)", "symbol": "ABC", "description": "ABC",
           "quantity": 10.0, "value": 100.0, "cost_basis": 80.0,
           "snapshot_date": "2026-08-01"},
          {"account": "IRA(*1234)", "symbol": None,
           "description": "Bank Deposit Sweep", "quantity": None,
           "value": 20.0, "cost_basis": None, "snapshot_date": "2026-08-01"}]
    ac = [{"label": "*1234", "value": 120.0, "sheet": "positions"}]
    rf = _roll_forward_holdings(hs, ac, [
        {"account": "IRA(*1234)", "date": "2026-08-02", "action": "Sell",
         "symbol": "ABC", "description": "ABC", "quantity": -10,
         "price": 12, "amount": 120},
        {"account": "IRA(*1234)", "date": "2026-08-03", "action": "Buy",
         "symbol": "XYZ", "description": "XYZ", "quantity": 5,
         "price": 10, "amount": -50},
    ])
    assert rf["records_applied"] == 2 and rf["share_deltas"] == {"ABC": -10.0,
                                                                  "XYZ": 5.0}
    assert not any(h.get("symbol") == "ABC" for h in hs)
    assert next(h for h in hs if h.get("symbol") == "XYZ")["quantity"] == 5
    assert next(h for h in hs if "Sweep" in (h.get("description") or ""))["value"] == 90
    print("extract self-test OK")
    return 0


def _stamp(path) -> dict:
    """Identity of a source file, for deciding whether its parse still stands."""
    st = path.stat()
    return {"name": path.name, "mtime": st.st_mtime, "size": st.st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _cached_doc(slug: str, stamp: dict) -> dict | None:
    """A previous parse of this exact file, if it is still the same file.

    Statements are broker exports: once written they never change, yet every
    run re-parsed every PDF and workbook in the inbox from scratch. Keyed on
    (mtime, size), so an edited or replaced file re-parses and an untouched one
    is simply reused. --force ignores this entirely.
    """
    doc = common.load_json(EXTRACTED / f"{slug}.json")
    if not doc:
        return None
    prev = doc.get("_source") or {}
    if (prev.get("mtime") == stamp["mtime"] and prev.get("size") == stamp["size"]
            and prev.get("sha256") == stamp.get("sha256")):
        return doc
    return None


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    force = "--force" in sys.argv
    common.ensure_dirs()
    files = [p for p in sorted(INBOX.iterdir())
             if p.is_file() and not p.name.startswith(".")]
    if not files:
        diag("[extract] inbox is empty — download or upload documents first")
        return 1
    diag(f"[extract] {len(files)} file(s) in inbox" + (" (--force: reparsing all)"
                                                       if force else ""))
    per_doc, diag_lines, failures, reused = {}, [], 0, 0
    for path in files:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name)[:80]
        stamp = _stamp(path)
        if not force:
            hit = _cached_doc(slug, stamp)
            if hit is not None:
                per_doc[slug] = hit
                reused += 1
                line = (f"[extract] {path.name}: unchanged since last run — "
                        f"reusing parse ({hit.get('type')})")
                diag_lines.append(line)
                diag(line)
                continue
        content = path.read_bytes()
        fmt = sources.sniff_format(path.name, content)
        try:
            if fmt == "pdf":
                pages, tables = parse_pdf_pages(content)
                # redact identity PII before ANY storage or extraction
                pages = redact.redact_pages(pages)
                tables = redact.redact_tables(tables)
                text = "\n".join(pages)
                doc_type = classify_doc(text, fmt)
                extractor = {"paystub": extract_paystub,
                             "mortgage": extract_mortgage,
                             "escrow_analysis": extract_escrow,
                             "ss_statement": extract_ss_statement}.get(doc_type)
                fields = extractor(pages, tables, path.name) if extractor else {}
                doc = {"file": path.name, "type": doc_type, "format": fmt,
                       "fields": fields,
                       "text_redacted": text[:15_000], "tables": tables[:20],
                       "n_pages": len(pages), "n_tables": len(tables)}
                if doc_type == "ss_statement":
                    doc["ss_role"] = _ss_role(path.name)
                extra = f"  pages={len(pages)} tables={len(tables)} chars={len(text)}"
            elif fmt in ("xlsx", "csv"):
                grids = (parse_xlsx_grids(content) if fmt == "xlsx"
                         else parse_csv_grid(content))
                grids = redact.redact_grids(grids)
                # checked before the ledger detectors: a vesting schedule has
                # dates and share counts, which an investment-history sniffer
                # will happily claim if it gets there first
                vest_schedule = vesting.parse(grids)
                is_inv, icol = is_investment_history(grids)
                is_card, ccol = is_card_ledger(grids)
                is_ledger, col = is_bank_ledger(grids)
                if vest_schedule:
                    doc_type = "vesting_schedule"
                    doc = {"file": path.name, "type": doc_type, "format": fmt,
                           "fields": {}, "vesting": vest_schedule}
                    fut = vest_schedule["future"]
                    span = (f'{fut[0]["date"]}..{fut[-1]["date"]}' if fut else "none")
                    extra = (f'  future_vests={vest_schedule["n_future"]} ({span}) '
                             f'sheet={vest_schedule["sheet"]!r} '
                             f'options={vest_schedule["has_options"]} '
                             f'conditions={len(vest_schedule["conditions"])}')
                elif is_inv:
                    doc_type = "investment_history"
                    body = _ledger_body(grids, icol, investments._month)
                    inv_records = investments.normalize(body, icol)
                    inv = investments.analyze_records(
                        inv_records, _sale_cost_overrides())
                    doc = {"file": path.name, "type": doc_type, "format": fmt,
                           "fields": {}, "investments": inv,
                           "investment_records": inv_records,
                           "activity_records": inv_records}
                    extra = (f"  txns={inv['n_transactions']} "
                             f"months={inv['n_months']} "
                             f"({inv['first_month']}..{inv['last_month']})")
                elif is_card:
                    doc_type = "card_ledger"
                    body = _ledger_body(grids, ccol, spending._month)
                    label = path.stem[:24]
                    card = spending.analyze_card(body, ccol, label)
                    card["issuer"] = (spending.issuer_of(path.name)
                                      or spending.issuer_of(
                                          " ".join(str(c) for r in body[:40] for c in r)))
                    doc = {"file": path.name, "type": doc_type, "format": fmt,
                           "fields": {}, "card": card}
                    extra = (f"  txns={card['n_transactions']} "
                             f"months={card['n_months']} "
                             f"({card['first_month']}..{card['last_month']}) "
                             f"issuer={card['issuer']}")
                elif is_ledger:
                    doc_type = "bank_ledger"
                    body = _ledger_body(grids, col, spending._month)
                    bank_records = spending.normalize(body, col)
                    sp = spending.analyze_records(bank_records)
                    doc = {"file": path.name, "type": doc_type, "format": fmt,
                           "fields": {}, "spending": sp,
                           "bank_records": bank_records}
                    extra = (f"  txns={sp.get('n_transactions', 0)} "
                             f"months={sp.get('n_months', 0)} "
                             f"({sp.get('first_month')}..{sp.get('last_month')})")
                else:
                    doc_type = "workbook"
                    wb = extract_workbook(grids, path.name)
                    doc = {"file": path.name, "type": doc_type, "format": fmt,
                           "fields": {}, **wb}
                    extra = (f"  sheets={wb['n_sheets']} "
                             f"accounts={len(wb['account_candidates'])} "
                             f"holdings={len(wb['holding_candidates'])} "
                             f"rollup_rows_dropped={wb['n_rollup_rows_dropped']} "
                             f"txn_sheets_skipped={wb['n_transaction_sheets_skipped']} "
                             f"fee_charges={len(wb['fee_charges'])}")
                # an activity export can be routed anywhere by its columns, and
                # the advisory fee it records matters whatever the route
                if "fee_charges" not in doc:
                    act = scan_activity(grids, path.name)
                    doc["fee_charges"] = act["fees"]
                    doc["income_activity"] = act["income"]
                    doc.setdefault("activity_records", act["records"])
            else:
                diag(f"[extract] {path.name}: unsupported format ({fmt}) — skipped")
                continue
        except Exception as e:
            diag(f"[extract] {path.name}: PARSE FAILED ({type(e).__name__})")
            failures += 1
            continue
        stamp["extracted_at"] = common.now_iso()
        doc["_source"] = stamp
        doc["pipeline_check"] = _compare_structure(doc, slug)
        per_doc[slug] = doc
        common.save_json(EXTRACTED / f"{slug}.json", doc)
        lines = report(path.name, doc_type, doc.get("fields", {}), extra)
        if doc_type == "investment_history":
            iv = doc["investments"]
            for g in iv["groups"]:
                lines.append(f"    {g['name']:<30} n={g['n']:<4} {g['total']:>14,.0f}")
            lines.append(f"  HSA/medical spending     = "
                         f"{iv['hsa_spending_monthly']:,.0f}/mo")
            lines.append(f"  contributions            = "
                         f"{iv['contributions_monthly']:,.0f}/mo")
            if iv.get("loan_active"):
                lines.append(f"  PLAN LOAN active: repayments "
                             f"{iv['loan_repayment_monthly']:,.0f}/mo")
            elif iv.get("loan_payoff_detected"):
                lines.append(f"  plan loan PAID OFF: last repayment "
                             f"{iv['loan_last_repayment_month']} "
                             f"(final {iv['loan_final_payment']:,.0f} vs usual "
                             f"{iv['loan_typical_monthly_repayment']:,.0f}), "
                             f"{iv['loan_months_since_last_repayment']} months since")
        if doc_type == "vesting_schedule":
            vs = doc["vesting"]
            for y, slot in sorted(vesting.by_year(vs["future"], None).items()):
                lines.append(f"    {y}  {slot['shares']:>10,.2f} shares "
                             f"over {len(slot['dates'])} vest date(s)")
            if vs["withholding_rate"] is not None:
                lines.append(f"  withholding measured    = "
                             f"{vs['withholding_rate'] * 100:.1f}% "
                             f"(from {vs['withholding_from_n']} past vests)")
            for c in vs["conditions"]:
                lines.append(f"  CONDITIONAL: {c}")
        if doc_type == "card_ledger":
            cd = doc["card"]
            lines.append(f"  net charges              = {cd['avg_monthly']:,.0f}/mo "
                         f"(payments recorded {cd['payments_total']:,.0f}, "
                         f"refunds {cd['refunds_total']:,.0f})")
            for c in cd["categories"][:10]:
                lines.append(f"    {c['name']:<26} {c['monthly']:>10,.0f}/mo "
                             f"(n={c['n']})")
        if doc_type == "bank_ledger":
            sp = doc["spending"]
            lines.append(f"  avg monthly spend        = {sp['avg_monthly']:,.0f} "
                         f"(median {sp['median_monthly']:,.0f}, "
                         f"last 12mo {sp['avg_monthly_recent12']:,.0f})")
            lines.append(f"  of which mortgage        = {sp['avg_monthly_mortgage']:,.0f}"
                         f"  -> ex-mortgage {sp['avg_monthly_ex_mortgage']:,.0f}")
            lines.append(f"  net transfers (excluded) = {sp['transfers_net']:,.0f}")
            for c in sp["categories"][:14]:
                lines.append(f"    {c['name']:<26} {c['monthly']:>10,.0f}/mo "
                             f"(n={c['n']})")
        if doc_type == "workbook":
            lines += [f"  account '{a['label']}' = {a['value']:,.0f}"
                      for a in doc["account_candidates"]]
            for st in doc.get("section_totals", []):
                lines.append(f"  section total '{st['label']}' = {st['value']:,.0f}")
            rec = doc.get("reconciliation")
            if rec:
                ok = "OK" if abs(rec["pct"]) <= 0.01 else "ADJUSTED"
                lines.append(f"  reconcile: captured {rec['captured']:,.0f} vs "
                             f"statement total {rec['reported_total']:,.0f} "
                             f"(delta {rec['delta']:,.0f}, {rec['pct']:.2%}) -> {ok}")
        for fc in doc.get("fee_charges") or []:
            lines.append(f"  advisory fee {fc['date']} {fc['account']:<28} "
                         f"{fc['amount']:>10,.2f}  "
                         f"({fc['billed'] or 'period unstated'})")
        diag_lines += lines
        for ln in lines:
            diag(ln)

    # Content-identical sources are excluded before merge. Overlapping exports
    # continue through their pipeline-specific canonical-key deduplication.
    hashes: dict[str, str] = {}
    for doc in per_doc.values():
        digest = (doc.get("_source") or {}).get("sha256")
        if digest and digest in hashes:
            doc["duplicate_of"] = hashes[digest]
        elif digest:
            hashes[digest] = doc.get("file")
    summary = merge(per_doc)
    common.save_json(EXTRACTED / "summary.json", summary)
    found = [k for k in CANONICAL if k in summary["fields"]]
    review = [k for k in found if summary["fields"][k].get("needs_review")]
    missing = [k for k in CANONICAL if k not in summary["fields"]]
    tail = [f"[extract] summary: {len(per_doc)} parsed"
            + (f" ({reused} reused unchanged — run with --force to reparse)"
               if reused else "")
            + f", {failures} failures; "
            f"{len(found)}/{len(CANONICAL)} canonical fields found, "
            f"{len(review)} flagged for review",
            f"[extract] missing: {', '.join(missing) if missing else 'none'}"]

    sp = summary.get("spending") or {}
    for rec in sp.get("reconciliation", []):
        tail.append(
            f"[reconcile] {rec['label']} ({rec['issuer']}): checking paid "
            f"{rec['bank_payments_in_window']:,.0f} vs statement payments "
            f"{rec['card_payments_recorded']:,.0f} (delta {rec['delta']:,.0f}); "
            f"removed {rec['payments_removed_from_checking']:,.0f} of payment "
            f"proxy, added {rec['card_net_charges']:,.0f} of real charges")
        if rec["uncovered_months"]:
            tail.append(f"[reconcile] {rec['label']}: "
                        f"{len(rec['uncovered_months'])} month(s) outside the "
                        "statement window keep the payment proxy "
                        f"({rec['uncovered_months'][0]}..{rec['uncovered_months'][-1]})")
    if sp.get("sources"):
        tail.append(f"[reconcile] combined spending sources: "
                    f"{', '.join(sp['sources'])}; "
                    f"avg {sp.get('avg_monthly', 0):,.0f}/mo")

    # auto-apply the extraction to profile.json (user edits are preserved)
    import autoprofile
    changes = autoprofile.apply(summary)
    tail += [f"[autoprofile] {ln}" for ln in changes]
    tail.append("[extract] next: review gaps in the intake form "
                "(http://127.0.0.1:5000/finance/intake), then Analyze + Render")
    for ln in tail:
        diag(ln)
    common.save_json(EXTRACTED / "diagnostics.json",
                     {"generated": common.now_iso(), "lines": diag_lines + tail,
                      "found": found, "needs_review": review, "missing": missing})
    return 0 if per_doc else 1


if __name__ == "__main__":
    raise SystemExit(main())
