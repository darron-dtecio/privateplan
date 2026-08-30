"""Portfolio-level analysis of the fund and ETF sleeve.

A fund cannot be valued on earnings, so the useful questions are different
from a stock's:

  * what does it cost, and what is that in dollars a year;
  * what is actually inside it, and does that duplicate shares already held
    directly (look-through exposure);
  * what does the whole portfolio really own once funds are unwrapped —
    stocks vs bonds vs cash, and which sectors.

Look-through uses each fund's published top holdings, which cover only part
of a fund, so the attributed portion is reported alongside the coverage so a
partial view is never mistaken for a complete one.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
from common import ROOT

SECTOR_LABELS = {
    "technology": "Technology", "financial_services": "Financial Services",
    "consumer_cyclical": "Consumer Cyclical", "healthcare": "Healthcare",
    "communication_services": "Communication Services",
    "industrials": "Industrials", "consumer_defensive": "Consumer Defensive",
    "energy": "Energy", "utilities": "Utilities",
    "realestate": "Real Estate", "basic_materials": "Basic Materials",
}


def load_fund(symbol: str) -> dict | None:
    return common.load_json(ROOT / "data" / symbol.upper() / "fund.json")


def _fee(f: dict, value: float) -> float | None:
    er = f.get("expense_ratio")
    return value * er if er is not None else None


def analyse(holdings: list[dict], stocks: list[dict], total: float) -> dict:
    """holdings: the fund-kind rows; stocks: modelled stock rows (for sectors)."""
    rows, missing = [], []
    fee_total = 0.0
    fee_known_value = 0.0

    for h in holdings:
        f = load_fund(h["symbol"])
        if not f:
            missing.append(h["symbol"])
            continue
        value = h["value"]
        er = f.get("expense_ratio")
        cat_er = f.get("category_expense_ratio")
        fee = _fee(f, value)
        if fee is not None:
            fee_total += fee
            fee_known_value += value
        rows.append({
            "symbol": f["symbol"], "name": f.get("name") or h["symbol"],
            "note": h.get("note", ""), "value": value,
            "weight": value / total if total else 0,
            # carried from the holding so the sleeve can be shown against what
            # was actually paid, not just what it currently costs to hold
            "cost_basis": h.get("cost_basis"), "gain": h.get("gain"),
            "gain_pct": h.get("gain_pct"),
            "category": f.get("category"), "family": f.get("family"),
            "expense_ratio": er, "category_expense_ratio": cat_er,
            "expense_source": f.get("expense_source"),
            "expense_zero": er == 0,
            "vs_category": (er - cat_er) if er is not None and cat_er else None,
            "annual_fee": fee,
            "yield": f.get("yield"), "ytd_return": f.get("ytd_return"),
            "return_3y": f.get("return_3y"), "return_5y": f.get("return_5y"),
            "beta_3y": f.get("beta_3y"), "total_assets": f.get("total_assets"),
            "n_holdings": len(f.get("top_holdings") or []),
            "asset_classes": f.get("asset_classes") or {},
            "sector_weightings": f.get("sector_weightings") or {},
            "top_holdings": f.get("top_holdings") or [],
        })
    rows.sort(key=lambda r: -r["value"])

    # ---- true asset allocation across the whole portfolio --------------------
    alloc = defaultdict(float)
    for r in rows:
        ac = r["asset_classes"]
        if ac:
            for k, w in ac.items():
                key = {"stockPosition": "stocks", "bondPosition": "bonds",
                       "cashPosition": "cash", "preferredPosition": "preferred",
                       "convertiblePosition": "convertible",
                       "otherPosition": "other"}.get(k, "other")
                alloc[key] += r["value"] * w
        else:
            alloc["unclassified"] += r["value"]

    # ---- look-through exposure ------------------------------------------------
    exposure: dict[str, dict] = {}
    for s in stocks:
        e = exposure.setdefault(s["ticker"], {"symbol": s["ticker"],
                                              "name": s.get("company") or s["ticker"],
                                              "direct": 0.0, "via_funds": 0.0,
                                              "sources": []})
        e["direct"] += s["value"]

    attributed = 0.0
    for r in rows:
        for th in r["top_holdings"]:
            sym = th["symbol"].upper().replace(".", "-")
            amt = r["value"] * th["weight"]
            attributed += amt
            e = exposure.setdefault(sym, {"symbol": sym, "name": th.get("name") or sym,
                                          "direct": 0.0, "via_funds": 0.0,
                                          "sources": []})
            e["via_funds"] += amt
            e["sources"].append({"fund": r["symbol"], "amount": round(amt, 2)})

    look = []
    for e in exposure.values():
        tot = e["direct"] + e["via_funds"]
        if tot < 100:
            continue
        e["total"] = round(tot, 2)
        e["direct"] = round(e["direct"], 2)
        e["via_funds"] = round(e["via_funds"], 2)
        e["pct_portfolio"] = tot / total if total else 0
        e["sources"] = sorted(e["sources"], key=lambda s: -s["amount"])[:4]
        look.append(e)
    look.sort(key=lambda e: -e["total"])

    fund_value = sum(r["value"] for r in rows)
    coverage = attributed / fund_value if fund_value else 0

    # ---- true sector weights ----------------------------------------------------
    sectors = defaultdict(float)
    sector_base = 0.0
    for s in stocks:
        if s.get("sector"):
            sectors[s["sector"]] += s["value"]
            sector_base += s["value"]
    for r in rows:
        sw = r["sector_weightings"]
        if not sw:
            continue
        equity = (r["asset_classes"].get("stockPosition") or 1.0) * r["value"]
        for k, w in sw.items():
            sectors[SECTOR_LABELS.get(k, k.replace("_", " ").title())] += equity * w
        sector_base += equity

    return {
        "funds": rows, "missing": missing,
        "fund_value": round(fund_value, 2),
        "annual_fee_total": round(fee_total, 2),
        "fee_coverage": (fee_known_value / fund_value) if fund_value else 0,
        "weighted_expense": (fee_total / fee_known_value) if fee_known_value else None,
        "allocation": {k: round(v, 2) for k, v in
                       sorted(alloc.items(), key=lambda kv: -kv[1])},
        "look_through": look,
        "look_through_coverage": round(coverage, 4),
        "look_through_attributed": round(attributed, 2),
        "sectors": {k: round(v, 2) for k, v in
                    sorted(sectors.items(), key=lambda kv: -kv[1])},
        "sector_base": round(sector_base, 2),
    }


def fee_projection(annual_fee: float, years: int, growth: float = 0.058) -> float:
    """Fees compound too: what the current fee level costs over `years`,
    grown with the portfolio rather than held flat."""
    total = 0.0
    for y in range(years):
        total += annual_fee * (1 + growth) ** y
    return round(total, 2)


if __name__ == "__main__":
    fake_stocks = [{"ticker": "MSFT", "company": "Microsoft", "value": 100000.0,
                    "sector": "Technology"}]
    fake_holdings = [{"symbol": "TESTF", "value": 200000.0, "note": "test"}]

    import json
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "data" / "TESTF"
        d.mkdir(parents=True)
        (d / "fund.json").write_text(json.dumps({
            "symbol": "TESTF", "name": "Test Fund", "category": "Large Blend",
            "expense_ratio": 0.005, "category_expense_ratio": 0.0078,
            "asset_classes": {"stockPosition": 0.9, "bondPosition": 0.1},
            "sector_weightings": {"technology": 0.5, "healthcare": 0.5},
            "top_holdings": [{"symbol": "MSFT", "name": "Microsoft", "weight": 0.25},
                             {"symbol": "AAPL", "name": "Apple", "weight": 0.10}],
        }))
        import common as c
        orig = c.ROOT
        globals()["ROOT"] = Path(td)
        out = analyse(fake_holdings, fake_stocks, 300000.0)
        globals()["ROOT"] = orig

    assert out["annual_fee_total"] == 1000.0, out["annual_fee_total"]
    assert out["weighted_expense"] == 0.005
    # look-through: MSFT is 100k direct + 25% of a 200k fund = 150k
    msft = next(e for e in out["look_through"] if e["symbol"] == "MSFT")
    assert msft["direct"] == 100000.0 and msft["via_funds"] == 50000.0
    assert msft["total"] == 150000.0
    assert abs(msft["pct_portfolio"] - 0.5) < 1e-9
    # only the published holdings are attributed, and coverage says so
    assert out["look_through_attributed"] == 70000.0
    assert abs(out["look_through_coverage"] - 0.35) < 1e-9
    # asset mix unwraps the fund
    assert out["allocation"]["stocks"] == 180000.0
    assert out["allocation"]["bonds"] == 20000.0
    # sectors blend direct stocks with the fund's equity sleeve
    assert abs(out["sectors"]["Technology"] - (100000 + 180000 * 0.5)) < 1
    assert abs(out["sectors"]["Healthcare"] - 90000) < 1
    assert fee_projection(1000, 3, 0.0) == 3000.0
    print("funds self-test OK")
