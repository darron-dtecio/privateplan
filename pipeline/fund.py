"""Fetch ETF / mutual-fund / closed-end-fund data into data/<SYMBOL>/fund.json.

A fund has no revenue or EPS, so the SEC-fundamentals pipeline cannot model it.
The questions that *do* matter for a fund are what it costs, what is actually
inside it, and how that overlaps with everything else you own — which is what
this collects.

Usage:  python pipeline/fund.py FXAIX [VTI TLT ...]
"""

from __future__ import annotations

import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

warnings.filterwarnings("ignore")


# Yahoo quoteType values that mean "pooled vehicle, no revenue or EPS of its own".
FUND_QUOTE_TYPES = {"ETF", "MUTUALFUND", "CLOSEDEND", "CLOSED-END FUND", "MONEYMARKET"}


_TICKERS: dict[str, object] = {}


def _ticker(symbol: str):
    """One yfinance Ticker per symbol, shared across this process.

    yfinance memoises `.info` on the instance, so reusing it means classify()
    and fetch() cost one round trip between them instead of one each — a fund
    used to be looked up twice, once to decide it was a fund and again to
    fetch it.
    """
    key = symbol.upper()
    if key not in _TICKERS:
        import yfinance as yf
        _TICKERS[key] = yf.Ticker(symbol)
    return _TICKERS[key]


def classify(symbol: str) -> str:
    """Return "fund", "equity" or "unknown" for a symbol, per Yahoo's quoteType.

    "unknown" means Yahoo did not answer — the caller should try the company
    pipeline and fall back rather than assume either way.
    """
    try:
        info = _ticker(symbol).info or {}
    except Exception:
        return "unknown"
    qt = str(info.get("quoteType") or "").upper().replace("_", "")
    if not qt:
        return "unknown"
    return "fund" if qt in FUND_QUOTE_TYPES else "equity"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # drop NaN


def _expense(info: dict, ops) -> tuple[float | None, float | None, str]:
    """Return (fund_fraction, category_average_fraction, source).

    Yahoo is inconsistent: `netExpenseRatio` comes back in percent (0.95 means
    0.95%) while the fund_operations table is a fraction (0.0095). Prefer the
    table and fall back to the percent field scaled down.
    """
    fund = cat = None
    source = "none"
    try:
        if ops is not None and "Annual Report Expense Ratio" in ops.index:
            row = ops.loc["Annual Report Expense Ratio"]
            vals = [_num(x) for x in row.tolist()]
            if vals and vals[0] is not None:
                fund, source = vals[0], "fund_operations"
            if len(vals) > 1 and vals[1] is not None:
                cat = vals[1]
    except Exception:
        pass
    if fund is None:
        pct = _num(info.get("netExpenseRatio")) or _num(info.get("annualReportExpenseRatio"))
        if pct is not None:
            fund, source = pct / 100.0, "netExpenseRatio"
    return fund, cat, source


def fetch(symbol: str) -> dict:
    t = _ticker(symbol)
    try:
        info = t.info or {}
    except Exception:
        info = {}

    ops = holdings = sectors = classes = None
    overview = {}
    try:
        fd = t.funds_data
        for attr, target in (("fund_operations", "ops"), ("top_holdings", "holdings"),
                             ("sector_weightings", "sectors"),
                             ("asset_classes", "classes"),
                             ("fund_overview", "overview")):
            try:
                val = getattr(fd, attr)
            except Exception:
                val = None
            if target == "ops":
                ops = val
            elif target == "holdings":
                holdings = val
            elif target == "sectors":
                sectors = val
            elif target == "classes":
                classes = val
            else:
                overview = val or {}
    except Exception:
        pass

    expense, cat_expense, exp_src = _expense(info, ops)

    top = []
    try:
        if holdings is not None and len(holdings):
            for sym, row in holdings.iterrows():
                w = _num(row.get("Holding Percent"))
                if w is None:
                    continue
                top.append({"symbol": str(sym).upper(),
                            "name": str(row.get("Name") or "")[:48],
                            "weight": round(w, 6)})
    except Exception:
        pass

    sect = {k: round(float(v), 6) for k, v in (sectors or {}).items()
            if _num(v) is not None} if isinstance(sectors, dict) else {}
    cls = {k: round(float(v), 6) for k, v in (classes or {}).items()
           if _num(v) is not None} if isinstance(classes, dict) else {}

    return {
        "symbol": symbol.upper(),
        "name": info.get("longName") or info.get("shortName") or symbol,
        "quote_type": info.get("quoteType"),
        "legal_type": info.get("legalType") or overview.get("legalType"),
        "category": info.get("category") or overview.get("categoryName"),
        "family": info.get("fundFamily") or overview.get("family"),
        "total_assets": _num(info.get("totalAssets")),
        "price": _num(info.get("regularMarketPrice")) or _num(info.get("navPrice")),
        "nav": _num(info.get("navPrice")),
        "yield": _num(info.get("yield")),
        "ytd_return": (_num(info.get("ytdReturn")) or 0) / 100
        if (_num(info.get("ytdReturn")) or 0) > 1 else _num(info.get("ytdReturn")),
        "return_3y": _num(info.get("threeYearAverageReturn")),
        "return_5y": _num(info.get("fiveYearAverageReturn")),
        "beta_3y": _num(info.get("beta3Year")),
        "expense_ratio": expense,
        "category_expense_ratio": cat_expense,
        "expense_source": exp_src,
        "top_holdings": top,
        "sector_weightings": sect,
        "asset_classes": cls,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def write(symbol: str) -> dict:
    """Fetch one fund and persist it to data/<SYMBOL>/fund.json."""
    data = fetch(symbol)
    out = ROOT / "data" / symbol.upper()
    out.mkdir(parents=True, exist_ok=True)
    (out / "fund.json").write_text(json.dumps(data, indent=1, default=str),
                                   encoding="utf-8")
    return data


def main() -> int:
    syms = [s.strip().upper() for s in sys.argv[1:] if s.strip()]
    if not syms:
        print("usage: python pipeline/fund.py SYMBOL [SYMBOL ...]")
        return 2
    ok = 0
    for s in syms:
        try:
            data = write(s)
        except Exception as e:
            print(f"[fund] {s}: FAILED ({type(e).__name__}: {e})", flush=True)
            continue
        er = data["expense_ratio"]
        print(f"[fund] {s}: {data['category'] or '?'} · "
              f"expense {er * 100:.3f}%" if er is not None else
              f"[fund] {s}: {data['category'] or '?'} · expense unknown", flush=True)
        print(f"       holdings={len(data['top_holdings'])} "
              f"sectors={len(data['sector_weightings'])} "
              f"assets={data['asset_classes'] or '{}'}", flush=True)
        ok += 1
    print(f"[fund] {ok}/{len(syms)} fetched", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
