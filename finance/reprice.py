"""Value the profile at today's prices instead of the statement date.

profile.json is a record of what a statement said: how many shares, what they
were worth the day the export was cut, what they cost. Markets move. By the
time a plan is being read, the balance it was built on can be tens of thousands
of dollars out, and every number downstream — net worth, allocation, the
projection, the Monte Carlo, the sustainable-spending solve — inherits that
staleness while looking authoritative.

This applies `finance_data/prices.json` (written by prices.py) to the statement
snapshot: share count x today's quote, per position and per account, with the
gain or loss against the statement value carried alongside so the adjustment is
visible rather than silent. profile.json is never rewritten — the statement
record stays intact and a bad quote is undone by deleting prices.json.

Rules that keep this honest:

  * Reprice only when the share count covers the whole position. A partial
    quantity times a live price understates the holding badly, so those rows
    keep their statement value.
  * Cash and money-market rows are not repriced; a dollar is a dollar.
  * An account is adjusted by the sum of its own repriced positions, so the
    gain lands in the right tax bucket — a deferred-account gain is not the
    same as a taxable one to anything downstream.
  * Whatever cannot be priced is reported as coverage, not quietly treated as
    unchanged.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

PRICES_PATH = common.FIN_DATA / "prices.json"
# Yahoo spells share classes with a dash; brokerages use a dot.
SYMBOL_FIXUPS = {"BRK.B": "BRK-B", "BF.B": "BF-B"}
# A repriced position needs a share count covering essentially all of it.
QTY_COVERAGE_MIN = 0.95
MASKED = re.compile(r"\*(\d{3,6})")


def load_quotes(path: Path | None = None) -> dict:
    data = common.load_json(path or PRICES_PATH) or {}
    return data.get("quotes") or {}


def fetched_at(path: Path | None = None) -> str | None:
    return (common.load_json(path or PRICES_PATH) or {}).get("fetched_at")


def quote_for(symbol: str, quotes: dict) -> float | None:
    sym = (symbol or "").strip().upper()
    q = quotes.get(SYMBOL_FIXUPS.get(sym, sym)) or quotes.get(sym)
    price = (q or {}).get("price")
    return float(price) if price else None


def market_value(h: dict, quotes: dict, is_cash: bool = False) -> tuple[float, float | None]:
    """(value to use, live price used or None) for one holding row.

    Returns the statement value untouched whenever the position cannot be
    repriced honestly — no quote, no share count, or a share count that covers
    only part of the position.
    """
    stmt = float(h.get("value") or 0)
    qty = h.get("quantity")
    if is_cash or not qty:
        return stmt, None
    covers = h.get("qty_covers")
    if covers is not None and stmt and covers / stmt < QTY_COVERAGE_MIN:
        return stmt, None
    price = quote_for(h.get("symbol") or "", quotes)
    if price is None:
        return stmt, None
    return round(float(qty) * price, 2), price


def _is_cash(h: dict) -> bool:
    import portfolio
    kind, _ = portfolio.classify(h.get("symbol") or "",
                                 h.get("description") or "")
    return kind == "cash"


def reprice_holdings(holdings: list[dict], quotes: dict) -> tuple[list[dict], dict]:
    """Repriced copies of the holdings, plus what the repricing did."""
    out, priced_value, stale_value, delta = [], 0.0, 0.0, 0.0
    for h in holdings:
        stmt = float(h.get("value") or 0)
        value, price = market_value(h, quotes, _is_cash(h))
        row = dict(h, value=value)
        if price is not None:
            row["statement_value"] = stmt
            row["live_price"] = price
            row["price_change"] = round(value - stmt, 2)
            priced_value += value
            delta += value - stmt
        else:
            stale_value += stmt
        out.append(row)
    out.sort(key=lambda r: -(r.get("value") or 0))
    total = priced_value + stale_value
    return out, {
        "n": len(out), "n_priced": sum(1 for r in out if r.get("live_price")),
        "total": round(total, 2),
        "delta": round(delta, 2),
        "statement_total": round(total - delta, 2),
        "coverage": round(priced_value / total, 4) if total else 0.0,
    }


def account_deltas(quotes: dict) -> dict[str, dict]:
    """Per-account market movement since the statement, by account label.

    Per account rather than per portfolio because the projection keeps taxable,
    tax-deferred and Roth money in separate buckets: a gain in a rollover IRA
    is taxed on the way out and one in a brokerage account is not, so the two
    cannot be pooled and spread around.
    """
    import fees
    out: dict[str, dict] = {}
    for label, rows in fees.account_holdings().items():
        stmt = market = priced = 0.0
        for h in rows:
            s = float(h.get("value") or 0)
            v, price = market_value(h, quotes, _is_cash(h))
            stmt += s
            market += v
            if price is not None:
                priced += v
        out[label] = {"statement": round(stmt, 2), "market": round(market, 2),
                      "delta": round(market - stmt, 2),
                      "coverage": round(priced / market, 4) if market else 0.0,
                      "n": len(rows)}
    return out


def _match_account(asset_name: str, deltas: dict[str, dict]) -> str | None:
    """Join a profile asset row to the extracted account it came from.

    autoprofile names an asset "401k R/O IRA (*1234)" from an alias plus the
    masked number, while the positions export labels the same account "*1234".
    The number is the part both agree on; fall back to the name when there is
    no mask (a plan account named in full, say).
    """
    name = (asset_name or "").strip()
    m = MASKED.search(name)
    if m:
        key = "*" + m.group(1)
        for label in deltas:
            if key in label:
                return label
    low = name.lower()
    for label in deltas:
        if label.strip().lower() == low:
            return label
    return None


def apply(profile: dict, quotes: dict | None = None,
          deltas: dict[str, dict] | None = None) -> tuple[dict, dict]:
    """(profile valued at today's prices, audit of what changed).

    The input profile is not modified — the statement record is the thing we
    can always fall back to.
    """
    quotes = load_quotes() if quotes is None else quotes
    if not quotes:
        return profile, {"applied": False, "reason": "no prices.json — run "
                                                     "finance/prices.py",
                         "delta": 0.0}

    holdings, stats = reprice_holdings(profile.get("holdings") or [], quotes)
    deltas = account_deltas(quotes) if deltas is None else deltas

    assets, matched, unmatched = [], [], []
    asset_delta = 0.0
    for a in profile.get("assets") or []:
        label = _match_account(a.get("name") or "", deltas)
        d = deltas.get(label, {}).get("delta", 0.0) if label else 0.0
        row = dict(a)
        if d:
            row["balance"] = round(float(a.get("balance") or 0) + d, 2)
            row["statement_balance"] = a.get("balance")
            row["price_change"] = round(d, 2)
            asset_delta += d
            matched.append({"name": a.get("name"), "account": label,
                            "delta": round(d, 2),
                            "coverage": deltas[label]["coverage"]})
        elif label is None and (a.get("balance") or 0) > 0:
            # An account we hold but cannot see positions for still counts in
            # net worth; it simply moves at the statement value until it can be
            # priced, and saying which accounts those are is the point.
            unmatched.append({"name": a.get("name"),
                              "balance": round(float(a.get("balance") or 0), 2)})
        assets.append(row)

    out = dict(profile, holdings=holdings, assets=assets)
    audit = {
        "applied": True,
        "fetched_at": fetched_at(),
        "quotes": len(quotes),
        "holdings": stats,
        "asset_delta": round(asset_delta, 2),
        "accounts_repriced": sorted(matched, key=lambda r: -abs(r["delta"])),
        "accounts_unpriced": sorted(unmatched, key=lambda r: -r["balance"]),
        "statement_portfolio": round(sum(float(a.get("balance") or 0)
                                         for a in profile.get("assets") or []), 2),
        "market_portfolio": round(sum(float(a.get("balance") or 0)
                                      for a in assets), 2),
    }
    return out, audit


if __name__ == "__main__":
    q = {"AAA": {"price": 12.0}, "BBB": {"price": 5.0}, "BRK-B": {"price": 100.0}}

    # ---- one position at a time ---------------------------------------------
    full = {"symbol": "AAA", "value": 1000.0, "quantity": 100.0, "qty_covers": 1000.0}
    assert market_value(full, q) == (1200.0, 12.0)
    # a share count covering only part of the position cannot be scaled up
    part = dict(full, qty_covers=400.0)
    assert market_value(part, q) == (1000.0, None)
    # no quote, no quantity, or cash: the statement value stands
    assert market_value({"symbol": "ZZZ", "value": 900.0, "quantity": 9.0}, q) == (900.0, None)
    assert market_value({"symbol": "AAA", "value": 900.0}, q) == (900.0, None)
    assert market_value(full, q, is_cash=True) == (1000.0, None)
    # the brokerage spelling of a share class finds the Yahoo one
    assert quote_for("BRK.B", q) == 100.0

    # ---- a list of them ------------------------------------------------------
    hold = [full,
            {"symbol": "BBB", "value": 500.0, "quantity": 80.0, "qty_covers": 500.0},
            {"symbol": "ZZZ", "value": 300.0, "quantity": 3.0, "qty_covers": 300.0}]
    rows, st = reprice_holdings(hold, q)
    assert st["n"] == 3 and st["n_priced"] == 2
    # 1200 + 400 + 300 (unpriced) = 1900 against 1800 on the statement
    assert st["total"] == 1900.0 and st["delta"] == 100.0
    assert st["statement_total"] == 1800.0
    assert abs(st["coverage"] - 1600 / 1900) < 1e-3
    assert rows[0]["symbol"] == "AAA" and rows[0]["price_change"] == 200.0
    assert rows[0]["statement_value"] == 1000.0
    # BBB fell: 80 x 5 = 400 against 500
    bbb = next(r for r in rows if r["symbol"] == "BBB")
    assert bbb["value"] == 400.0 and bbb["price_change"] == -100.0
    # the unpriced row is untouched and carries no live-price fields
    zzz = next(r for r in rows if r["symbol"] == "ZZZ")
    assert zzz["value"] == 300.0 and "live_price" not in zzz

    # ---- joining accounts to profile assets ----------------------------------
    d = {"*1234": {"delta": 5000.0, "coverage": 1.0},
         "ACME CORP 401K PLAN": {"delta": -200.0, "coverage": 1.0}}
    assert _match_account("401k R/O IRA (*1234)", d) == "*1234"
    assert _match_account("Acme Corp 401k Plan", d) == "ACME CORP 401K PLAN"
    assert _match_account("Some other account", d) is None

    # ---- end to end ----------------------------------------------------------
    prof = {"holdings": hold,
            "assets": [{"name": "IRA (*1234)", "type": "trad_ira", "balance": 100000.0},
                       {"name": "Savings", "type": "cash", "balance": 20000.0}]}
    # no account could be matched: balances stand, and the accounts are
    # reported as unpriced rather than quietly assumed flat
    priced, audit = apply(prof, q, deltas={})
    assert audit["applied"] is True
    # the input profile is left exactly as it was
    assert prof["assets"][0]["balance"] == 100000.0
    assert prof["holdings"][0]["value"] == 1000.0
    assert priced["assets"][0]["balance"] == 100000.0
    assert audit["asset_delta"] == 0.0
    assert [u["name"] for u in audit["accounts_unpriced"]] == ["IRA (*1234)", "Savings"]
    # holdings are still repriced even when no account could be matched
    assert priced["holdings"][0]["value"] == 1200.0
    assert audit["market_portfolio"] == audit["statement_portfolio"]

    # with account deltas available, the balance moves and the audit says so
    priced2, audit2 = apply(
        {"holdings": [], "assets": prof["assets"]}, q,
        deltas={"*1234": {"delta": 5000.0, "coverage": 0.98,
                          "statement": 95000.0, "market": 100000.0, "n": 3}})
    assert priced2["assets"][0]["balance"] == 105000.0
    assert priced2["assets"][0]["statement_balance"] == 100000.0
    assert audit2["asset_delta"] == 5000.0
    assert audit2["accounts_repriced"][0]["account"] == "*1234"
    assert audit2["market_portfolio"] - audit2["statement_portfolio"] == 5000.0
    # a cash account is not an unpriced holding to chase — it just is what it is
    assert [u["name"] for u in audit2["accounts_unpriced"]] == ["Savings"]

    # no quotes at all: unchanged profile, and a reason rather than a silent pass
    same, none_audit = apply(prof, {})
    assert same is prof and none_audit["applied"] is False
    assert "prices.py" in none_audit["reason"]

    print("reprice self-test OK")
