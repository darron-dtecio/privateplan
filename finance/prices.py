"""Refresh live quotes for every holding, then rebuild the portfolio view.

Brokerage exports are a snapshot: the market value in them is whatever the
position was worth when the statement was cut. This fetches a current quote per
symbol and lets the portfolio be revalued at today's price without re-importing
anything.

Quotes are written to finance_data/prices.json and applied at portfolio-build
time. profile.json — the statement-derived record of what you actually hold and
what you paid — is never overwritten, so a bad or stale quote can always be
discarded by deleting prices.json.

Usage:
    python finance/prices.py              # refresh quotes and re-render
    python finance/prices.py --no-render  # quotes only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common
from common import diag

PRICES_PATH = common.FIN_DATA / "prices.json"
HISTORY_PATH = common.FIN_DATA / "price_history.json"
# Yahoo spells share classes with a dash; brokerages use a dot.
SYMBOL_FIXUPS = {"BRK.B": "BRK-B", "BF.B": "BF-B"}


def quotable_symbols() -> list[str]:
    """Held symbols worth quoting — skips cash sweeps and money-market funds."""
    import portfolio
    out, seen = [], set()
    for h in portfolio.load_holdings():
        if h["kind"] == "cash":
            continue
        sym = h["ticker"]
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    # The employer stock behind an unvested equity schedule usually is not held
    # yet — that is the point of it — so it appears in no position file and
    # would never be quoted. The plan cannot value the schedule without it.
    eq = (common.load_json(common.PROFILE_PATH) or {}).get("equity_comp") or {}
    sym = (eq.get("symbol") or "").strip().upper()
    if sym and sym not in seen:
        out.append(sym)
    return out


def fetch(symbols: list[str]) -> dict:
    import yfinance as yf
    quotes, errors = {}, []
    # One Tickers object issues a single batched session rather than N logins.
    ts = yf.Tickers(" ".join(symbols))
    for sym in symbols:
        try:
            fi = ts.tickers[sym].fast_info
            px = fi.get("lastPrice") or fi.get("last_price")
            if px is None or float(px) <= 0:
                errors.append(f"{sym}: no price")
                continue
            quotes[sym] = {"price": round(float(px), 4),
                           "currency": fi.get("currency") or "USD"}
        except Exception as e:                      # noqa: BLE001 - report, continue
            errors.append(f"{sym}: {type(e).__name__}")
    return {"fetched_at": common.now_iso(), "quotes": quotes, "errors": errors}


def fetch_history(symbols: list[str], start: str) -> dict:
    """Weekly closing prices per symbol from `start` (YYYY-MM-DD) to today.

    Weekly, not daily: the only consumer values an account at a handful of
    billing dates, and a weekly series is a twentieth of the payload while
    still landing within a few days of any date asked for. Split/dividend
    adjusted, so a series spanning a split does not show a phantom collapse.
    """
    import yfinance as yf
    frame = yf.download(symbols, start=start, interval="1wk",
                        auto_adjust=True, progress=False)
    if frame is None or frame.empty:
        return {"start": start, "series": {}, "errors": ["no data returned"]}
    close = frame["Close"]
    series, errors = {}, []
    for sym in symbols:
        col = close[sym] if sym in getattr(close, "columns", []) else (
            close if len(symbols) == 1 else None)
        if col is None:
            errors.append(f"{sym}: not returned")
            continue
        points = {d.strftime("%Y-%m-%d"): round(float(v), 4)
                  for d, v in col.items() if v == v}      # NaN != NaN
        if points:
            series[sym] = points
        else:
            errors.append(f"{sym}: empty series")
    return {"fetched_at": common.now_iso(), "start": start,
            "series": series, "errors": errors}


def price_on(series: dict[str, float], when: str) -> float | None:
    """Close on or before `when` — the last print the market actually made.

    A billing date can be a weekend or a holiday, and a weekly series only has
    one point per week, so the nearest *earlier* close is the honest answer.
    Nothing before the series starts is invented.
    """
    # max over a generator rather than max(list-of-all-prior): same answer, no
    # intermediate list built on every lookup, and this is called once per
    # holding per billing boundary.
    prior = max((d for d in series if d <= when), default=None)
    return series[prior] if prior is not None else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-render", action="store_true",
                    help="write prices.json but skip rebuilding the portfolio view")
    ap.add_argument("--history", metavar="YYYY-MM-DD", nargs="?", const="auto",
                    help="also fetch weekly close history from this date "
                         "(default: 15 months back, which covers a year of "
                         "quarterly advisory billing plus the period before it)")
    args = ap.parse_args()

    common.ensure_dirs()
    syms = quotable_symbols()
    if not syms:
        diag("[prices] no holdings to quote — run the extract step first")
        return 1
    diag(f"[prices] fetching {len(syms)} quotes ...")
    data = fetch(syms)
    common.save_json(PRICES_PATH, data)
    diag(f"[prices] {len(data['quotes'])} quoted, {len(data['errors'])} failed"
         + (f" ({', '.join(data['errors'][:6])})" if data["errors"] else ""))

    if args.history:
        start = args.history
        if start == "auto":
            # 15 months: a year of quarterly billing, plus the quarter before
            # the first bill so that bill has an opening value to measure from
            start = (datetime.now().date() - timedelta(days=456)).isoformat()
        # Benchmarks are only sometimes held. A blend leg missing from the
        # history disables the very comparison it exists to make, so the
        # yardsticks are fetched whether or not they are owned.
        import fees
        bench = [s for s in fees.benchmark_symbols(fees.load_config())
                 if s not in syms]
        hist_syms = syms + bench
        diag(f"[prices] fetching weekly history for {len(hist_syms)} symbols "
             f"from {start} ..."
             + (f" (incl. benchmarks {', '.join(bench)})" if bench else ""))
        hist = fetch_history(hist_syms, start)
        common.save_json(HISTORY_PATH, hist)
        diag(f"[prices] history: {len(hist['series'])} series, "
             f"{len(hist['errors'])} failed"
             + (f" ({', '.join(hist['errors'][:6])})" if hist["errors"] else ""))

    if not args.no_render:
        import portfolio
        import portfolio_render
        out = portfolio.build(None, None, False, True)
        portfolio_render.render(out)
        # The retirement plan starts from these same balances, so new prices
        # that only reached the portfolio view would leave the projection
        # compounding yesterday's number while claiming to be current.
        rc = subprocess.run([sys.executable, str(HERE / "analyze.py")],
                            capture_output=True, text=True)
        if rc.returncode:
            diag("[prices] retirement analysis FAILED — dashboard still shows "
                 "the previous prices:")
            diag((rc.stdout or rc.stderr or "")[-600:])
        else:
            subprocess.run([sys.executable, str(HERE / "render.py")],
                           capture_output=True, text=True)
            diag("[prices] retirement analysis and dashboard rebuilt at the "
                 "new prices")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
