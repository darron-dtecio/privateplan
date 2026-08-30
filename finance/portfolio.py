"""Run the stock pipeline across the portfolio and build a combined dashboard.

Reads the holdings that the finance app extracted from the brokerage exports,
decides which of them are operating companies the SEC-backed analyzer can
model, runs fetch/forecast/render for each, and then produces one portfolio
dashboard that weights every name by what is actually held.

Usage:
    python finance/portfolio.py --list          # classify holdings, run nothing
    python finance/portfolio.py --fetch         # run the pipeline per stock
    python finance/portfolio.py --render        # build the combined dashboard
    python finance/portfolio.py --fetch --render [--limit N] [--only MSFT,AAPL]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
from common import ROOT, diag

PIPELINE = ROOT / "pipeline"
PORTFOLIO_PATH = common.FIN_DATA / "portfolio.json"
DASHBOARD_PATH = common.FIN_DATA / "portfolio.html"

# Cash sweeps and money-market positions: not investments to analyse.
CASHY = re.compile(r"SPAXX|FDRXX|FSPXX|FZFXX|SWVXX|VMFXX|FNSXX|"
                   r"BANK\s*DEPOSIT|MONEY\s*MARKET|ACCRUED|^CASH", re.I)
# Funds, ETFs and collective trusts. They have no company fundamentals in
# EDGAR, so the analyzer cannot model them — they are held as exposures.
# Shipped list lives in finance/funds_known.json; add your own holdings to
# finance_data/funds_extra.json (same shape) so updates never clobber them.
def _load_funds() -> dict[str, str]:
    known = common.load_json(Path(__file__).resolve().parent / "funds_known.json") or {}
    extra = common.load_json(common.FIN_DATA / "funds_extra.json") or {}
    return {str(k).upper(): str(v)
            for k, v in {**known, **extra}.items()
            if not str(k).startswith("_")}


FUNDS = _load_funds()


TICKER_RE = re.compile(r"^[A-Z]{1,5}([.\-][A-Z])?$")
# EDGAR and Yahoo spell share classes with a dash
TICKER_FIXUPS = {"BRK.B": "BRK-B", "BF.B": "BF-B"}


def classify(symbol: str, description: str = "") -> tuple[str, str]:
    text = f"{symbol} {description}"
    if CASHY.search(text):
        return "cash", "cash / money market"
    sym = symbol.strip().upper()
    if sym in FUNDS:
        return "fund", FUNDS[sym]
    if not TICKER_RE.match(sym):
        return "other", "unrecognised instrument"
    return "stock", ""


def load_holdings() -> list[dict]:
    profile = common.load_json(common.PROFILE_PATH) or {}
    out = []
    # Live quotes, when the price refresh has been run. Applied here rather than
    # written back into profile.json so the statement snapshot stays intact.
    quotes = (common.load_json(common.FIN_DATA / "prices.json") or {}).get("quotes") or {}

    for h in profile.get("holdings", []):
        kind, note = classify(h["symbol"], h.get("description") or "")
        sym = h["symbol"].strip().upper()
        value = float(h["value"])
        stmt_value, priced_at = value, None
        # Reprice only when the share count covers the whole position; a partial
        # quantity times a live price would understate the holding badly.
        qty = h.get("quantity")
        qcov = h.get("qty_covers")
        if qty and kind != "cash" and (qcov is None or not value or qcov / value >= 0.95):
            q = quotes.get(TICKER_FIXUPS.get(sym, sym)) or quotes.get(sym)
            if q and q.get("price"):
                value = round(float(qty) * float(q["price"]), 2)
                priced_at = q["price"]
        cost = h.get("cost_basis")
        cost = float(cost) if cost is not None else None
        # Some accounts report no cost at all — shares transferred in without a
        # basis carry "N/A" for every cost field. Rather than dropping the whole
        # position, measure the gain over just the portion that does report cost
        # and say what fraction that is. Coverage is taken against the statement
        # value because that is the basis cost_covers was measured on; comparing
        # it to a repriced value would misread a market gain as missing data.
        covers = h.get("cost_covers")
        coverage = (covers / stmt_value) if (cost is not None and covers
                                             and stmt_value) else None
        if cost is not None and coverage is None:
            coverage = 1.0
        # All lots are the same security, so value scales uniformly and the
        # covered slice of a repriced position is exact, not an approximation.
        covered_value = round(value * coverage, 2) if coverage else None
        out.append({"symbol": sym, "ticker": TICKER_FIXUPS.get(sym, sym),
                    "value": value, "cost_basis": cost,
                    "cost_coverage": coverage,
                    "covered_value": covered_value,
                    "gain": round(covered_value - cost, 2) if cost is not None else None,
                    "gain_pct": ((covered_value / cost - 1) if cost else None),
                    "quantity": qty, "live_price": priced_at,
                    "statement_value": stmt_value if priced_at else None,
                    "kind": kind, "note": note})
    return sorted(out, key=lambda x: -x["value"])


def run_pipeline(ticker: str, skip_sentiment: bool = True) -> bool:
    cmd = [sys.executable, str(PIPELINE / "fetch.py"), ticker,
           "--skip-sources"] + (["--skip-sentiment"] if skip_sentiment else [])
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        diag(f"[portfolio] {ticker}: fetch FAILED\n{(r.stdout + r.stderr)[-600:]}")
        return False
    r2 = subprocess.run([sys.executable, str(PIPELINE / "render.py"), ticker],
                        cwd=ROOT, capture_output=True, text=True, timeout=300)
    if r2.returncode != 0:
        diag(f"[portfolio] {ticker}: render FAILED\n{(r2.stdout + r2.stderr)[-400:]}")
        return False
    diag(f"[portfolio] {ticker}: OK")
    return True


def run_funds(symbols: list[str]) -> bool:
    """Funds have no SEC fundamentals; pipeline/fund.py collects what matters
    for them instead (cost, holdings, sector mix)."""
    if not symbols:
        return True
    ok = True
    for script in ("fund.py", "fund_render.py"):
        r = subprocess.run([sys.executable, str(PIPELINE / script)] + symbols,
                           cwd=ROOT, capture_output=True, text=True, timeout=1800)
        for line in (r.stdout or "").splitlines():
            diag(line)
        if r.returncode != 0:
            diag(f"[portfolio] {script} FAILED\n{(r.stderr or '')[-400:]}")
            ok = False
    return ok


def _pct(cur, prev):
    return (cur / prev - 1) if cur is not None and prev else None


def collect(ticker: str) -> dict | None:
    """Pull the headline numbers the combined view needs for one ticker."""
    d = ROOT / "data" / ticker
    fin = common.load_json(d / "financials.json")
    market = common.load_json(d / "market.json") or {}
    est = common.load_json(d / "estimates.json") or {}
    fc = common.load_json(d / "forecast.json")
    if not fin:
        return None
    stats = market.get("stats", {})
    prof = market.get("profile", {})
    qs = [q for q in fin.get("quarters", []) if q.get("revenue")]
    if not qs:
        return None   # pre-revenue: nothing for the fundamentals model to chew on
    last = qs[-1] if qs else {}
    prior = next((q for q in qs
                  if q.get("fiscal_year") == last.get("fiscal_year", 0) - 1
                  and q.get("fiscal_quarter") == last.get("fiscal_quarter")), None)
    tgt = est.get("price_targets") or {}
    price = stats.get("price")

    fc_growth = None
    if fc and fc.get("quarters"):
        fq = fc["quarters"]
        base_now = sum(q["revenue"]["base"] for q in fq[:4])
        base_out = sum(q["revenue"]["base"] for q in fq[-4:])
        if base_now:
            fc_growth = (base_out / base_now) ** (1 / 2) - 1  # 2-yr CAGR
    return {
        "ticker": ticker,
        "company": fin.get("company") or prof.get("name") or ticker,
        "sector": prof.get("sector") or "",
        "industry": prof.get("industry") or "",
        "price": price,
        "market_cap": stats.get("market_cap"),
        "trailing_pe": stats.get("trailing_pe"),
        "forward_pe": stats.get("forward_pe"),
        "dividend_yield": (stats.get("dividend_yield") or 0) / 100 or None,
        "beta": stats.get("beta"),
        "last_quarter": last.get("fiscal_label"),
        "revenue_yoy": _pct(last.get("revenue"), prior.get("revenue") if prior else None),
        "eps_yoy": _pct(last.get("eps_diluted"),
                        prior.get("eps_diluted") if prior else None),
        "net_margin": (last.get("net_income") / last["revenue"]
                       if last.get("net_income") is not None and last.get("revenue")
                       else None),
        "target_mean": tgt.get("mean"),
        "upside": _pct(tgt.get("mean"), price),
        "forecast_2y_cagr": fc_growth,
        "has_dashboard": (ROOT / "dashboards" / f"{ticker}.html").exists(),
    }


def _why_unmodellable(ticker: str) -> str:
    d = ROOT / "data" / ticker
    fin = common.load_json(d / "financials.json")
    if not fin:
        return "no SEC company facts found"
    if not [q for q in fin.get("quarters", []) if q.get("revenue")]:
        return "pre-revenue — no reported revenue to model"
    return "insufficient fundamentals"


def build(only: list[str] | None, limit: int | None, do_fetch: bool,
          skip_sentiment: bool) -> dict:
    holdings = load_holdings()
    stocks = [h for h in holdings if h["kind"] == "stock"]
    if only:
        want = {s.upper() for s in only}
        stocks = [h for h in stocks if h["ticker"] in want or h["symbol"] in want]
    if limit:
        stocks = stocks[:limit]

    fund_holdings = [h for h in holdings if h["kind"] == "fund"]
    if only:
        want = {s.upper() for s in only}
        fund_holdings = [h for h in fund_holdings if h["symbol"] in want]

    if do_fetch:
        for i, h in enumerate(stocks, 1):
            diag(f"[portfolio] ({i}/{len(stocks)}) {h['ticker']} …")
            run_pipeline(h["ticker"], skip_sentiment)
        if fund_holdings:
            diag(f"[portfolio] fetching {len(fund_holdings)} fund(s) …")
            run_funds([h["symbol"] for h in fund_holdings])

    rows, failed = [], []
    for h in stocks:
        info = collect(h["ticker"])
        if info is None:
            # Not modellable on ratios, but a pre-revenue name can still have a
            # rendered dashboard (spend, runway, narrative) worth linking to.
            failed.append({"ticker": h["ticker"], "value": h["value"],
                           "reason": _why_unmodellable(h["ticker"]),
                           "has_dashboard": (ROOT / "dashboards"
                                             / f'{h["ticker"]}.html').exists()})
            continue
        info.update({"value": h["value"], "symbol": h["symbol"],
                     "cost_basis": h.get("cost_basis"), "gain": h.get("gain"),
                     "gain_pct": h.get("gain_pct"),
                     "cost_coverage": h.get("cost_coverage"),
                     "live_price": h.get("live_price"),
                     "statement_value": h.get("statement_value")})
        rows.append(info)

    total = sum(h["value"] for h in holdings)
    for r in rows:
        r["weight"] = r["value"] / total if total else 0
    rows.sort(key=lambda r: -r["value"])

    # Portfolio-level performance, computed only over holdings that actually
    # reported a cost basis. `covered` says how much of the portfolio that is,
    # so the summary can state its own completeness instead of implying the
    # return covers everything.
    with_cost = [h for h in holdings if h.get("cost_basis") is not None]
    cost_total = round(sum(h["cost_basis"] for h in with_cost), 2)
    # only the slice of each position that reports a cost, so a partially
    # covered holding contributes its measurable part rather than all or nothing
    value_covered = round(sum(h.get("covered_value") or h["value"]
                              for h in with_cost), 2)
    partial = [h for h in with_cost if (h.get("cost_coverage") or 1) < 0.999]
    px = common.load_json(common.FIN_DATA / "prices.json") or {}
    live = [h for h in holdings if h.get("live_price")]
    pricing = {
        "fetched_at": px.get("fetched_at"),
        "priced": len(live),
        "priceable": sum(1 for h in holdings if h["kind"] != "cash"),
        "value_priced": round(sum(h["value"] for h in live), 2),
        "errors": (px.get("errors") or [])[:8],
    }

    performance = {
        "cost_basis": cost_total or None,
        "value": value_covered or None,
        "gain": round(value_covered - cost_total, 2) if with_cost else None,
        "gain_pct": (value_covered / cost_total - 1) if cost_total else None,
        "positions_with_cost": len(with_cost),
        "positions_total": len(holdings),
        "covered_pct": (value_covered / total) if total else None,
        "partial": [{"symbol": h["symbol"],
                     "coverage": round(h["cost_coverage"], 4)} for h in partial],
    }

    out = {
        "generated": common.now_iso(),
        "total_portfolio": round(total, 2),
        "performance": performance,
        "pricing": pricing,
        "stocks": rows, "failed": failed,
        "funds": [h for h in holdings if h["kind"] == "fund"],
        "cash": [h for h in holdings if h["kind"] == "cash"],
        "other": [h for h in holdings if h["kind"] == "other"],
        # The portfolio page owns investment transaction reporting. Keep the
        # normalized, de-duplicated ledger beside the holdings it explains.
        "investment_activity": (common.load_json(common.PROFILE_PATH) or {}).get(
            "investment_activity"),
    }
    for bucket in ("stocks", "funds", "cash", "other"):
        key = "value"
        out[f"{bucket}_total"] = round(
            sum(x[key] for x in out[bucket]), 2)

    # unwrap the fund sleeve: cost, contents, and how they overlap the stocks
    import funds as funds_mod
    out["fund_analysis"] = funds_mod.analyse(out["funds"], rows, total)
    out["fund_analysis"]["cash_total"] = out["cash_total"]

    # advisory fees charged on top of the funds' own costs, against the return
    # they have to earn to be worth paying
    import fees as fees_mod
    yrs_ret, yrs_hor = 20, 36
    an = common.load_json(common.FIN_DATA / "analysis.json") or {}
    pj = an.get("projection") or {}
    if pj.get("ages") and pj.get("markers"):
        yrs_ret = max(pj["markers"]["retire_age"] - pj["ages"][0], 0)
        yrs_hor = max(pj["ages"][-1] - pj["ages"][0], 0)
    out["advisory"] = fees_mod.evaluate(out, years_to_retirement=yrs_ret,
                                        years_horizon=yrs_hor)
    adv = out["advisory"]
    if adv.get("configured"):
        billed = sum(len(a.get("periods") or []) for a in adv["accounts"])
        priced = sum(1 for a in adv["accounts"] for p in (a.get("periods") or [])
                     if p.get("total_return") is not None)
        diag(f"[portfolio] advisory: {adv['fee_total_annual']:,.0f}/yr, "
             f"{billed} billing period(s), {priced} priced against the market")
        if billed and not priced:
            diag("[portfolio] no price history — run "
                 "'python finance/prices.py --history' to chart the fees "
                 "against what the account actually made")

    common.save_json(PORTFOLIO_PATH, out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="classify holdings only")
    ap.add_argument("--fetch", action="store_true", help="run the pipeline per stock")
    ap.add_argument("--render", action="store_true", help="build combined dashboard")
    ap.add_argument("--only", help="comma-separated tickers")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--with-sentiment", action="store_true")
    args = ap.parse_args()

    if args.list:
        hs = load_holdings()
        for h in hs:
            diag(f"  {h['symbol']:<24} {h['value']:>12,.0f}  {h['kind']:<6} {h['note']}")
        for k in ("stock", "fund", "cash", "other"):
            sel = [h for h in hs if h["kind"] == k]
            diag(f"[portfolio] {k}: {len(sel)} positions, "
                 f"{sum(h['value'] for h in sel):,.0f}")
        return 0

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    out = build(only, args.limit, args.fetch, not args.with_sentiment)
    diag(f"[portfolio] {len(out['stocks'])} stocks collected, "
         f"{len(out['failed'])} not modellable: "
         + (", ".join(f"{f['ticker']} ({f['reason']})" for f in out["failed"]) or "none"))

    if args.render:
        import portfolio_render
        portfolio_render.render(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
