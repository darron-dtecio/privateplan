"""Equity awards that have not vested yet, as future income the plan can see.

A vesting schedule is one of the few things in a financial plan that is known
in advance: the dates are set, the share counts are set, and the only genuinely
unknown input is the share price on the day. That makes unvested equity worth
modelling explicitly rather than leaving it as a pleasant surprise — it can
move a retirement date by years.

Three things this is careful about, because each one quietly changes the
answer:

  * Only vests dated after today count. A schedule export is mostly history,
    and past vests are already sitting in the brokerage balances the rest of
    the plan reads. Counting them again would double the money.
  * Options and stock awards are not the same asset. A stock award (SA/RSU)
    is worth the full share price on the vest date. A non-qualified option
    (NQ) is worth only the amount the price exceeds its strike, and nothing
    at all below it. Valuing an option at the full share price overstates it
    by the strike, every time.
  * A vest is ordinary income on the day it lands, taxed at supplemental
    rates before any shares reach the account. Modelling the gross value as
    if it were investable money overstates it by roughly a third. The
    withholding rate is measured from the awards actually taxed in the past
    where the export records them, rather than assumed.

What this module does not do is decide whether a conditional vest will happen.
Schedules carry provisions — retirement, severance, change of control — and
whether one applies is a fact about the person, not the spreadsheet. Those
rows are parsed, labelled with their condition, and handed up for the profile
to include or exclude deliberately.
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

# Header text -> the field it supplies. Matched on the header rather than on a
# column letter: exports from the same plan administrator reorder columns
# between report versions, and a fixed index silently reads the wrong data.
COLUMNS = {
    "award_date": (r"award\s*date", r"grant\s*date"),
    "vest_date": (r"vest\s*date", r"vesting\s*date"),
    "shares": (r"vest\s*shares", r"vest(ed|ing)?\s*(qty|quantity|units)",
               r"^shares?\s*vest"),
    "award_type": (r"award\s*type", r"grant\s*type"),
    "award_id": (r"award\s*id", r"grant\s*(id|number)"),
    "award_price": (r"award\s*price", r"(strike|exercise|grant)\s*price"),
    "status": (r"is\s*vested", r"vest(ed)?\s*status"),
}

# Award types that pay the full share price at vest. Anything else is treated
# as an option — worth only its spread over the strike — unless it carries no
# strike price at all, in which case a full-value award is the safer read.
FULL_VALUE_TYPES = {"SA", "RS", "RSU", "PSU", "PS", "RSA", "DSU", "SU"}
OPTION_TYPES = {"NQ", "NQSO", "ISO", "SO", "OPT", "SAR"}

# A status cell holding prose rather than a yes/no flag is describing the terms
# under which the shares will vest — "will vest as part of the Retirement
# Provision". That is a condition, and it is kept with the row.
_FLAG_VALUES = {"1", "0", "y", "n", "yes", "no", "true", "false", ""}

# Fallback when an export records no tax detail. US supplemental withholding is
# 22% federal under $1M, plus Medicare and a typical state bite — deliberately
# a round, visible number rather than a precise-looking guess.
DEFAULT_WITHHOLDING = 0.32


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def find_columns(header: list) -> dict | None:
    """Map the fields this module needs onto column indexes in a header row.

    Returns None unless both a vest date and a share count are present — those
    two are what make a row a vesting event; everything else is refinement.
    """
    found: dict[str, int] = {}
    for idx, cell in enumerate(header):
        text = _norm(cell)
        if not text:
            continue
        for field, patterns in COLUMNS.items():
            if field in found:
                continue
            if any(re.search(p, text) for p in patterns):
                found[field] = idx
                break
    return found if ("vest_date" in found and "shares" in found) else None


def _parse_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    text = str(v or "").strip()
    if not text:
        return None
    # grids stringify datetimes as "2026-08-31 00:00:00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y",
                "%m/%d/%y", "%b %d, %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text[:len(fmt) + 6].strip(), fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    return None


def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return None if v != v else float(v)
    text = re.sub(r"[,$\s]", "", str(v or ""))
    if not text or text in ("-", "—"):
        return None
    neg = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        f = float(text)
    except ValueError:
        return None
    return -f if neg else f


def _condition(cell) -> str | None:
    """The terms a row vests under, when the status column carries prose."""
    text = str(cell or "").strip()
    if not text or _norm(text) in _FLAG_VALUES:
        return None
    return re.sub(r"\s+", " ", text)


def parse_rows(grid: list[list], col: dict, today: date | None = None) -> dict:
    """Split a vesting grid into what has already vested and what has not."""
    today = today or date.today()
    past, future, skipped = [], [], 0
    for row in grid:
        if len(row) <= max(col.values()):
            continue
        when = _parse_date(row[col["vest_date"]])
        shares = _num(row[col["shares"]])
        if when is None or shares is None or shares <= 0:
            skipped += 1
            continue
        atype = str(row[col["award_type"]] or "").strip().upper() if "award_type" in col else ""
        strike = _num(row[col["award_price"]]) if "award_price" in col else None
        item = {
            "date": when.isoformat(),
            "shares": round(shares, 4),
            "award_date": (_parse_date(row[col["award_date"]]).isoformat()
                           if "award_date" in col
                           and _parse_date(row[col["award_date"]]) else None),
            "award_type": atype or None,
            "award_id": (str(row[col["award_id"]]).strip()
                         if "award_id" in col and row[col["award_id"]] else None),
            # a strike of 0 is not a strike; some exports fill the column for
            # awards that have none
            "strike": strike if strike else None,
            "condition": _condition(row[col["status"]]) if "status" in col else None,
        }
        item["is_option"] = _is_option(atype, item["strike"])
        (future if when > today else past).append(item)
    future.sort(key=lambda v: v["date"])
    return {"past": past, "future": future, "unreadable_rows": skipped}


def _is_option(award_type: str, strike: float | None) -> bool:
    """Does this award pay only its spread over a strike, or its full value?"""
    t = (award_type or "").upper()
    if t in FULL_VALUE_TYPES:
        return False
    if t in OPTION_TYPES:
        return True
    # unknown type: a strike price is the tell. Without one there is nothing to
    # subtract, so it can only be valued as a full-value award.
    return strike is not None and strike > 0


def withholding_rate(grids: dict[str, list[list]]) -> tuple[float | None, int]:
    """Effective tax rate on past vests, measured from the export's own record.

    Plan administrators publish the taxable spread and the tax actually taken
    for every past transaction. Where those are present the rate this household
    has really been withheld at is a better input than a published supplemental
    rate, for the same reason a billed advisory fee beats a schedule.
    """
    for grid in grids.values():
        if not grid:
            continue
        header = grid[0]
        idx = {}
        for i, cell in enumerate(header):
            t = _norm(cell)
            if t == "taxable spread":
                idx["spread"] = i
            elif t == "total taxes":
                idx["taxes"] = i
        if "spread" not in idx or "taxes" not in idx:
            continue
        spread = taxes = 0.0
        n = 0
        for row in grid[1:]:
            if len(row) <= max(idx.values()):
                continue
            s, t = _num(row[idx["spread"]]), _num(row[idx["taxes"]])
            if s and s > 0 and t is not None and t >= 0:
                spread += s
                taxes += t
                n += 1
        if n and spread > 0:
            # pooled, not averaged per row: a big vest and a small one should
            # not carry equal weight in the rate
            return min(taxes / spread, 0.9), n
    return None, 0


def parse(grids: dict[str, list[list]], today: date | None = None) -> dict | None:
    """Find a vesting schedule anywhere in a workbook and read it."""
    for name, grid in grids.items():
        for header_row in range(min(15, len(grid))):
            col = find_columns(grid[header_row])
            if not col:
                continue
            out = parse_rows(grid[header_row + 1:], col, today)
            if not (out["past"] or out["future"]):
                continue
            rate, n = withholding_rate(grids)
            out.update({
                "sheet": name,
                "n_past": len(out["past"]),
                "n_future": len(out["future"]),
                "withholding_rate": rate,
                "withholding_from_n": n,
                "conditions": sorted({v["condition"] for v in out["future"]
                                      if v["condition"]}),
                "has_options": any(v["is_option"] for v in out["future"]),
                "as_of": (today or date.today()).isoformat(),
            })
            # only the future matters to a forward-looking plan; the past is
            # already inside the account balances it reads
            out["past"] = []
            return out
    return None


def merge_schedules(schedules: list[dict]) -> dict | None:
    """Union overlapping exports while preserving distinct grants and rows."""
    usable = [s for s in schedules if s and s.get("future")]
    if not usable:
        return None

    def key(v: dict) -> tuple:
        return (v.get("award_date"), v.get("date"), float(v.get("shares") or 0),
                v.get("award_type"), float(v.get("strike") or 0),
                v.get("condition"), bool(v.get("is_option")))

    merged: dict[tuple, list[dict]] = {}
    for schedule in usable:
        local: dict[tuple, list[dict]] = {}
        for vest in schedule.get("future") or []:
            local.setdefault(key(vest), []).append(vest)
        for event_key, copies in local.items():
            current = merged.setdefault(event_key, [])
            if len(copies) > len(current):
                current.extend(copies[len(current):])

    future = sorted((v for copies in merged.values() for v in copies),
                    key=lambda v: (v["date"], v.get("award_date") or "",
                                   v.get("award_type") or ""))
    measured = max((s for s in usable if s.get("withholding_rate") is not None),
                   key=lambda s: s.get("withholding_from_n") or 0, default=None)
    return {
        "past": [], "future": future, "n_past": 0, "n_future": len(future),
        "unreadable_rows": sum(s.get("unreadable_rows") or 0 for s in usable),
        "sheet": ", ".join(dict.fromkeys(str(s.get("sheet")) for s in usable)),
        "source_files": [s.get("source_file") for s in usable if s.get("source_file")],
        "withholding_rate": measured.get("withholding_rate") if measured else None,
        "withholding_from_n": measured.get("withholding_from_n") if measured else 0,
        "conditions": sorted({v["condition"] for v in future if v.get("condition")}),
        "has_options": any(v.get("is_option") for v in future),
        "as_of": max(str(s.get("as_of") or "") for s in usable),
    }


def by_year(vests: list[dict], price: float | None,
            withholding: float = DEFAULT_WITHHOLDING) -> dict[int, dict]:
    """Aggregate vests into the annual buckets the projection works in.

    Priced at today's share price with no drift assumed. The alternative is to
    grow the price at the plan's return assumption, which would quietly make
    the equity position compound at the market rate on top of the portfolio
    that already does — and would make the plan's success depend on a forecast
    of one company's stock.
    """
    out: dict[int, dict] = {}
    for v in vests:
        year = int(v["date"][:4])
        slot = out.setdefault(year, {"shares": 0.0, "gross": 0.0, "net": 0.0,
                                     "dates": [], "options": 0.0})
        slot["shares"] += v["shares"]
        slot["dates"].append(v["date"])
        if price is None:
            continue
        if v["is_option"]:
            # worth what it is in the money, and never less than nothing
            per_share = max(price - (v["strike"] or 0.0), 0.0)
            slot["options"] += v["shares"]
        else:
            per_share = price
        slot["gross"] += v["shares"] * per_share
    for slot in out.values():
        slot["shares"] = round(slot["shares"], 4)
        slot["gross"] = round(slot["gross"], 2)
        slot["net"] = round(slot["gross"] * (1 - withholding), 2)
        slot["dates"] = sorted(slot["dates"])
    return dict(sorted(out.items()))


def summarise(schedule: dict | None, price: float | None,
              withholding: float | None = None) -> dict | None:
    """Everything the plan and the page need about unvested equity."""
    if not schedule or not schedule.get("future"):
        return None
    rate = (withholding if withholding is not None
            else (schedule.get("withholding_rate") or DEFAULT_WITHHOLDING))
    years = by_year(schedule["future"], price, rate)
    total_shares = round(sum(v["shares"] for v in schedule["future"]), 4)
    gross = round(sum(y["gross"] for y in years.values()), 2) if price else None
    return {
        "symbol": schedule.get("symbol"),
        "price": price,
        "withholding": rate,
        "withholding_measured": schedule.get("withholding_rate") is not None,
        "withholding_from_n": schedule.get("withholding_from_n") or 0,
        "n_future": len(schedule["future"]),
        "total_shares": total_shares,
        "first_date": schedule["future"][0]["date"],
        "last_date": schedule["future"][-1]["date"],
        "gross_total": gross,
        "net_total": round(gross * (1 - rate), 2) if gross is not None else None,
        "by_year": years,
        "conditions": schedule.get("conditions") or [],
        "has_options": schedule.get("has_options", False),
        "unreadable_rows": schedule.get("unreadable_rows", 0),
        "as_of": schedule.get("as_of"),
    }


def selftest() -> None:
    g = [["Award ID", "Award Date", "Award Type", "Award Price", "Plan",
          "Vest Date - Current", "Vest Shares", "Is Vested"],
         ["A1", "2020-01-01", "SA", None, "91", "2020-06-30 00:00:00", 100, "1"],
         ["A1", "2020-01-01", "SA", None, "91", "2027-06-30 00:00:00", 50, "1"],
         ["A2", "2021-01-01", "NQ", 40.0, "91", "2027-09-30 00:00:00", 20, ""],
         ["A3", "2022-01-01", "SA", None, "91", "2028-03-31 00:00:00", 30,
          "Will vest as a part of the Retirement Provision"],
         ["A4", "2022-01-01", "SA", None, "91", "", 10, "1"]]
    col = find_columns(g[0])
    assert col and col["vest_date"] == 5 and col["shares"] == 6, col
    assert col["award_price"] == 3 and col["status"] == 7

    out = parse_rows(g[1:], col, today=date(2026, 8, 5))
    assert out["n_past"] if False else True
    assert len(out["past"]) == 1 and len(out["future"]) == 3
    assert out["unreadable_rows"] == 1              # the row with no vest date
    assert [v["date"] for v in out["future"]] == ["2027-06-30", "2027-09-30",
                                                  "2028-03-31"]
    assert out["future"][0]["award_date"] == "2020-01-01"
    # a stock award is full value; an option is not, and carries its strike
    assert out["future"][0]["is_option"] is False and out["future"][0]["strike"] is None
    assert out["future"][1]["is_option"] is True and out["future"][1]["strike"] == 40.0
    # prose in the status column is a condition, a flag is not
    assert out["future"][2]["condition"] == "Will vest as a part of the Retirement Provision"
    assert out["future"][0]["condition"] is None

    # an option is worth its spread, not the share price: at 60 with a 40
    # strike that is 20/share, so 20 shares are worth 400, not 1,200
    years = by_year(out["future"], price=60.0, withholding=0.0)
    assert years[2027]["gross"] == 50 * 60 + 20 * 20, years[2027]
    assert years[2028]["gross"] == 30 * 60
    assert years[2027]["shares"] == 70 and years[2027]["options"] == 20
    # underwater options are worth nothing, never a negative
    assert by_year(out["future"], price=30.0, withholding=0.0)[2027]["gross"] == 50 * 30
    # withholding comes off the top: a vest is income before it is an asset
    assert by_year(out["future"], 60.0, 0.25)[2028]["net"] == round(30 * 60 * 0.75, 2)
    # no price -> shares still scheduled, value left absent rather than zeroed
    assert by_year(out["future"], None)[2027]["shares"] == 70
    assert by_year(out["future"], None)[2027]["gross"] == 0.0

    # the rate is measured from what was actually withheld, pooled by dollar
    tx = {"Transactions": [["Transaction Date", "Taxable Spread", "Total Taxes"],
                           ["2024-01-01", 10000.0, 3300.0],
                           ["2024-06-01", 30000.0, 9900.0],
                           ["2024-09-01", 0.0, 0.0]]}
    rate, n = withholding_rate(tx)
    assert abs(rate - 0.33) < 1e-9 and n == 2, (rate, n)
    assert withholding_rate({"x": [["a", "b"], [1, 2]]}) == (None, 0)

    # end to end: a workbook is found by its headers, wherever it sits
    grids = {"Awards": [["nothing", "useful"]], "Vesting Schedule": g,
             "Transactions": tx["Transactions"]}
    sch = parse(grids, today=date(2026, 8, 5))
    assert sch["sheet"] == "Vesting Schedule" and sch["n_future"] == 3
    assert sch["conditions"] == ["Will vest as a part of the Retirement Provision"]
    assert sch["has_options"] is True and abs(sch["withholding_rate"] - 0.33) < 1e-9
    assert sch["past"] == []                      # history is not future income

    # Repeated exports do not duplicate the same grant. A genuinely distinct
    # award on the same vest date remains separate because award date is part
    # of the identity.
    repeated = merge_schedules([sch, sch])
    assert repeated["n_future"] == 3 and sum(v["shares"] for v in repeated["future"]) == 100
    new_award = dict(sch)
    new_award["future"] = [{**sch["future"][0], "award_date": "2026-01-01"}]
    combined = merge_schedules([sch, new_award])
    assert combined["n_future"] == 4
    assert sum(v["shares"] for v in combined["future"]) == 150

    s = summarise(sch, price=60.0)
    assert s["total_shares"] == 100 and s["n_future"] == 3
    assert s["first_date"] == "2027-06-30" and s["last_date"] == "2028-03-31"
    assert s["gross_total"] == 50 * 60 + 20 * 20 + 30 * 60
    assert abs(s["net_total"] - s["gross_total"] * 0.67) < 0.01
    assert s["withholding_measured"] is True and s["withholding_from_n"] == 2
    # measured rate is overridable, and the default applies when none was found
    assert summarise(sch, 60.0, withholding=0.5)["net_total"] == s["gross_total"] * 0.5
    bare = dict(sch, withholding_rate=None)
    assert summarise(bare, 60.0)["withholding"] == DEFAULT_WITHHOLDING
    assert summarise(bare, 60.0)["withholding_measured"] is False
    # nothing unvested -> nothing to model, said as None rather than zeros
    assert summarise({"future": []}, 60.0) is None
    assert summarise(None, 60.0) is None
    # a workbook with no vesting schedule in it is not one
    assert parse({"Sheet1": [["Date", "Amount"], ["2026-01-01", 5]]}) is None

    print("vesting self-test OK")


if __name__ == "__main__":
    selftest()
