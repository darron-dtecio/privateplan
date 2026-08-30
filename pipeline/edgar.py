"""SEC EDGAR XBRL client: official quarterly financials for any US-listed ticker.

Uses the free data.sec.gov JSON APIs (no key; requires a descriptive User-Agent).
Q4 values are derived as annual-minus-first-three-quarters because companies
file a 10-K, not a 10-Q, for their fourth fiscal quarter.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import requests

import contact

# SEC EDGAR requires a real contact address; see pipeline/contact.py.
USER_AGENT = contact.user_agent("equity research")
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "_cache"
# The ticker->CIK map changes when a company lists or delists; a day-old copy
# is fine and saves re-downloading 1.7MB for every ticker in a batch. This one
# is served from www.sec.gov, which does send Last-Modified, so an expired copy
# revalidates to a bodyless 304 rather than a fresh download.
TICKER_MAP_TTL = 86_400
# Company facts run to several MB each (NVDA is 4.5MB) and only change when the
# filer files. data.sec.gov sends no ETag or Last-Modified — checked, not
# assumed — so there is nothing to revalidate against and the age of the copy
# is the only usable signal. Six hours keeps a batch run and an afternoon of
# re-runs on one download while still picking up a new filing the same day.
# Set EDGAR_NO_CACHE=1 to bypass entirely, e.g. when a filing has just landed.
FACTS_TTL = 21_600

# us-gaap concepts to extract, in fallback preference order per metric.
CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        # Banks report "total net revenue" (net interest income plus noninterest
        # income) and tag none of the concepts above. Without this a bank's
        # revenue series simply stops — JPMorgan's ended in 2014 — and the model
        # then forecasts off decade-old quarters.
        "RevenuesNetOfInterestExpense",
    ],
    "cost_of_revenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "shares_diluted": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingDiluted",
    ],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        # Eli Lilly switched to the "Other" variant after 2022; without it a
        # company in the middle of a manufacturing build shows no capex at all.
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
    ],
    "d_and_a": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "Depreciation",
    ],
    "sbc": ["ShareBasedCompensation"],
    "dividends_paid": [
        "PaymentsOfDividends",
        "PaymentsOfDividendsCommonStock",
    ],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
}

# Balance-sheet concepts: instant facts (an `end` date but no `start`).
CONCEPTS_INSTANT: dict[str, list[str]] = {
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "stockholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "Cash",
    ],
    "short_term_investments": [
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
        "DebtSecuritiesCurrent",
    ],
    # Long-dated securities still fund operations for a pre-revenue issuer, so
    # they belong in the liquidity picture rather than being dropped.
    "long_term_investments": [
        "MarketableSecuritiesNoncurrent",
        "DebtSecuritiesAvailableForSaleExcludingAccruedInterestNoncurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
    ],
    # Total first: LongTermDebt includes current maturities, which net cash
    # should be charged for. Noncurrent-only is the fallback.
    "long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
    "inventory": ["InventoryNet"],
    "accounts_receivable": ["AccountsReceivableNetCurrent"],
    "goodwill": ["Goodwill"],
}

# Per-share and share-count metrics don't sum across quarters, so Q4 derivation differs.
NON_ADDITIVE = {"eps_diluted", "shares_diluted"}

# Cash-flow-statement metrics are filed as fiscal-YTD windows in 10-Qs (3/6/9/12
# months), so quarterly values come from differencing consecutive YTD windows.
CASH_FLOW_METRICS = {"operating_cash_flow", "capex", "d_and_a", "sbc",
                     "dividends_paid", "buybacks"}


def _get(url: str, retries: int = 3) -> dict:
    contact.warn_if_unset("SEC EDGAR")
    for attempt in range(retries):
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (403, 429):
            time.sleep(1 + attempt * 2)
            continue
        resp.raise_for_status()
    resp.raise_for_status()
    return {}


def _cache_paths(url: str) -> tuple[Path, Path]:
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{key}.json", CACHE_DIR / f"{key}.meta.json"


def _write_atomic(path: Path, text: str) -> None:
    """Replace `path` in one step so a reader never sees a half-written file.

    Batch runs have several ticker processes going at once and they share this
    cache, so a plain write would let one process read what another is still
    writing. os.replace is atomic on Windows and POSIX alike.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _get_cached(url: str, ttl: int, retries: int = 3) -> dict:
    """_get, but backed by data/_cache and SEC's own freshness headers.

    Within `ttl` the cached copy is used with no request at all. After that the
    request carries whatever validators the last response supplied, so a host
    that supports revalidation (www.sec.gov does) answers with a bodyless 304
    and the cached copy stands. data.sec.gov supplies none, so there the TTL is
    doing all the work.

    A cached copy is also the fallback when the network or SEC fails: stale
    data with a known age beats no data mid-batch.
    """
    if os.environ.get("EDGAR_NO_CACHE"):
        ttl = 0
    body_path, meta_path = _cache_paths(url)
    meta, cached = {}, None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        cached = json.loads(body_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta, cached = {}, None

    if cached is not None and (time.time() - float(meta.get("at") or 0)) < ttl:
        return cached

    headers = dict(HEADERS)
    if cached is not None:
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]

    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException:
            if cached is not None:
                return cached          # network down beats no data at all
            raise
        if resp.status_code == 304 and cached is not None:
            meta["at"] = time.time()
            _write_atomic(meta_path, json.dumps(meta))
            return cached
        if resp.status_code == 200:
            data = resp.json()
            _write_atomic(body_path, json.dumps(data))
            _write_atomic(meta_path, json.dumps({
                "at": time.time(), "url": url,
                "etag": resp.headers.get("ETag"),
                "last_modified": resp.headers.get("Last-Modified")}))
            return data
        if resp.status_code in (403, 429):
            time.sleep(1 + attempt * 2)
            continue
        if cached is not None:
            return cached
        resp.raise_for_status()
    if cached is not None:
        return cached
    resp.raise_for_status()
    return {}


class TickerNotFound(ValueError):
    """Symbol is absent from SEC's operating-company list.

    Usually means it is not an operating company at all — ETFs, mutual funds and
    closed-end funds file under a trust CIK and never appear here. Callers can
    catch this specifically to route the symbol to the fund pipeline.
    """


def ticker_to_cik(ticker: str) -> tuple[int, str]:
    """Return (CIK, registrant title) for a ticker symbol."""
    entry = _ticker_index().get(ticker.upper())
    if entry is None:
        raise TickerNotFound(f"Ticker {ticker!r} not found in SEC company list")
    return int(entry["cik_str"]), entry["title"]


_TICKER_INDEX: dict[str, dict] | None = None
_TICKER_INDEX_LOCK = threading.Lock()


def _ticker_index() -> dict[str, dict]:
    """symbol -> entry, inverted once per run rather than scanned per lookup.

    Guarded because a batch run resolves several tickers on parallel threads
    and would otherwise build the same 10k-entry index in each of them.
    """
    global _TICKER_INDEX
    with _TICKER_INDEX_LOCK:
        if _TICKER_INDEX is None:
            data = _get_cached(TICKER_MAP_URL, TICKER_MAP_TTL)
            _TICKER_INDEX = {e["ticker"].upper(): e for e in data.values()
                             if e.get("ticker")}
        return _TICKER_INDEX


@dataclass
class Period:
    start: date | None   # None for instant (balance-sheet) facts
    end: date
    value: float
    fy: int          # fiscal year per the filing (e.g. 2026)
    fp: str          # FY, Q1, Q2, Q3 (Q4 only via derivation)
    form: str        # 10-K, 10-Q, ...
    filed: date
    rank: int = 0    # index of the source concept in its fallback list (lower = preferred)

    @property
    def months(self) -> int:
        if self.start is None:
            return 0
        return round((self.end - self.start).days / 30.4)


def _parse_units(units: dict, want_instant: bool = False, rank: int = 0) -> list[Period]:
    out = []
    unit_key = next((k for k in ("USD", "USD/shares", "shares") if k in units), None)
    if unit_key is None:
        return out
    for item in units[unit_key]:
        if not item.get("end") or item.get("val") is None:
            continue
        if want_instant:
            if item.get("start"):
                continue
        elif not item.get("start"):
            continue
        if item.get("form") not in ("10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F"):
            continue
        out.append(Period(
            start=date.fromisoformat(item["start"]) if item.get("start") else None,
            end=date.fromisoformat(item["end"]),
            value=float(item["val"]),
            fy=item.get("fy") or 0,
            fp=item.get("fp") or "",
            form=item["form"],
            filed=date.fromisoformat(item["filed"]),
            rank=rank,
        ))
    return out


def _dedupe_latest(periods: list[Period]) -> dict[tuple[date, date], Period]:
    """Keep the most recently filed value per (start, end) window.

    Deliberately does not tie-break on concept preference the way instant facts
    do. Duration windows feed the annual-minus-quarters Q4 derivation, and for
    issuers that restate (discontinued operations, reverse mergers) the same
    window is filed under different concepts with different scopes; forcing a
    preferred concept can pair an annual from one scope with quarters from
    another and derive a nonsense (even negative) residual.
    """
    best: dict[tuple[date, date], Period] = {}
    for p in periods:
        key = (p.start, p.end)
        if key not in best or p.filed >= best[key].filed:
            best[key] = p
    return best


def _collect_periods(facts: dict, concepts: list[str], want_instant: bool = False) -> list[Period]:
    """Merge periods from every fallback concept.

    Companies sometimes switch which XBRL tag they file revenue/capex/etc.
    under partway through their history (e.g. NVIDIA moved off
    RevenueFromContractWithCustomerExcludingAssessedTax circa FY2023 back to
    plain Revenues). Merge periods from every fallback concept rather than
    stopping at the first one that has any data, so older filings under one
    tag don't shadow newer filings under another.
    """
    gaap = facts.get("facts", {}).get("us-gaap", {})
    periods: list[Period] = []
    for rank, concept in enumerate(concepts):
        if concept in gaap:
            periods.extend(_parse_units(gaap[concept].get("units", {}), want_instant, rank))
    return periods


def _extract_metric(facts: dict, concepts: list[str], additive: bool) -> dict[date, dict]:
    """Return {quarter_end_date: {value, source}} of ~3-month periods.

    For additive metrics, missing fiscal Q4s are derived as FY total minus the
    three quarterly values inside the same annual window.
    """
    periods = _collect_periods(facts, concepts)
    if not periods:
        return {}

    best = _dedupe_latest(periods)
    quarters = {p.end: p for p in best.values() if 2 <= p.months <= 4}
    annuals = [p for p in best.values() if 11 <= p.months <= 13]

    out = {end: {"value": p.value, "source": "reported"} for end, p in quarters.items()}

    for ann in annuals:
        if ann.end in out:
            continue
        inside = [q for q in quarters.values() if ann.start <= q.start and q.end < ann.end]
        if additive:
            if len(inside) == 3:
                q4 = ann.value - sum(q.value for q in inside)
                out[ann.end] = {"value": q4, "source": "derived_q4"}
        elif len(inside) == 3:
            # Annual EPS ≈ sum of quarterly EPS (small drift from buybacks), so
            # subtraction still works. Share counts average rather than sum, so
            # approximate Q4 shares with the annual weighted average instead.
            if concepts is CONCEPTS["shares_diluted"]:
                out[ann.end] = {"value": ann.value, "source": "derived_q4_approx"}
            else:
                q4 = ann.value - sum(q.value for q in inside)
                out[ann.end] = {"value": q4, "source": "derived_q4_approx"}
    return out


def _extract_ytd_metric(facts: dict, concepts: list[str]) -> dict[date, dict]:
    """Extract quarterly values for cash-flow metrics filed as fiscal-YTD windows.

    10-Qs report the cash-flow statement cumulatively (Q2 is a 6-month window,
    Q3 nine months, the 10-K twelve), so all windows of one fiscal year share a
    start date. Quarterly values are differences between consecutive windows in
    the same start-date group; discrete ~3-month windows are kept as reported.
    """
    periods = _collect_periods(facts, concepts)
    if not periods:
        return {}
    best = _dedupe_latest(periods)

    out = {p.end: {"value": p.value, "source": "reported"}
           for p in best.values() if 2 <= p.months <= 4}

    # Group windows by fiscal-year start; 52/53-week calendars shift starts by
    # a few days between restatements, so allow a small tolerance.
    wins = sorted(best.values(), key=lambda p: (p.start, p.end))
    groups: list[list[Period]] = []
    for p in wins:
        if groups and abs((p.start - groups[-1][0].start).days) <= 10:
            groups[-1].append(p)
        else:
            groups.append([p])

    for group in groups:
        for prev, cur in zip(group, group[1:]):
            if cur.end in out:
                continue
            gap = round((cur.end - prev.end).days / 30.4)
            if 2 <= gap <= 4:
                out[cur.end] = {"value": cur.value - prev.value,
                                "source": "derived_ytd_diff"}

    # Fallback: derive a missing fiscal Q4 as annual minus the three quarters
    # inside its window (covers years where only the 10-K window survived).
    for ann in (p for p in best.values() if 11 <= p.months <= 13):
        if ann.end in out:
            continue
        inside = [v["value"] for e, v in out.items() if ann.start < e < ann.end]
        if len(inside) == 3:
            out[ann.end] = {"value": ann.value - sum(inside), "source": "derived_q4"}
    return out


def _extract_instant_metric(facts: dict, concepts: list[str]) -> dict[date, dict]:
    """Extract balance-sheet (instant) facts: {period_end: {value, source}}."""
    periods = _collect_periods(facts, concepts, want_instant=True)
    best: dict[date, Period] = {}
    for p in periods:
        # fy/fp metadata is unreliable on instants; key purely by end/filed.
        if p.end not in best or (p.filed, -p.rank) > (best[p.end].filed, -best[p.end].rank):
            best[p.end] = p
    return {end: {"value": p.value, "source": "reported"} for end, p in best.items()}


def _fiscal_label(end: date, fye_month: int) -> tuple[int, int, str]:
    """Return (fiscal_year, fiscal_quarter, calendar_label) for a quarter end."""
    month = end.month
    # A 52/53-week fiscal year ends on a fixed weekday, so the year end drifts a
    # few days either side of month end and some years land in the *next* month
    # (Costco: 2023-09-03, 2024-09-01, 2025-08-31 are three consecutive year
    # ends). Keying purely off the calendar month pushes those into the
    # following fiscal year, which duplicates Q4 and inflates that year's
    # revenue — enough to invert the modelled growth rate.
    prev_month = 12 if month == 1 else month - 1
    if end.day <= 7 and prev_month == fye_month:
        month = fye_month
    # Fiscal year is the year of the fiscal year end date.
    if month <= fye_month:
        fy = end.year
    else:
        fy = end.year + 1
    months_after_fye = (month - fye_month) % 12
    fq = {3: 1, 6: 2, 9: 3, 0: 4}.get(months_after_fye)
    if fq is None:  # non-standard month offsets (52/53-week calendars): round
        fq = round(months_after_fye / 3) or 4
        fq = min(fq, 4)
    cq = (end.month - 1) // 3 + 1
    return fy, fq, f"CQ{cq} {end.year}"


def _add_derived_fields(row: dict) -> None:
    """Compute convenience fields from the raw metrics on one quarter row."""
    if (row.get("gross_profit") is None and row.get("cost_of_revenue") is not None
            and row.get("revenue") is not None):
        row["gross_profit"] = row["revenue"] - row["cost_of_revenue"]
        row.setdefault("derived_fields", []).append("gross_profit")
    ocf, capex = row.get("operating_cash_flow"), row.get("capex")
    row["fcf"] = ocf - capex if ocf is not None and capex is not None else None
    row["fcf_margin"] = (row["fcf"] / row["revenue"]
                         if row["fcf"] is not None and row.get("revenue") else None)
    row["capex_to_ocf"] = capex / ocf if capex is not None and ocf else None
    if row.get("cash") is not None:
        row["net_cash"] = (row["cash"] + (row.get("short_term_investments") or 0)
                           - (row.get("long_term_debt") or 0))
        # Headline liquidity as companies report it: cash + all marketable
        # securities, including the non-current tranche.
        row["total_liquidity"] = (row["cash"] + (row.get("short_term_investments") or 0)
                                  + (row.get("long_term_investments") or 0))
    else:
        row["net_cash"] = None
        row["total_liquidity"] = None


def fetch_financials(ticker: str) -> dict:
    """Fetch and assemble quarterly financial history for a ticker."""
    cik, title = ticker_to_cik(ticker)
    facts = _get_cached(COMPANY_FACTS_URL.format(cik=cik), FACTS_TTL)

    metrics: dict[str, dict[date, dict]] = {}
    for name, concepts in CONCEPTS.items():
        if name in CASH_FLOW_METRICS:
            metrics[name] = _extract_ytd_metric(facts, concepts)
        else:
            metrics[name] = _extract_metric(facts, concepts, additive=name not in NON_ADDITIVE)
    for name, concepts in CONCEPTS_INSTANT.items():
        metrics[name] = _extract_instant_metric(facts, concepts)

    # Gap-fill net income from ProfitLoss, which includes noncontrolling
    # interests. Strictly a fallback: for a filer with meaningful NCI the two
    # differ, and NetIncomeLoss (attributable to the parent) is the one EPS is
    # struck on. But some filers tag only ProfitLoss — Broadcom stopped filing
    # NetIncomeLoss after 2019 — and leaving those quarters null makes the
    # forecast fall back to a default net margin instead of the real one.
    profit_loss = _extract_metric(facts, ["ProfitLoss"], additive=True)
    for end, item in profit_loss.items():
        if metrics["net_income"].get(end) is None:
            metrics["net_income"][end] = item

    all_ends = sorted({e for m in metrics.values() for e in m})
    if not all_ends:
        raise RuntimeError(f"No quarterly XBRL data found for {ticker}")

    # Infer fiscal-year-end month from the most recent annual 'end' month. Revenue is
    # the best anchor, but pre-revenue companies tag none, so fall back to net income.
    gaap = facts.get("facts", {}).get("us-gaap", {})
    fye_month = all_ends[-1].month
    for concept in CONCEPTS["revenue"] + CONCEPTS["net_income"]:
        if concept in gaap:
            anns = [p for p in _parse_units(gaap[concept]["units"]) if 11 <= p.months <= 13]
            if anns:
                fye_month = max(anns, key=lambda p: p.end).end.month
                break

    quarters = []
    for end in all_ends:
        fy, fq, cal = _fiscal_label(end, fye_month)
        row = {
            "end_date": end.isoformat(),
            "fiscal_year": fy,
            "fiscal_quarter": fq,
            "fiscal_label": f"FY{str(fy)[2:]} Q{fq}",
            "calendar_label": cal,
        }
        has_data = False
        for name, series in metrics.items():
            item = series.get(end)
            row[name] = item["value"] if item else None
            if item:
                has_data = True
                if item["source"] != "reported":
                    row.setdefault("derived_fields", []).append(name)
        # Anchor on any income-statement line, not revenue alone: pre-revenue
        # companies (development-stage biotech, reactor/hardware pre-commercial)
        # file full P&L and balance sheets with no revenue tag at all.
        anchored = any(row.get(k) is not None
                       for k in ("revenue", "net_income", "operating_income"))
        if has_data and anchored:
            _add_derived_fields(row)
            quarters.append(row)

    return {
        "ticker": ticker.upper(),
        "company": title,
        "cik": cik,
        "fiscal_year_end_month": fye_month,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "quarters": quarters,
    }


if __name__ == "__main__":
    import sys
    result = fetch_financials(sys.argv[1] if len(sys.argv) > 1 else "MSFT")
    print(json.dumps(result["quarters"][-6:], indent=2))
