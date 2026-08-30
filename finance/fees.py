"""Advisory fees against the return they are meant to earn.

An advisory fee is charged on assets, every year, whether the advice helps or
not. The return that is supposed to justify it is a random variable. That
asymmetry is the whole subject of this module, and it is why the honest
questions are statistical ones:

  * what is the fee actually costing, as a rate and in dollars — measured from
    the charges themselves where they exist, not just the published schedule;
  * what must the advisor beat to leave you level — the fee plus the funds'
    own expense ratios, against a passive alternative holding the same risk;
  * how long a track record would it take to *prove* they are earning it —
    which, at realistic tracking error, is usually longer than an
    investing lifetime;
  * what the fee compounds to over the plan horizon.

The last two are the point. A 1% fee against 4% tracking error needs roughly a
century of data to distinguish skill from luck at conventional significance,
so "my advisor has beaten the market for six years" is not evidence of much.
Saying that with numbers attached is more useful than another cost table.

Which accounts are advised is never guessed from an account number. It is known
one of two ways: an activity export contains the charge itself, which names its
own account ("Advisory Fee ... 401k R/O IRA(*1234)") and is the best evidence
there is; or the owner declares it in finance_data/advisory_fees.json (see
finance/advisory_fees.sample.json) for accounts whose charges we never see.
Harvested charges also *measure* the rate, so the published schedule becomes a
cross-check rather than the input.
"""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import NormalDist

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
# Single definition, kept in the module that fetches the series it reads —
# this file used to carry a byte-identical copy of it.
from prices import price_on

CONFIG_PATH = common.FIN_DATA / "advisory_fees.json"
SAMPLE_PATH = Path(__file__).resolve().parent / "advisory_fees.sample.json"

# A broad-market index fund is the yardstick: what the same market exposure
# costs when nobody is being paid for advice. Overridable in the config.
DEFAULT_PASSIVE_ER = 0.0003
# Annualised standard deviation of an advised portfolio's return *around* its
# benchmark. 4% is typical for a diversified managed account that is not
# closet-indexed; a concentrated one is higher, which makes proving skill
# take even longer. This drives every detectability number, so it is stated
# rather than hidden.
DEFAULT_TRACKING_ERROR = 0.04
DEFAULT_GROWTH = 0.058

# The headline yardstick answers "should I have just bought the index instead",
# so it stays a single broad fund — that is the alternative actually on offer.
DEFAULT_BENCHMARK = "VTI"
# The second yardstick answers a different question — "did the advice earn its
# keep for the risk it took" — and for that the comparison has to hold the same
# shape of portfolio. These are the building blocks it is mixed from.
DEFAULT_BLEND_SYMBOLS = {"us_equity": "VTI", "intl_equity": "VXUS",
                         "bonds": "BND", "cash": "BIL"}
# Fund categories that mark an equity sleeve as non-US. Matched case-folded as
# substrings, so "Foreign Large Blend" and "China Region" both land.
_EXUS_HINTS = ("europe", "foreign", "internation", "global", "world",
               "emerging", "china", "japan", "india", "pacific", "latin",
               "diversified emerging")

Z_95 = NormalDist().inv_cdf(0.975)     # two-sided 95% significance
Z_80 = NormalDist().inv_cdf(0.80)      # 80% power


def load_config() -> dict | None:
    return common.load_json(CONFIG_PATH)


def install_sample() -> Path:
    """Drop the template into finance_data/ so there is something to fill in."""
    if CONFIG_PATH.exists():
        return CONFIG_PATH
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(SAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return CONFIG_PATH


# ---------------------------------------------------------------- account data --
def account_holdings() -> dict[str, list[dict]]:
    """Holdings grouped by the account label they were extracted under.

    profile.json collapses holdings by symbol and drops the account, so the
    per-account view has to come back from the extracted documents. Deduped the
    same way autoprofile does — largest value per (account, symbol) — so a
    portfolio arriving as both CSV and XLSX is not counted twice.
    """
    best: dict[tuple, dict] = {}
    ex = common.FIN_DATA / "extracted"
    if not ex.exists():
        return {}
    for p in sorted(ex.glob("*.json")):
        doc = common.load_json(p) or {}
        for h in doc.get("holding_candidates") or []:
            acct = (h.get("account") or "").strip()
            sym = (h.get("symbol") or h.get("description") or "").strip()
            if not acct or not sym:
                continue
            key = (acct.lower(), sym.lower())
            if key not in best or (h.get("value") or 0) > (best[key].get("value") or 0):
                best[key] = h
    out: dict[str, list[dict]] = {}
    for h in best.values():
        out.setdefault(h["account"].strip(), []).append(h)
    return out


MASKED = re.compile(r"\*(\d{3,6})")


def harvested_charges() -> list[dict]:
    """Advisory-fee charges found in the extracted activity ledgers.

    extract.scan_fee_charges() writes these per document; summary.json holds
    the deduped set. Falling back to the per-document files keeps this working
    when a run wrote documents but no summary.
    """
    ex = common.FIN_DATA / "extracted"
    if not ex.exists():
        return []
    summary = common.load_json(ex / "summary.json") or {}
    if summary.get("fee_charges"):
        return list(summary["fee_charges"])
    seen: dict[tuple, dict] = {}
    for p in sorted(ex.glob("*.json")):
        for c in (common.load_json(p) or {}).get("fee_charges") or []:
            seen.setdefault((str(c.get("account", "")).strip().lower(),
                             c.get("date"), c.get("amount")), c)
    return sorted(seen.values(), key=lambda c: str(c.get("date")))


def _annualise_charges(charges: list[dict]) -> tuple[float, float, int] | None:
    """(annualised total, years covered, n) from dated fee charges.

    Two ways to get there, and the first is much the better one. When the
    charge states its own billing period — brokers write "PIM QUARTERLY FEE"
    right in the description — the annual figure is the average charge times
    the periods in a year, which is right even when the export window caught
    only some of them.

    Otherwise the spacing has to carry it, and the span is measured from one
    period *before* the first charge: four quarterly bills cover a year, but
    their first and last dates are only nine months apart. Without that
    correction a year of quarterly billing annualises a third too high.
    """
    dated, per_year = [], None
    for c in charges:
        try:
            d = date.fromisoformat(str(c["date"])[:10])
        except (ValueError, KeyError, TypeError):
            continue
        amt = abs(float(c.get("amount") or 0))
        if not amt:
            continue
        dated.append((d, amt))
        # a stated period on any charge applies to the billing arrangement
        if per_year is None and c.get("periods_per_year"):
            per_year = int(c["periods_per_year"])
    if not dated:
        return None
    dated.sort()
    if per_year:
        mean = sum(a for _, a in dated) / len(dated)
        return mean * per_year, len(dated) / per_year, len(dated)
    # extract repairs impossible dates at the source; anything still in the
    # future would poison a span-derived rate, so it does not vote
    dated = [(d, a) for d, a in dated if d <= date.today()]
    if len(dated) < 2:
        return None
    span_days = (dated[-1][0] - dated[0][0]).days
    if span_days <= 0:
        return None
    period = span_days / (len(dated) - 1)
    covered = (span_days + period) / 365.25
    total = sum(a for _, a in dated)
    return total / covered, covered, len(dated)


def harvested_income() -> list[dict]:
    """Dividend/interest rows from the extracted activity ledgers."""
    ex = common.FIN_DATA / "extracted"
    summary = common.load_json(ex / "summary.json") or {} if ex.exists() else {}
    return list(summary.get("income_activity") or [])


def _values_at(holdings: list[dict], history: dict, when: str,
               memo: dict[str, tuple[float | None, float]]
               ) -> tuple[float | None, float]:
    """_account_value_at, remembered per date.

    Consecutive billing periods share a boundary — one period's end is the
    next one's start — so every interior date was being valued twice, each
    time walking every holding and its price series.
    """
    if when not in memo:
        memo[when] = _account_value_at(holdings, history, when)
    return memo[when]


def _account_value_at(holdings: list[dict], history: dict, when: str) -> tuple[float | None, float]:
    """(value of these holdings on `when`, share of today's value priced).

    Share counts are today's, so this is what the *current* portfolio would
    have been worth then, not what was actually held — the ledger shows a
    handful of trades in the window. Coverage is returned rather than assumed:
    positions with no share count (cash sweeps, unpriceable lots) cannot be
    walked back, and a period priced on half the account is not a gain figure
    worth showing.
    """
    series = history.get("series") or {}
    valued = priced_now = 0.0
    total_now = sum(float(h.get("value") or 0) for h in holdings)
    for h in holdings:
        sym = (h.get("symbol") or "").strip().upper()
        qty = h.get("quantity")
        now = float(h.get("value") or 0)
        if not sym or not qty:
            continue
        px = price_on(series.get(sym) or series.get(sym.replace(".", "-")) or {}, when)
        if px is None:
            continue
        valued += float(qty) * px
        priced_now += now
    if not priced_now:
        return None, 0.0
    return round(valued, 2), (priced_now / total_now if total_now else 0.0)




def charge_periods(charges: list[dict], holdings: list[dict], history: dict,
                   income: list[dict] | None = None,
                   match: str = "") -> list[dict]:
    """One record per billing period: what the account made, what it was billed.

    This is the fee put next to the thing it is supposed to buy. Each period
    runs from the previous charge to this one (the first reaches back by its
    stated billing period), and inside it:

      * market gain/loss — today's share counts valued at each end, so the
        movement is price movement rather than money added or taken out;
      * income — dividends and interest the ledger recorded in the window;
      * the fee actually charged on the closing date.

    It is a comparison, not an attribution: a rising market pays the fee too,
    and none of this says whether the advisor caused the gain. What it does say
    is what the fee cost against what the account produced, quarter by quarter,
    including the quarters that produced nothing.
    """
    dated = sorted((c for c in charges if c.get("date") and c.get("amount")),
                   key=lambda c: str(c["date"])[:10])
    if not dated or not holdings:
        return []
    per_year = next((int(c["periods_per_year"]) for c in dated
                     if c.get("periods_per_year")), 4)
    span_days = round(365.25 / per_year)
    inc = [i for i in (income or [])
           if not match or match in str(i.get("account") or "").lower()]
    # An export can record income for only part of the window it covers — this
    # one has no dividend rows for the advised account before May. Periods
    # before the first row are not "zero income", they are unrecorded, and a
    # fee share computed against them would be wrong in the direction that
    # flatters nobody.
    first_income = min((str(i["date"])[:10] for i in inc), default=None)

    out = []
    valued: dict[str, tuple[float | None, float]] = {}
    for idx, c in enumerate(dated):
        end = str(c["date"])[:10]
        if idx:
            start = str(dated[idx - 1]["date"])[:10]
        else:
            start = (date.fromisoformat(end)
                     - timedelta(days=span_days)).isoformat()
        v0, cov0 = _values_at(holdings, history, start, valued)
        v1, cov1 = _values_at(holdings, history, end, valued)
        gain = round(v1 - v0, 2) if (v0 is not None and v1 is not None) else None
        income_complete = bool(first_income) and first_income <= start
        earned = round(sum(float(i.get("amount") or 0) for i in inc
                           if start < str(i.get("date"))[:10] <= end), 2)
        fee = round(abs(float(c["amount"])), 2)
        # income the export does not fully cover is shown but not added in: a
        # partial figure would understate the return and overstate the fee's
        # share of it
        total = (round(gain + (earned if income_complete else 0), 2)
                 if gain is not None else None)
        out.append({
            "label": _period_label(end),
            "start": start, "end": end,
            "start_value": v0, "end_value": v1,
            "coverage": round(min(cov0, cov1), 4) if (v0 and v1) else 0.0,
            "market_gain": gain, "income": earned,
            "income_complete": income_complete, "total_return": total,
            "fee": fee,
            # what fraction of everything the account produced went to the fee;
            # undefined when the account produced nothing to take a share of
            "fee_share": (fee / total) if (total and total > 0) else None,
            # One signed measure that works in both directions: the fee against
            # what the quarter did. A gaining quarter gave up a share of the
            # gain (positive); a losing one had the fee added on top of the
            # loss (negative), which is the case a "share of gain" cannot
            # express and where the fee does the most damage.
            "fee_impact": ((fee / total) if total and total > 0
                           else (-fee / abs(total)) if total and total < 0
                           else None),
            # and the fee as a drag on the balance itself — always negative,
            # because it always comes out
            "fee_vs_value": (-(fee / v1) if v1 else None),
            "net": (round(total - fee, 2) if total is not None else None),
            "loss": bool(total is not None and total < 0),
            "date_repaired_from": c.get("date_repaired_from"),
        })
    return out


def _symbol_return(history: dict, start: str, end: str,
                   symbol: str) -> float | None:
    series = (history.get("series") or {}).get(symbol) or {}
    p0, p1 = price_on(series, start), price_on(series, end)
    if not p0 or not p1:
        return None
    return (p1 / p0) - 1


def blend_label(spec) -> str:
    """"87% VTI / 13% BND" — the mix stated, never hidden behind a name."""
    if isinstance(spec, str):
        return spec
    parts = [(s, w) for s, w in _blend_pairs(spec) if round(w * 100) >= 1]
    return " / ".join(f"{w * 100:.0f}% {s}" for s, w in parts)


def _blend_pairs(spec) -> list[tuple[str, float]]:
    """Normalise a blend spec to [(symbol, weight), ...] summing to 1."""
    if isinstance(spec, str):
        return [(spec, 1.0)]
    pairs = list(spec.items()) if isinstance(spec, dict) else [
        (p[0], p[1]) for p in spec]
    pairs = [(str(s).upper(), float(w)) for s, w in pairs if float(w) > 0]
    tot = sum(w for _, w in pairs)
    if not tot:
        return []
    return [(s, w / tot) for s, w in pairs]


def benchmark_return(history: dict, start: str, end: str,
                     symbol=DEFAULT_BENCHMARK) -> tuple[float | None, str]:
    """Total return of a passive yardstick over the same dates.

    A managed account's return means little on its own — most of it is usually
    the market. The comparison is what turns a number into a judgment, so the
    benchmark runs over exactly the same window, from the same weekly closes,
    with dividends included the same way (the series is adjusted).

    `symbol` is either one ticker or a blend — a dict or [[sym, weight], ...].
    A blend is held, not rebalanced: over one window that is the weighted sum
    of the components' total returns, which is what someone who bought the mix
    at the start and left it alone would actually have earned. Every component
    has to be priced; a blend missing a leg is reported as unavailable rather
    than quietly renormalised onto the legs that happen to exist, which would
    silently change the comparison being made.
    """
    pairs = _blend_pairs(symbol)
    if not pairs:
        return None, blend_label(symbol)
    total = 0.0
    for sym, w in pairs:
        r = _symbol_return(history, start, end, sym)
        if r is None:
            return None, blend_label(symbol)
        total += w * r
    return total, blend_label(symbol)


def asset_mix(holdings: list[dict], funds: dict[str, dict]) -> dict | None:
    """Fraction of an account sitting in each asset class, funds looked through.

    A direct equity holding is equity. A fund is split by the asset-class
    breakdown its data carries; one with no breakdown is counted as
    unclassified and then spread across whatever *is* classified, so an
    unknown fund tilts nothing on its own.
    """
    mix: dict[str, float] = {}
    total = unclassified = 0.0
    keymap = {"stockPosition": "stocks", "bondPosition": "bonds",
              "cashPosition": "cash", "preferredPosition": "preferred",
              "convertiblePosition": "convertible", "otherPosition": "other"}
    exus = 0.0
    for h in holdings:
        v = float(h.get("value") or 0)
        if v <= 0:
            continue
        total += v
        sym = str(h.get("symbol") or "").strip().upper()
        f = funds.get(sym)
        classes = (f or {}).get("asset_classes") or {}
        if not classes:
            if f is not None:
                unclassified += v
            else:
                # not in the fund table at all: a directly held company
                mix["stocks"] = mix.get("stocks", 0.0) + v
            continue
        cat = str(f.get("category") or "").casefold()
        equity_w = float(classes.get("stockPosition") or 0)
        if equity_w and any(hint in cat for hint in _EXUS_HINTS):
            exus += v * equity_w
        for k, w in classes.items():
            mix[keymap.get(k, "other")] = mix.get(keymap.get(k, "other"), 0.0) + v * float(w)
    if not total:
        return None
    classified = total - unclassified
    if classified <= 0:
        return None
    # unclassified value rides along with the classified mix rather than
    # becoming a category of its own in the benchmark
    out = {k: v / classified for k, v in mix.items()}
    out["ex_us_equity"] = exus / classified
    out["unclassified_share"] = unclassified / total
    return out


def derive_blend(mix: dict | None, symbols: dict | None = None) -> list | None:
    """A risk-matched benchmark with the same shape as the account.

    Equity against equity, bonds against bonds, cash against cash — so the
    comparison stops rewarding whoever simply held more stock. Preferreds and
    convertibles sit with bonds: they are credit instruments that trade on
    rates, and pretending they are equity would overstate the equity the
    account actually carries.
    """
    if not mix:
        return None
    syms = {**DEFAULT_BLEND_SYMBOLS, **(symbols or {})}
    equity = max(mix.get("stocks", 0.0), 0.0)
    exus = min(max(mix.get("ex_us_equity", 0.0), 0.0), equity)
    bonds = (max(mix.get("bonds", 0.0), 0.0) + max(mix.get("preferred", 0.0), 0.0)
             + max(mix.get("convertible", 0.0), 0.0))
    cash = max(mix.get("cash", 0.0), 0.0)
    parts = [(syms["us_equity"], equity - exus), (syms["intl_equity"], exus),
             (syms["bonds"], bonds), (syms["cash"], cash)]
    parts = [(s, w) for s, w in parts if w > 0.0005]
    if not parts:
        return None
    tot = sum(w for _, w in parts)
    return [[s, round(w / tot, 4)] for s, w in parts]


def benchmark_symbols(config: dict | None = None) -> list[str]:
    """Every ticker the fee comparison needs priced, so the fetch can add them.

    The headline benchmark is usually held anyway; the blend legs are not, and
    a benchmark that is missing from the price history silently disables the
    comparison it exists to make.
    """
    cfg = config or {}
    out = list(_blend_pairs(cfg.get("benchmark") or DEFAULT_BENCHMARK))
    spec = cfg.get("benchmark_blend", "auto")
    if isinstance(spec, (list, dict)):
        out += _blend_pairs(spec)
    syms = {**DEFAULT_BLEND_SYMBOLS, **(cfg.get("benchmark_symbols") or {})}
    seen, res = set(), []
    for s in [s for s, _ in out] + (list(syms.values()) if spec else []):
        s = str(s).upper()
        if s and s not in seen:
            seen.add(s)
            res.append(s)
    return res


def _risk_matched(out: dict, history: dict, start: str, end: str,
                  blend, mix: dict | None) -> None:
    """Attach the risk-matched comparison to a performance record, in place.

    Kept separate from the headline benchmark because it answers a different
    question, and a reader who conflates the two will draw the wrong
    conclusion from whichever one happens to look better.
    """
    if not blend:
        return
    r, label = benchmark_return(history or {}, start, end, blend)
    out["blend"] = blend
    out["blend_label"] = label
    out["blend_return"] = r
    out["blend_mix"] = mix
    if r is None:
        # named so the page can say which leg is missing rather than "—"
        out["blend_missing"] = [s for s, _ in _blend_pairs(blend)
                                if _symbol_return(history or {}, start, end, s) is None]
        return
    net, gross = out["net_return"], out["gross_return"]
    out["excess_net_blend"] = net - r
    out["excess_gross_blend"] = gross - r
    out["blend_dollars"] = round((net - r) * out["start_value"], 2)


def account_performance(periods: list[dict], history: dict | None = None,
                        benchmark=DEFAULT_BENCHMARK, blend=None,
                        mix: dict | None = None) -> dict | None:
    """What the advised account earned over its billing history, and after fees.

    Money-weighted returns need every cash flow, which a positions export does
    not carry. What this measures instead is the value of today's holdings at
    the start of the window against their value at the end, plus the income the
    ledger recorded — a time-weighted approximation with the same standing
    caveat as the periods it is built from: share counts are today's, so a
    position bought mid-window is treated as if it were held throughout.

    Reported gross and net of the advisory fee, because the fee is the part
    that is certain and the only part the arrangement itself controls.
    """
    usable = [p for p in periods if p.get("total_return") is not None
              and p.get("start_value") and p.get("end_value")]
    if len(usable) < 2:
        return None
    start, end = usable[0]["start"], usable[-1]["end"]
    v0, v1 = usable[0]["start_value"], usable[-1]["end_value"]
    if not v0:
        return None
    gain = round(sum(p["market_gain"] or 0 for p in usable), 2)
    income = round(sum(p["income"] or 0 for p in usable
                       if p.get("income_complete")), 2)
    fee = round(sum(p["fee"] for p in usable), 2)
    total = round(gain + income, 2)
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days or 1
    years = days / 365.25

    gross = total / v0
    net = (total - fee) / v0
    bench, bench_sym = benchmark_return(history or {}, start, end, benchmark)

    def annualise(r):
        return ((1 + r) ** (1 / years) - 1) if years and r > -1 else None

    out = {
        "start": start, "end": end, "days": days, "years": round(years, 2),
        "start_value": v0, "end_value": v1,
        "market_gain": gain, "income": income, "fee": fee,
        "total_return_dollars": total,
        "net_dollars": round(total - fee, 2),
        "gross_return": gross, "net_return": net,
        "fee_drag": gross - net,
        "gross_annualised": annualise(gross), "net_annualised": annualise(net),
        "benchmark": bench_sym, "benchmark_return": bench,
        # what the advice earned or cost against simply owning the market —
        # after the fee, because that is the choice actually on offer
        "excess_gross": (gross - bench) if bench is not None else None,
        "excess_net": (net - bench) if bench is not None else None,
        "periods": len(usable),
        "income_complete": all(p.get("income_complete") for p in usable),
        "coverage": min((p.get("coverage") or 0) for p in usable),
    }
    _risk_matched(out, history or {}, start, end, blend, mix)
    out["verdict"] = fee_verdict(out)
    return out


def fee_verdict(perf: dict | None,
                tracking_error: float = DEFAULT_TRACKING_ERROR) -> dict | None:
    """Did the fee pay off? Yes or no, on the record so far.

    The question has a plain arithmetic answer: after the fee was taken, did
    the advised money end up ahead of the same money in a plain index fund over
    the same dates. That is what "paying off" means to the person paying, and
    it is the number this answers.

    What it deliberately does not do is dress that answer up as proof. A short
    record cannot separate skill from luck — the same module computes how many
    years it would take — so the verdict reports what happened and says how
    much weight it carries. Both halves matter: a "no" is still a no even when
    it is not statistically conclusive, because the fee was certain and the
    shortfall was real money.
    """
    if not perf:
        return None
    excess = perf.get("excess_net")
    if excess is None:
        return {"answer": "unknown",
                "because": "no benchmark prices covering these dates, so there "
                           "is nothing to compare the return against"}

    v0, years = perf["start_value"], perf.get("years") or 0
    # what the answer is worth in money: the gap applied to the money that
    # earned it, which is the amount better or worse off than indexing
    dollars = round(excess * v0, 2)
    ahead_gross = perf.get("excess_gross")
    # the fee is what separates the two comparisons: picks ahead of the index
    # before the fee, behind after it, means the fee is the whole story
    fee_flipped = (ahead_gross is not None and ahead_gross > 0 >= excess)
    # How long this record would have to run before the result could be called
    # skill rather than noise. The test is on *annual* excess return, so the
    # annualised figures are used where they exist; excess/years is only a
    # fallback, and it understates a compounded gap over a long window.
    alpha = None
    if perf.get("net_annualised") is not None and perf.get("benchmark_return") is not None:
        bench_ann = ((1 + perf["benchmark_return"]) ** (1 / years) - 1) if years else None
        if bench_ann is not None:
            alpha = perf["net_annualised"] - bench_ann
    if alpha is None and years:
        alpha = excess / years
    needed = years_to_detect(abs(alpha), tracking_error) if alpha else None
    return {
        "answer": "yes" if excess > 0 else "no",
        "excess_net": excess,
        "dollars": dollars,
        "years": years,
        "fee_flipped": fee_flipped,
        "excess_gross": ahead_gross,
        # a verdict this short a record cannot prove either way — said plainly
        # rather than implied by a p-value nobody reads
        "conclusive": bool(needed is not None and years >= needed),
        "years_needed": needed,
        "alpha_annual": alpha,
    }


def advised_performance(accounts: list[dict], history: dict | None = None,
                        benchmark=DEFAULT_BENCHMARK, blend=None,
                        mix: dict | None = None) -> dict | None:
    """The advised money as one account: what all of it earned, after fees.

    Per-account returns answer "how is this account doing"; this answers the
    question actually being asked of an advisor — what the whole managed
    relationship returned, and what it returned after the fee.

    Combined on dollars, not by averaging percentages: a return on a large
    account and a return on a small one do not carry equal weight, and adding
    the gains and dividing by the money that produced them is the only
    aggregation that stays true when the accounts differ in size.
    """
    usable = [(a, a["performance"]) for a in accounts if a.get("performance")]
    if not usable:
        return None

    v0 = round(sum(p["start_value"] for _, p in usable), 2)
    if not v0:
        return None
    gain = round(sum(p["market_gain"] for _, p in usable), 2)
    income = round(sum(p["income"] for _, p in usable), 2)
    fee = round(sum(p["fee"] for _, p in usable), 2)
    total = round(gain + income, 2)
    start = min(p["start"] for _, p in usable)
    end = max(p["end"] for _, p in usable)
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days or 1
    years = days / 365.25

    gross, net = total / v0, (total - fee) / v0
    bench, bench_sym = benchmark_return(history or {}, start, end, benchmark)

    def annualise(r):
        return ((1 + r) ** (1 / years) - 1) if years and r > -1 else None

    out = {
        "accounts": [a["label"] for a, _ in usable],
        "n_accounts": len(usable),
        # accounts billed on different cycles cover different windows, so the
        # combined figure spans the union of them. Said plainly rather than
        # presented as one clean period.
        "aligned": len({(p["start"], p["end"]) for _, p in usable}) == 1,
        "start": start, "end": end, "days": days, "years": round(years, 2),
        "start_value": v0,
        "end_value": round(sum(p["end_value"] for _, p in usable), 2),
        "market_gain": gain, "income": income, "fee": fee,
        "total_return_dollars": total, "net_dollars": round(total - fee, 2),
        "gross_return": gross, "net_return": net, "fee_drag": gross - net,
        "gross_annualised": annualise(gross), "net_annualised": annualise(net),
        "benchmark": bench_sym, "benchmark_return": bench,
        "excess_gross": (gross - bench) if bench is not None else None,
        "excess_net": (net - bench) if bench is not None else None,
        # the fee as a share of what the money actually made — the number the
        # arrangement is judged on when the market did the earning
        "fee_share_of_return": (fee / total) if total > 0 else None,
        "income_complete": all(p["income_complete"] for _, p in usable),
        "coverage": min(p["coverage"] for _, p in usable),
    }
    _risk_matched(out, history or {}, start, end, blend, mix)
    out["verdict"] = fee_verdict(out)
    return out


def _period_label(end: str) -> str:
    d = date.fromisoformat(end)
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


def _fund_expense(holdings: list[dict], fund_er: dict[str, float]) -> tuple[float | None, float]:
    """Value-weighted expense ratio of the funds inside one account."""
    num = den = 0.0
    for h in holdings:
        sym = (h.get("symbol") or "").strip().upper()
        er = fund_er.get(sym)
        if er is None:
            continue
        v = float(h.get("value") or 0)
        num += v * er
        den += v
    return ((num / den) if den else None), den


# ------------------------------------------------------------------ statistics --
def years_to_detect(alpha: float, tracking_error: float) -> float | None:
    """Years of record needed to call `alpha` significant at 95% with 80% power.

    One-sample test on annual excess returns: n = ((z_a + z_b) * sigma / alpha)^2.
    """
    if not alpha or alpha <= 0 or tracking_error <= 0:
        return None
    return ((Z_95 + Z_80) * tracking_error / alpha) ** 2


def prob_beats_passive(hurdle: float, tracking_error: float, years: float) -> float | None:
    """P(the advised account ends ahead of the passive alternative).

    Under the null that the advisor adds no gross alpha, mean excess return
    over `years` is ~N(0, sigma/sqrt(years)), and they only come out ahead by
    clearing `hurdle` — the fee gap — on average. Not a prediction that the
    advisor is worthless; it is what the odds look like if they are average.
    """
    if tracking_error <= 0 or years <= 0:
        return None
    return 1 - NormalDist().cdf(hurdle * (years ** 0.5) / tracking_error)


def excess_return_test(excess: list[float]) -> dict | None:
    """One-sample t-test on a realised annual excess-return series.

    Only meaningful when the owner supplies actual period returns; with the
    handful of periods a statement history usually gives, the honest result is
    a wide interval that excludes nothing, and this reports it as such.
    """
    n = len(excess)
    if n < 2:
        return None
    mean = sum(excess) / n
    var = sum((x - mean) ** 2 for x in excess) / (n - 1)
    sd = var ** 0.5
    se = sd / (n ** 0.5)
    if se == 0:
        return None
    t = mean / se
    # Normal approximation to the t distribution — with the small n available
    # here the interval is wide enough that the difference is immaterial, and
    # it keeps this dependency-free.
    p = 2 * (1 - NormalDist().cdf(abs(t)))
    return {"n": n, "mean": mean, "sd": sd, "se": se, "t": t, "p": p,
            "ci_lo": mean - Z_95 * se, "ci_hi": mean + Z_95 * se,
            "significant": p < 0.05}


def compounded_drag(value: float, fee_rate: float, years: float,
                    growth: float = DEFAULT_GROWTH) -> dict:
    """Terminal-value cost of paying `fee_rate` on `value` for `years`.

    Measured on the balance as it stands, without future contributions, so it
    understates the real figure for anyone still saving.
    """
    gross = value * (1 + growth) ** years
    net = value * (1 + growth - fee_rate) ** years
    return {"years": years, "growth": growth,
            "gross": round(gross, 2), "net": round(net, 2),
            "drag": round(gross - net, 2),
            "drag_pct": (gross - net) / gross if gross else None}


# -------------------------------------------------------------------- evaluate --
def _clean_label(label: str) -> str:
    """Account labels come through the redactor, so they carry its placeholders
    ("[NAME] 401k R/O IRA(*1234)"). Strip those for display — the account is
    identified by its masked number, not by whose name was on it."""
    return re.sub(r"\s{2,}", " ", re.sub(r"\[[A-Z_]+\]", "", label or "")).strip()


def _account_key(label: str) -> str:
    """What to match an account by: its masked number when it has one.

    "Alex 401k R/O IRA(*1234)" and a positions export's bare "*1234" are the
    same account, and the number is the part both files agree on.
    """
    m = MASKED.search(label or "")
    return ("*" + m.group(1)) if m else (label or "").strip()


def discover_accounts(charges: list[dict], declared: list[dict]) -> list[dict]:
    """Advised accounts evidenced by their own fee charges.

    A charge is not an inference: the ledger row says this account was billed
    an advisory fee. Accounts already declared in the config are left to the
    config so its label, institution and schedule survive.
    """
    known = [str(a.get("match") or "").strip().lower() for a in declared]
    out: dict[str, dict] = {}
    for c in charges:
        label = str(c.get("account") or "").strip()
        key = _account_key(label)
        if not key or any(k and k in label.lower() for k in known):
            continue
        out.setdefault(key, {"match": key, "label": _clean_label(label) or key,
                             "institution": None, "annual_rate": None,
                             "billed": c.get("billed"), "discovered": True})
    return list(out.values())


def evaluate(portfolio: dict, config: dict | None = None,
             holdings_by_account: dict[str, list[dict]] | None = None,
             years_to_retirement: float = 20, years_horizon: float = 36,
             charges: list[dict] | None = None,
             history: dict | None = None,
             income: list[dict] | None = None) -> dict:
    """Full advisory-fee evaluation.

    Runs on any account that either was billed an advisory fee in an extracted
    activity ledger or is declared in the config. Returns
    {"configured": False, ...} only when there is neither — the analysis is
    never guessed from account numbers alone.
    """
    cfg = config if config is not None else load_config()
    harvested = charges if charges is not None else harvested_charges()
    hist = history if history is not None else (
        common.load_json(common.FIN_DATA / "price_history.json") or {})
    earned = income if income is not None else harvested_income()
    declared_cfg = [a for a in ((cfg or {}).get("accounts") or []) if a.get("match")]
    # a declared account needs a rate to be evaluated on, unless its charges
    # supply one
    accounts_cfg = [a for a in declared_cfg
                    if a.get("annual_rate") is not None
                    or any(str(a["match"]).strip().lower()
                           in str(c.get("account") or "").lower() for c in harvested)]
    accounts_cfg += discover_accounts(harvested, declared_cfg)
    if not accounts_cfg:
        return {"configured": False, "config_path": str(CONFIG_PATH),
                "sample_path": str(SAMPLE_PATH)}

    passive_er = float((cfg or {}).get("passive_alternative_er", DEFAULT_PASSIVE_ER))
    te = float((cfg or {}).get("tracking_error", DEFAULT_TRACKING_ERROR))
    growth = float((cfg or {}).get("growth", DEFAULT_GROWTH))

    by_account = (holdings_by_account if holdings_by_account is not None
                  else account_holdings())
    fa = portfolio.get("fund_analysis") or {}
    fund_er = {f["symbol"].upper(): f["expense_ratio"] for f in (fa.get("funds") or [])
               if f.get("expense_ratio") is not None}
    funds_by_sym = {f["symbol"].upper(): f for f in (fa.get("funds") or [])}
    benchmark = (cfg or {}).get("benchmark") or DEFAULT_BENCHMARK
    # "auto" derives the mix from what each account holds; a list or dict pins
    # it to a benchmark of the owner's choosing; false turns it off entirely
    blend_cfg = (cfg or {}).get("benchmark_blend", "auto")
    blend_syms = (cfg or {}).get("benchmark_symbols")
    portfolio_total = portfolio.get("total_portfolio") or 0

    charges_by_acct: dict[str, list[dict]] = {}
    for c in (cfg or {}).get("charges") or []:
        charges_by_acct.setdefault(str(c.get("account") or "").strip().lower(),
                                   []).append(c)
    returns_by_acct: dict[str, list[dict]] = {}
    for r in (cfg or {}).get("returns") or []:
        returns_by_acct.setdefault(str(r.get("account") or "").strip().lower(),
                                   []).append(r)

    rows, gaps, advised_holdings = [], [], []
    for a in accounts_cfg:
        match = str(a["match"]).strip().lower()
        matched = [label for label in by_account if match in label.lower()]
        holdings = [h for label in matched for h in by_account[label]]
        value = round(sum(float(h.get("value") or 0) for h in holdings), 2)
        cost_rows = [h for h in holdings if h.get("cost_basis") is not None]
        cost = round(sum(float(h["cost_basis"]) for h in cost_rows), 2) or None
        cost_value = round(sum(float(h.get("cost_value") or h.get("value") or 0)
                               for h in cost_rows), 2)

        # An account named in the config that matched nothing is a real finding
        # — the fee is being paid against holdings this analysis cannot see.
        if not matched:
            gaps.append(f'no extracted account matches "{a["match"]}" — '
                        f'fee measured on the declared value only')
        if not value and a.get("value"):
            value = float(a["value"])

        declared = (float(a["annual_rate"]) if a.get("annual_rate") is not None
                    else None)
        # config charges are keyed by the match string; harvested ones carry
        # the full account label the ledger used, so match them the same way
        # holdings are matched
        acct_charges = list(charges_by_acct.get(match, []))
        acct_charges += [c for c in harvested
                         if match in str(c.get("account") or "").lower()]
        ann = _annualise_charges(acct_charges)
        measured_rate = (ann[0] / value) if (ann and value) else None
        rate = measured_rate if measured_rate is not None else declared
        if rate is None:
            gaps.append(f'{a.get("label") or a["match"]}: charges found but no '
                        f'account value to measure a rate against — add '
                        f'annual_rate or value to {CONFIG_PATH.name}')
            continue
        fee_annual = (round(ann[0], 2) if (ann and not value)
                      else round(value * rate, 2) if value else None)
        billed = a.get("billed") or next(
            (c.get("billed") for c in acct_charges if c.get("billed")), None)
        # The trailing year is what was paid; the most recent bill times its
        # period is what the arrangement costs *now*. They differ when the
        # relationship started mid-window or the balance moved, and the
        # forward-looking one is what the projection should assume.
        run_rate = None
        per_year = next((int(c["periods_per_year"]) for c in acct_charges
                         if c.get("periods_per_year")), None)
        # "most recent" has to ignore the charge whose date we just said not to
        # trust, or a future-dated row wins the comparison every time
        usable = [c for c in acct_charges
                  if str(c.get("date", ""))[:10] <= date.today().isoformat()]
        if per_year and ann and ann[2] >= 2 and usable:
            latest = max(usable, key=lambda c: str(c.get("date")))
            run_rate = round(abs(float(latest.get("amount") or 0)) * per_year, 2)
            if ann[0] and abs(run_rate - ann[0]) / ann[0] > 0.10:
                gaps.append(f'{a.get("label") or a["match"]}: last bill annualises '
                            f'to {run_rate:,.0f} against {ann[0]:,.0f} paid over the '
                            f'window — the charges are not level, so the rate here '
                            f'is the trailing one')
        repaired = [c for c in acct_charges if c.get("date_repaired_from")]
        for c in repaired:
            gaps.append(f'{a.get("label") or a["match"]}: a charge dated '
                        f'{c["date_repaired_from"]} in the source export cannot be '
                        f'in the future — read as {c["date"]}')
        if ann and ann[2] == 1:
            # one bill scaled up by its stated period. Right if the arrangement
            # ran all year, an overstatement if it started mid-window — worth
            # saying out loud rather than burying in a rate.
            gaps.append(f'{a.get("label") or a["match"]}: annual fee '
                        f'extrapolated from a single {billed or "periodic"} '
                        f'charge — confirm against a full year of statements')

        acct_er, er_covered = _fund_expense(holdings, fund_er)
        if acct_er is None:
            acct_er = fa.get("weighted_expense")
            if acct_er is not None:
                gaps.append(f'{a.get("label") or a["match"]}: no per-fund expense '
                            f'data in this account — using the portfolio-wide '
                            f'weighted expense ratio')
        all_in = rate + (acct_er or 0.0)
        # Two different questions, two different hurdles: what the advice alone
        # costs versus self-managing these same funds, and what the whole stack
        # must beat versus a passive portfolio of the same market exposure.
        advice_only = rate
        breakeven = all_in - passive_er

        detect = years_to_detect(breakeven, te)
        odds = {int(y): prob_beats_passive(breakeven, te, y)
                for y in (1, 5, 10, round(years_to_retirement) or 10)}

        advised_holdings += holdings
        mix = asset_mix(holdings, funds_by_sym)
        blend = (derive_blend(mix, blend_syms) if blend_cfg == "auto"
                 else (blend_cfg if isinstance(blend_cfg, (list, dict)) else None))

        obs = None
        series = returns_by_acct.get(match) or []
        excess = [float(r["account_return"]) - float(r["benchmark_return"])
                  for r in series
                  if r.get("account_return") is not None
                  and r.get("benchmark_return") is not None]
        if excess:
            obs = excess_return_test(excess)
            if obs:
                obs["net_of_fee_mean"] = obs["mean"] - rate if not (
                    cfg or {}).get("returns_are_net_of_fee") else obs["mean"]

        # Gain is measured only over the lots that reported a cost, against the
        # market value of those same lots — mixing a partial basis into a whole
        # account's value would invent a gain.
        gain = (round(sum(float(h.get("value") or 0) for h in cost_rows) - cost, 2)
                if cost else None)
        fee_share_of_gain = (fee_annual / gain) if (fee_annual and gain and gain > 0) else None

        rows.append({
            "label": a.get("label") or a.get("institution") or a["match"],
            "institution": a.get("institution"),
            "match": a["match"], "matched_accounts": len(matched),
            "billed": billed, "discovered": bool(a.get("discovered")),
            "value": value, "pct_portfolio": (value / portfolio_total)
            if portfolio_total else None,
            "cost_basis": cost, "gain": gain,
            "declared_rate": declared,
            "measured_rate": measured_rate,
            "rate": rate,
            "rate_source": "charges" if measured_rate is not None else "declared",
            "charges_n": ann[2] if ann else 0,
            "charges_years": round(ann[1], 2) if ann else None,
            "charges_total": round(sum(abs(float(c.get("amount") or 0))
                                       for c in acct_charges), 2) if acct_charges else None,
            "charges_annualised": round(ann[0], 2) if ann else None,
            "run_rate_annual": run_rate,
            "run_rate_rate": (run_rate / value) if (run_rate and value) else None,
            "periods": (per := charge_periods(acct_charges, holdings, hist,
                                              earned, match)),
            "performance": account_performance(per, hist, benchmark, blend, mix),
            "asset_mix": mix, "benchmark_blend": blend,
            "charges_from": sorted({c.get("source") or "advisory_fees.json"
                                    for c in acct_charges}) or None,
            # a date we refused to trust is shown flagged rather than quietly
            "charges_dates": sorted(str(c.get("date"))[:10]
                                    + ("*" if c.get("date_repaired_from") else "")
                                    for c in acct_charges) or None,
            # a schedule and the actual bills disagreeing is worth surfacing
            "rate_gap": (measured_rate - declared)
            if (measured_rate is not None and declared is not None) else None,
            "fee_annual": fee_annual,
            "fund_er": acct_er, "fund_er_coverage": (er_covered / value)
            if value and er_covered else None,
            "all_in": all_in, "advice_only": advice_only,
            "passive_er": passive_er, "breakeven_alpha": breakeven,
            "fee_share_of_gain": fee_share_of_gain,
            "years_to_detect": detect,
            "odds_beat_passive": odds,
            "observed": obs,
            "drag_retirement": compounded_drag(value, rate, years_to_retirement, growth)
            if value else None,
            "drag_horizon": compounded_drag(value, rate, years_horizon, growth)
            if value else None,
        })

    rows.sort(key=lambda r: -(r["fee_annual"] or 0))
    advised_value = round(sum(r["value"] for r in rows), 2)
    fee_total = round(sum(r["fee_annual"] or 0 for r in rows), 2)
    blended = (fee_total / advised_value) if advised_value else None
    weighted_breakeven = (sum(r["breakeven_alpha"] * r["value"] for r in rows)
                          / advised_value) if advised_value else None

    # The combined stack, priced the same way one account is. Weighting the
    # fund expense by value — and only over the value that reports one — keeps
    # this a blended cost rather than an average of averages.
    # the risk-matched yardstick for the advised money as a whole, derived from
    # everything it holds rather than averaged from the per-account blends
    combined_mix = asset_mix(advised_holdings, funds_by_sym)
    combined_blend = (derive_blend(combined_mix, blend_syms) if blend_cfg == "auto"
                      else (blend_cfg if isinstance(blend_cfg, (list, dict)) else None))
    combined_perf = advised_performance(rows, hist, benchmark, combined_blend,
                                        combined_mix)
    if combined_perf and combined_perf.get("blend_missing"):
        gaps.append("no price history for "
                    + ", ".join(combined_perf["blend_missing"])
                    + " — the risk-matched comparison needs it; run "
                      "python finance/prices.py --history")

    er_value = sum(r["value"] for r in rows if r["fund_er"] is not None)
    weighted_fund_er = (sum(r["fund_er"] * r["value"] for r in rows
                            if r["fund_er"] is not None) / er_value) if er_value else None
    charges_total = sum(r["charges_total"] or 0 for r in rows) or None
    run_rate_total = sum(r["run_rate_annual"] or 0 for r in rows) or None
    gain_total = sum(r["gain"] for r in rows if r.get("gain") is not None) or None

    return {
        "configured": True,
        "accounts": rows,
        # what the advised money as a whole returned, gross and after the fee
        "performance": combined_perf,
        "benchmark": blend_label(benchmark),
        "benchmark_blend": combined_blend,
        "asset_mix": combined_mix,
        "charges_harvested": len(harvested),
        "discovered_accounts": sum(1 for r in rows if r["discovered"]),
        "advised_value": advised_value,
        "pct_of_portfolio": (advised_value / portfolio_total) if portfolio_total else None,
        "fee_total_annual": fee_total,
        "blended_rate": blended,
        "weighted_breakeven": weighted_breakeven,
        "years_to_detect": years_to_detect(weighted_breakeven or 0, te),
        "weighted_fund_er": weighted_fund_er,
        "fund_er_coverage": (er_value / advised_value) if advised_value else None,
        "all_in_blended": (blended or 0) + (weighted_fund_er or 0),
        "odds_beat_passive": {int(y): prob_beats_passive(weighted_breakeven or 0, te, y)
                              for y in (1, 5, 10, round(years_to_retirement) or 10)},
        "charges_n": sum(r["charges_n"] or 0 for r in rows),
        "charges_total": charges_total,
        "run_rate_annual": run_rate_total,
        "run_rate_rate": (run_rate_total / advised_value)
        if (run_rate_total and advised_value) else None,
        "gain": gain_total,
        "fee_share_of_gain": (fee_total / gain_total)
        if (fee_total and gain_total and gain_total > 0) else None,
        "accounts_n": len(rows),
        "accounts_billed_n": sum(1 for r in rows if r["rate_source"] == "charges"),
        "tracking_error": te,
        "passive_er": passive_er,
        "growth": growth,
        "years_to_retirement": years_to_retirement,
        "years_horizon": years_horizon,
        "drag_retirement": compounded_drag(advised_value, blended or 0,
                                           years_to_retirement, growth)
        if advised_value else None,
        "drag_horizon": compounded_drag(advised_value, blended or 0,
                                        years_horizon, growth)
        if advised_value else None,
        "data_gaps": gaps,
        "generated": common.now_iso(),
    }


if __name__ == "__main__":
    if "--install" in sys.argv:
        p = install_sample()
        common.diag(f"[fees] config at {p} — move an entry from _example_accounts "
                    f"into accounts to switch the analysis on")
        raise SystemExit(0)

    # ---- annualising periodic charges ---------------------------------------
    q = [{"date": "2025-01-02", "amount": 1000},
         {"date": "2025-04-01", "amount": 1000},
         {"date": "2025-07-01", "amount": 1000},
         {"date": "2025-10-01", "amount": 1000}]
    ann, yrs, n = _annualise_charges(q)
    # four quarterly bills = one year of fees, not the 0.75yr the dates span
    assert n == 4 and abs(yrs - 1.0) < 0.02, (yrs, n)
    assert abs(ann - 4000) < 100, ann
    # a single charge says nothing on its own...
    assert _annualise_charges(q[:1]) is None
    # ...but a charge that states its own billing period does: brokers write
    # "PIM QUARTERLY FEE" on the row, so one bill is a quarter of the year
    one = [{"date": "2026-07-10", "amount": -5000, "periods_per_year": 4,
            "billed": "quarterly"}]
    ann1, yrs1, n1 = _annualise_charges(one)
    assert (ann1, n1) == (20000, 1) and abs(yrs1 - 0.25) < 1e-9, (ann1, yrs1)
    # a stated period beats the spacing when the export caught only some bills
    gappy = [{"date": "2025-10-01", "amount": 1000, "periods_per_year": 4},
             {"date": "2026-07-01", "amount": 1000}]
    assert _annualise_charges(gappy)[0] == 4000

    # ---- the fee against what the account produced --------------------------
    hold = [{"symbol": "AAA", "quantity": 100.0, "value": 12000.0},
            {"symbol": "CASH", "quantity": None, "value": 0.0}]
    hx = {"series": {"AAA": {"2025-10-01": 100.0, "2026-01-01": 110.0,
                             "2026-03-30": 105.0, "2026-04-08": 95.0}}}
    bills = [{"date": "2026-01-09", "amount": 250.0, "periods_per_year": 4},
             {"date": "2026-04-10", "amount": 260.0, "periods_per_year": 4}]
    divs = [{"date": "2025-10-09", "amount": 5.0, "account": "IRA(*1)"},
            {"date": "2025-12-15", "amount": 40.0, "account": "IRA(*1)"},
            {"date": "2026-02-15", "amount": 60.0, "account": "IRA(*1)"},
            {"date": "2026-06-15", "amount": 99.0, "account": "IRA(*1)"}]
    per = charge_periods(bills, hold, hx, divs)
    assert [p["label"] for p in per] == ["Q1 2026", "Q2 2026"], per
    # first period reaches back one billing period; prices are the last close
    # on or before each end, so 100 -> 110 on 100 shares is +1,000
    assert per[0]["start"] == "2025-10-10" and per[0]["market_gain"] == 1000.0
    assert per[0]["income"] == 40.0 and per[0]["total_return"] == 1040.0
    assert per[0]["net"] == 790.0 and abs(per[0]["fee_share"] - 250 / 1040) < 1e-9
    # a down quarter is still billed: gain negative, fee positive, no share
    assert per[1]["market_gain"] == -1500.0, per[1]
    assert per[1]["income"] == 60.0 and per[1]["total_return"] == -1440.0
    assert per[1]["fee_share"] is None and per[1]["net"] == -1700.0
    assert per[1]["loss"] is True and per[0]["loss"] is False
    # the signed measure works in both directions: a share of the gain when
    # there was one, the fee added to the loss when there wasn't
    assert abs(per[0]["fee_impact"] - 250 / 1040) < 1e-9
    assert abs(per[1]["fee_impact"] + 260 / 1440) < 1e-9
    # against the balance the fee is always a drag, so always negative
    assert per[1]["fee_vs_value"] < 0
    assert abs(per[1]["fee_vs_value"] + 260 / 9500) < 1e-9
    # income outside the billed window belongs to no period
    assert sum(p["income"] for p in per) == 100.0
    # a ledger whose income starts mid-window reports "unknown", not zero, for
    # the periods it does not cover
    late = charge_periods(bills, hold, hx, [{"date": "2026-01-05", "amount": 10.0},
                                            {"date": "2026-03-01", "amount": 70.0}])
    # the 10.0 landed inside period 1, but the ledger does not cover the whole
    # of it, so the figure is shown and left out of the total
    assert late[0]["income"] == 10.0 and late[0]["income_complete"] is False
    assert late[0]["total_return"] == 1000.0     # market gain still stands
    assert late[1]["income"] == 70.0 and late[1]["income_complete"] is True
    # coverage reports the priceable share, so a half-priced account is visible
    half = charge_periods(bills, hold + [{"symbol": "ZZZ", "quantity": 1.0,
                                          "value": 12000.0}], hx, divs)
    assert abs(half[0]["coverage"] - 0.5) < 1e-9, half[0]["coverage"]
    # nothing to price, or nothing billed, yields no periods rather than zeros
    assert charge_periods(bills, hold, {"series": {}}, divs)[0]["market_gain"] is None
    assert charge_periods([], hold, hx, divs) == []
    assert charge_periods(bills, [], hx, divs) == []
    # the account filter keeps another account's dividends out — leaving no
    # income record at all, which reads as unknown rather than as zero
    other = charge_periods(bills, hold, hx, divs, match="*9")
    assert other[0]["income"] == 0.0 and other[0]["income_complete"] is False

    # ---- return on the advised money, gross and after the fee ----------------
    hxb = {"series": dict(hx["series"],
                          VTI={"2025-10-01": 200.0, "2026-04-08": 210.0})}
    perf = account_performance(per, hxb)
    # the window opens where the first period opens and closes where the last
    # one closes — 10,000 in, 9,500 out
    assert perf["start"] == "2025-10-10" and perf["end"] == "2026-04-10"
    assert perf["start_value"] == 10000.0 and perf["end_value"] == 9500.0
    # +1000 then -1500 of market, 100 of income, 510 of fees
    assert perf["market_gain"] == -500.0 and perf["income"] == 100.0
    assert perf["fee"] == 510.0 and perf["total_return_dollars"] == -400.0
    assert perf["net_dollars"] == -910.0
    assert abs(perf["gross_return"] + 400 / 10000) < 1e-9
    assert abs(perf["net_return"] + 910 / 10000) < 1e-9
    # the fee is the gap between the two, always in the same direction
    assert perf["fee_drag"] > 0
    assert abs(perf["fee_drag"] - 510 / 10000) < 1e-9
    # measured against the same window of the same weekly closes
    assert perf["benchmark"] == "VTI"
    assert abs(perf["benchmark_return"] - 0.05) < 1e-9
    assert abs(perf["excess_net"] - (perf["net_return"] - 0.05)) < 1e-12
    assert perf["excess_net"] < perf["excess_gross"]     # the fee is real
    # a single period is not a return series — refused rather than annualised
    assert account_performance(per[:1], hxb) is None
    # no benchmark in the history: the return still stands, the comparison
    # is reported as absent rather than filled with a zero
    nb = account_performance(per, {"series": {}})
    assert nb["benchmark_return"] is None and nb["excess_net"] is None

    # ---- the second yardstick: same asset mix, not just the index -----------
    hxr = {"series": dict(hxb["series"],
                          BND={"2025-10-01": 100.0, "2026-04-08": 102.0},
                          BIL={"2025-10-01": 100.0, "2026-04-08": 101.0},
                          VXUS={"2025-10-01": 50.0, "2026-04-08": 56.0})}
    # a blend is the weighted sum of its legs' total returns over the window
    r, lab = benchmark_return(hxr, "2025-10-01", "2026-04-08",
                              [["VTI", 0.8], ["BND", 0.2]])
    assert abs(r - (0.8 * 0.05 + 0.2 * 0.02)) < 1e-12, r
    assert lab == "80% VTI / 20% BND", lab
    # weights that do not sum to 1 are normalised rather than rejected
    r2, _ = benchmark_return(hxr, "2025-10-01", "2026-04-08",
                             [["VTI", 8.0], ["BND", 2.0]])
    assert abs(r2 - r) < 1e-12
    # a leg with no prices disables the blend — never renormalised onto the
    # legs that happen to exist, which would silently change the comparison
    gone, _ = benchmark_return(hxr, "2025-10-01", "2026-04-08",
                               [["VTI", 0.8], ["NOPE", 0.2]])
    assert gone is None
    # a plain symbol still works, and still reports its own name
    assert benchmark_return(hxr, "2025-10-01", "2026-04-08", "VTI")[1] == "VTI"

    # the mix is read through funds; a direct holding is equity, preferreds and
    # convertibles are credit, and an unknown fund tilts nothing
    fundtab = {
        "AGG1": {"symbol": "AGG1", "category": "Intermediate Core Bond",
                 "asset_classes": {"bondPosition": 0.95, "cashPosition": 0.05}},
        "EUR1": {"symbol": "EUR1", "category": "Europe Stock",
                 "asset_classes": {"stockPosition": 1.0}},
        "PFD1": {"symbol": "PFD1", "category": "Preferred Stock",
                 "asset_classes": {"preferredPosition": 1.0}},
        "MYST": {"symbol": "MYST", "category": "Unknown", "asset_classes": {}},
    }
    mixed = asset_mix([{"symbol": "AAPL", "value": 6000.0},
                       {"symbol": "EUR1", "value": 1000.0},
                       {"symbol": "AGG1", "value": 2000.0},
                       {"symbol": "PFD1", "value": 1000.0},
                       {"symbol": "MYST", "value": 1000.0}], fundtab)
    # classified base is 10,000 — the unclassified 1,000 is excluded from the
    # denominator rather than dumped into a class
    assert abs(mixed["stocks"] - 0.70) < 1e-9, mixed
    assert abs(mixed["ex_us_equity"] - 0.10) < 1e-9
    assert abs(mixed["bonds"] - 0.19) < 1e-9 and abs(mixed["preferred"] - 0.10) < 1e-9
    assert abs(mixed["unclassified_share"] - 1000 / 11000) < 1e-9
    blend = derive_blend(mixed)
    got = dict(blend)
    # 60% US equity, 10% ex-US, 19% bonds + 10% preferred as credit, 1% cash
    assert abs(got["VTI"] - 0.60) < 0.005 and abs(got["VXUS"] - 0.10) < 0.005
    assert abs(got["BND"] - 0.29) < 0.005 and abs(got["BIL"] - 0.01) < 0.005
    assert abs(sum(w for _, w in blend) - 1.0) < 1e-6
    # the building blocks are configurable
    swapped = dict(derive_blend(mixed, {"bonds": "AGG"}))
    assert "AGG" in swapped and "BND" not in swapped
    # an all-equity account collapses to the equity leg alone
    allstock = asset_mix([{"symbol": "AAPL", "value": 100.0}], fundtab)
    assert derive_blend(allstock) == [["VTI", 1.0]]
    assert asset_mix([], fundtab) is None and derive_blend(None) is None

    # both comparisons ride on one performance record, and they can disagree
    rm = account_performance(per, hxr, "VTI", [["VTI", 0.5], ["BND", 0.5]])
    assert abs(rm["blend_return"] - 0.035) < 1e-12
    assert rm["blend_label"] == "50% VTI / 50% BND"
    # behind the index by more than it is behind the softer same-mix yardstick
    assert rm["excess_net"] < rm["excess_net_blend"] < 0
    assert abs(rm["blend_dollars"] - round(rm["excess_net_blend"] * 10000.0, 2)) < 0.01
    # the headline verdict stays on the headline benchmark
    assert abs(rm["verdict"]["excess_net"] - rm["excess_net"]) < 1e-12
    # a blend that cannot be priced names the leg that is missing
    broke = account_performance(per, hxr, "VTI", [["VTI", 0.5], ["NOPE", 0.5]])
    assert broke["blend_return"] is None and broke["blend_missing"] == ["NOPE"]
    assert "excess_net_blend" not in broke
    # no blend asked for: the record carries no second comparison at all
    assert "blend" not in account_performance(per, hxr, "VTI", None)

    # every symbol the comparison needs is reported for the price fetch
    syms = benchmark_symbols({"benchmark": "VTI", "benchmark_blend": "auto"})
    assert syms[0] == "VTI" and "BND" in syms and "VXUS" in syms and "BIL" in syms
    assert len(syms) == len(set(syms))           # fetched once, not per use
    pinned = benchmark_symbols({"benchmark": "SPY",
                                "benchmark_blend": [["ACWI", 0.6], ["AGG", 0.4]]})
    assert "SPY" in pinned and "ACWI" in pinned and "AGG" in pinned
    # the blend switched off leaves only the headline benchmark to fetch
    assert benchmark_symbols({"benchmark_blend": False}) == ["VTI"]

    # combined across accounts: dollar-weighted, never an average of percents
    big = dict(perf, start_value=100000.0, market_gain=5000.0, income=0.0,
               fee=1000.0, end_value=105000.0)
    small = dict(perf, start_value=10000.0, market_gain=-500.0, income=0.0,
                 fee=100.0, end_value=9500.0)
    comb = advised_performance([{"label": "A", "performance": big},
                                {"label": "B", "performance": small}], hxb)
    assert comb["start_value"] == 110000.0 and comb["n_accounts"] == 2
    # 4500 of gain on 110k — not the midpoint of +5% and -5%
    assert abs(comb["gross_return"] - 4500 / 110000) < 1e-9
    assert abs(comb["net_return"] - 3400 / 110000) < 1e-9
    assert comb["fee"] == 1100.0 and comb["aligned"] is True
    assert abs(comb["fee_share_of_return"] - 1100 / 4500) < 1e-9
    # accounts billed on different cycles span the union, and say so
    off = dict(small, start="2025-01-01", end="2026-04-08")
    wide = advised_performance([{"label": "A", "performance": big},
                                {"label": "B", "performance": off}], hxb)
    assert wide["aligned"] is False and wide["start"] == "2025-01-01"
    # nothing measurable -> None, so the page can say so instead of showing 0%
    assert advised_performance([{"label": "A", "performance": None}], hxb) is None
    assert advised_performance([], hxb) is None

    # ---- is the fee paying off? yes or no -----------------------------------
    # behind the index after the fee -> no, with the shortfall in real money
    v = comb["verdict"]
    assert v["answer"] == "no", v
    assert abs(v["excess_net"] - comb["excess_net"]) < 1e-12
    assert abs(v["dollars"] - round(comb["excess_net"] * 110000.0, 2)) < 0.01
    assert v["dollars"] < 0                      # behind means money lost
    # ahead after the fee -> yes, and the dollars are what it was worth
    win = advised_performance([{"label": "A", "performance": dict(
        big, market_gain=20000.0, fee=1000.0)}], hxb)["verdict"]
    assert win["answer"] == "yes" and win["dollars"] > 0
    # the case the fee argument turns on: the picks beat the index, the fee
    # took more than the win, so the answer is still no
    flip = fee_verdict({"start_value": 100000.0, "years": 1.0,
                        "excess_gross": 0.004, "excess_net": -0.006})
    assert flip["answer"] == "no" and flip["fee_flipped"] is True
    assert fee_verdict({"start_value": 100000.0, "years": 1.0,
                        "excess_gross": 0.02, "excess_net": 0.01})["fee_flipped"] is False
    # a short record cannot prove a small edge either way, and says so
    assert flip["conclusive"] is False and flip["years_needed"] > 1
    # a big *cumulative* excess is not a big annual one: 8% spread over 40
    # years is 0.2%/yr, which 40 years cannot distinguish from luck
    spread = fee_verdict({"start_value": 100000.0, "years": 40.0,
                          "excess_gross": 0.09, "excess_net": 0.08})
    assert spread["answer"] == "yes" and spread["conclusive"] is False
    assert spread["years_needed"] > 40
    # ...but a genuine 4%/yr edge sustained over 40 years can be called, and
    # is measured off the annualised figures rather than excess/years
    strong = fee_verdict({"start_value": 100000.0, "years": 40.0,
                          "net_annualised": 0.10, "benchmark_return": 9.28,
                          "excess_gross": 5.0, "excess_net": 4.5})
    assert abs(strong["alpha_annual"] - 0.04) < 5e-4, strong["alpha_annual"]
    assert strong["answer"] == "yes" and strong["conclusive"] is True
    assert strong["years_needed"] < 10
    # no benchmark -> refuse the question rather than answer it wrongly
    unk = fee_verdict({"start_value": 100000.0, "years": 1.0,
                       "excess_gross": None, "excess_net": None})
    assert unk["answer"] == "unknown" and "nothing to compare" in unk["because"]
    assert fee_verdict(None) is None

    # ---- discovering advised accounts from the charges themselves ------------
    harvest = [{"account": "Alex 401k R/O IRA(*1234)", "date": "2026-07-10",
                "amount": 5457.93, "billed": "quarterly", "periods_per_year": 4,
                "source": "activity.xlsx"}]
    disc = discover_accounts(harvest, [])
    assert len(disc) == 1 and disc[0]["match"] == "*1234", disc
    assert discover_accounts([{"account": "[NAME] 401k R/O IRA(*1234)",
                               "date": "2026-07-10", "amount": 1.0}],
                             [])[0]["label"] == "401k R/O IRA(*1234)"
    assert disc[0]["discovered"] and disc[0]["annual_rate"] is None
    # already declared -> the config keeps ownership of the label and schedule
    assert discover_accounts(harvest, [{"match": "*1234"}]) == []
    assert _account_key("Taxable brokerage") == "Taxable brokerage"

    # ---- detectability -------------------------------------------------------
    # 1.1% hurdle against 4% tracking error: about a century of annual data
    yd = years_to_detect(0.011, 0.04)
    assert 90 < yd < 120, yd
    # a bigger edge is provable sooner; a free one is not a question
    assert years_to_detect(0.04, 0.04) < yd
    assert years_to_detect(0, 0.04) is None
    # odds of coming out ahead fall as the fee compounds over more years
    p1 = prob_beats_passive(0.011, 0.04, 1)
    p10 = prob_beats_passive(0.011, 0.04, 10)
    assert 0.35 < p1 < 0.5 and 0.1 < p10 < 0.25, (p1, p10)
    assert p10 < p1

    # ---- t-test --------------------------------------------------------------
    flat = excess_return_test([0.01, -0.01, 0.02, -0.02, 0.005])
    assert flat["n"] == 5 and not flat["significant"], flat
    assert flat["ci_lo"] < 0 < flat["ci_hi"]         # excludes nothing
    strong = excess_return_test([0.05, 0.04, 0.06, 0.05, 0.045, 0.055, 0.05, 0.048])
    assert strong["significant"] is True and strong["ci_lo"] > 0, strong
    assert excess_return_test([0.01]) is None
    # a series with no variance at all is a data artefact, not a finding
    assert excess_return_test([0.05] * 8) is None

    # ---- compounding ---------------------------------------------------------
    d = compounded_drag(1_000_000, 0.011, 20, 0.058)
    assert d["drag"] > 500_000, d          # a 1.1% fee on $1M over 20 years
    assert 0 < d["drag_pct"] < 0.3, d
    assert compounded_drag(1000, 0.0, 10, 0.05)["drag"] == 0

    # ---- end to end ----------------------------------------------------------
    cfg = {
        "passive_alternative_er": 0.0003, "tracking_error": 0.04,
        "accounts": [{"match": "*1234", "institution": "Test Advisors",
                      "label": "Advised IRA", "annual_rate": 0.011,
                      "billed": "quarterly"}],
        "charges": q + [{"account": "*1234", "date": "2026-01-02", "amount": 1000}],
        "returns": [{"account": "*1234", "account_return": 0.11,
                     "benchmark_return": 0.10},
                    {"account": "*1234", "account_return": 0.06,
                     "benchmark_return": 0.08},
                    {"account": "*1234", "account_return": 0.14,
                     "benchmark_return": 0.12}],
    }
    for c in cfg["charges"]:
        c["account"] = "*1234"
    held = {"Advisory IRA (*1234)": [
        {"symbol": "VTI", "value": 300000.0, "cost_basis": 200000.0,
         "cost_value": 300000.0},
        {"symbol": "AGG", "value": 100000.0, "cost_basis": 95000.0,
         "cost_value": 100000.0}]}
    pf = {"total_portfolio": 800000.0,
          "fund_analysis": {"funds": [{"symbol": "VTI", "expense_ratio": 0.0003},
                                      {"symbol": "AGG", "expense_ratio": 0.0003}],
                            "weighted_expense": 0.0003}}
    out = evaluate(pf, cfg, held, years_to_retirement=20, years_horizon=36,
                   charges=[])
    assert out["configured"] is True
    r = out["accounts"][0]
    assert r["matched_accounts"] == 1 and r["value"] == 400000.0, r
    assert r["rate_source"] == "charges"
    # five quarterly bills of 1000 = 4000/yr on 400k = 1.00%, under the 1.10%
    # schedule, so the gap is negative and the measured rate is what's used
    assert abs(r["measured_rate"] - 0.01) < 0.0005, r["measured_rate"]
    assert r["rate_gap"] < 0
    assert abs(r["all_in"] - (r["rate"] + 0.0003)) < 1e-9
    assert abs(r["breakeven_alpha"] - r["rate"]) < 1e-9   # funds already passive-priced
    assert r["gain"] == 105000.0, r["gain"]
    # measured 1.00% against 4% tracking error — 125 years of annual returns
    assert 120 < r["years_to_detect"] < 130, r["years_to_detect"]
    assert r["observed"]["n"] == 3 and not r["observed"]["significant"]
    assert out["pct_of_portfolio"] == 0.5
    assert abs(out["blended_rate"] - 0.01) < 0.0005
    assert out["drag_horizon"]["drag"] > out["drag_retirement"]["drag"]

    # ---- harvested charges alone are enough ---------------------------------
    # nothing declared, but the ledger shows *1234 was billed an advisory fee:
    # that is evidence, so the account is evaluated and the rate measured
    hv = [{"account": "Advisory IRA (*1234)", "date": "2026-07-10",
           "amount": 1000.0, "billed": "quarterly", "periods_per_year": 4,
           "source": "activity.xlsx"}]
    auto = evaluate(pf, {}, held, charges=hv)
    assert auto["configured"] is True and auto["discovered_accounts"] == 1
    ar = auto["accounts"][0]
    assert ar["match"] == "*1234" and ar["discovered"] is True
    assert ar["declared_rate"] is None and ar["rate_source"] == "charges"
    # 1000/quarter = 4000/yr on 400k
    assert abs(ar["rate"] - 0.01) < 1e-9 and ar["fee_annual"] == 4000.0
    assert ar["rate_gap"] is None and ar["billed"] == "quarterly"
    assert ar["charges_from"] == ["activity.xlsx"]
    # one bill extrapolated to a year is flagged, not passed off as measured
    assert any("single quarterly charge" in g for g in auto["data_gaps"]), \
        auto["data_gaps"]
    # four quarterly bills that ramp: the trailing year is what was paid, the
    # last bill is what it costs now, and the gap between them is reported
    ramp = [dict(hv[0], date=d, amount=amt) for d, amt in
            (("2025-11-06", 2709.26), ("2026-01-09", 5240.09),
             ("2026-04-10", 4875.94), ("2026-07-10", 5457.93))]
    rr = evaluate(pf, {}, held, charges=ramp)["accounts"][0]
    assert rr["charges_n"] == 4
    assert abs(rr["charges_annualised"] - 18283.22) < 0.01, rr["charges_annualised"]
    assert rr["run_rate_annual"] == round(5457.93 * 4, 2)
    assert any("not level" in g for g in
               evaluate(pf, {}, held, charges=ramp)["data_gaps"])
    # extract repairs an impossible date at the source; the repair is reported
    # here rather than left for the reader to notice
    fixed = [dict(c) for c in ramp]
    fixed[0] = dict(fixed[0], date_repaired_from="2026-11-06")
    out_fixed = evaluate(pf, {}, held, charges=fixed)
    assert any("cannot be in the future" in g and "2025-11-06" in g
               for g in out_fixed["data_gaps"]), out_fixed["data_gaps"]
    assert "2025-11-06*" in out_fixed["accounts"][0]["charges_dates"]
    # a date that somehow survives unrepaired must not win "most recent" and
    # drag the run rate down with it
    bad = [dict(c) for c in ramp]
    bad[0]["date"] = "2099-11-06"
    out_bad = evaluate(pf, {}, held, charges=bad)
    assert out_bad["accounts"][0]["charges_annualised"] == rr["charges_annualised"]
    assert out_bad["accounts"][0]["run_rate_annual"] == rr["run_rate_annual"]
    # ...and with no stated period it is dropped rather than allowed to stretch
    # the span it is measured over
    undated = [{"date": "2025-01-02", "amount": 1000},
               {"date": "2025-04-01", "amount": 1000},
               {"date": "2099-01-01", "amount": 1000}]
    assert _annualise_charges(undated)[2] == 2

    # a declared account with no rate of its own still runs off its charges
    lean = evaluate(pf, {"accounts": [{"match": "*1234", "label": "WF"}]},
                    held, charges=hv)
    assert lean["accounts"][0]["label"] == "WF"
    assert lean["discovered_accounts"] == 0 and lean["accounts"][0]["fee_annual"] == 4000.0

    # not configured -> says so rather than guessing which accounts are advised
    assert evaluate(pf, {}, held, charges=[])["configured"] is False
    assert evaluate(pf, {"accounts": [{"match": "*9"}]}, held,
                    charges=[])["configured"] is False

    # a configured account that matches nothing is reported, not silently zeroed
    miss = evaluate(pf, {"accounts": [{"match": "*9999", "annual_rate": 0.01,
                                       "value": 50000}]}, held, charges=[])
    assert miss["accounts"][0]["value"] == 50000.0
    assert any("*9999" in g for g in miss["data_gaps"]), miss["data_gaps"]

    print("fees self-test OK")
