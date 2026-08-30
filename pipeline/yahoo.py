"""Yahoo Finance wrapper: market data + analyst consensus via yfinance.

Every field is fetched defensively — yfinance endpoints break routinely, and a
missing field should degrade the analysis, not kill the pipeline.
"""

from __future__ import annotations

import math
from datetime import datetime

import yfinance as yf


_TICKERS: dict[str, yf.Ticker] = {}


def _ticker(ticker: str) -> yf.Ticker:
    """One Ticker per symbol per process.

    fetch_market and fetch_estimates are called back to back on the same
    symbol and each used to build its own Ticker — separate objects with
    separate caches, so yfinance re-fetched the profile for the second one.
    """
    key = ticker.upper()
    if key not in _TICKERS:
        _TICKERS[key] = yf.Ticker(ticker)
    return _TICKERS[key]


def _safe(fn, default=None):
    try:
        val = fn()
        if isinstance(val, float) and math.isnan(val):
            return default
        return val
    except Exception:
        return default


def _df_to_records(df) -> list[dict] | None:
    if df is None:
        return None
    try:
        if df.empty:
            return None
        out = df.reset_index()
        out.columns = [str(c) for c in out.columns]
        records = out.to_dict(orient="records")
        for rec in records:
            for k, v in rec.items():
                if hasattr(v, "isoformat"):
                    rec[k] = v.isoformat()
                elif isinstance(v, float) and math.isnan(v):
                    rec[k] = None
        return records
    except Exception:
        return None


def fetch_market(ticker: str) -> dict:
    t = _ticker(ticker)
    info = _safe(lambda: t.info, {}) or {}

    prices = None
    try:
        hist = t.history(period="2y", interval="1d", auto_adjust=True)
        if not hist.empty:
            prices = [
                {"date": idx.date().isoformat(), "close": round(float(row["Close"]), 2)}
                for idx, row in hist.iterrows()
            ]
    except Exception:
        pass

    return {
        "ticker": ticker.upper(),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "profile": {
            "name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "currency": info.get("currency", "USD"),
        },
        "stats": {
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "market_cap": info.get("marketCap"),
            "trailing_pe": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "dividend_yield": info.get("dividendYield"),
            "beta": info.get("beta"),
            "52w_high": info.get("fiftyTwoWeekHigh"),
            "52w_low": info.get("fiftyTwoWeekLow"),
            "shares_outstanding": info.get("sharesOutstanding"),
        },
        "prices": prices,
    }


def fetch_estimates(ticker: str) -> dict:
    t = _ticker(ticker)
    return {
        "ticker": ticker.upper(),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        # rows indexed 0q (current qtr), +1q, 0y (current FY), +1y
        "earnings_estimate": _safe(lambda: _df_to_records(t.earnings_estimate)),
        "revenue_estimate": _safe(lambda: _df_to_records(t.revenue_estimate)),
        "eps_trend": _safe(lambda: _df_to_records(t.eps_trend)),
        "growth_estimates": _safe(lambda: _df_to_records(t.growth_estimates)),
        "price_targets": _safe(lambda: t.analyst_price_targets),
        "recommendations": _safe(lambda: _df_to_records(t.recommendations_summary)),
        # actual vs estimate for recent quarters (beat/miss context)
        "earnings_history": _safe(lambda: _df_to_records(t.earnings_history)),
        "next_earnings_dates": _safe(
            lambda: [d.isoformat() for d in t.earnings_dates.index[:4]]
        ),
    }


if __name__ == "__main__":
    import json
    import sys

    tk = sys.argv[1] if len(sys.argv) > 1 else "MSFT"
    est = fetch_estimates(tk)
    print(json.dumps({k: v for k, v in est.items() if k != "fetched_at"}, indent=2, default=str)[:3000])
