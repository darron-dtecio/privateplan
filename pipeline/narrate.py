"""Write data/<SYMBOL>/narrative.json from the fetched data, with no LLM.

Usage:
    python pipeline/narrate.py MSFT [MSFT ...] [--force] [--stdout]
    python pipeline/narrate.py --selftest

The /analyze skill ends by having Claude read the filings and write the story
onto the dashboard. Everything that story is *made of* is already computed by
the time it runs: growth and its trend, margins and their drift, cash
generation against capex, the balance sheet, where analyst consensus stops and
the model starts, and what the scraped chatter says. So this module writes the
same narrative.json from those numbers instead — every sentence assembled from
a value that exists in financials.json, forecast.json, estimates.json,
market.json, sentiment.json or fund.json.

It writes `"method": "rules"` into the file, and the dashboards say so, because
a reader is owed the difference between a judgement and an arithmetic result.

What this deliberately does NOT do, and what /analyze is still for:

- web research — earnings-call tone, analyst commentary, product cycles,
  anything material that is not in a filing or a quote feed;
- reading the press release for guidance and overriding the model's
  assumptions against it (step 4 of the skill);
- discovering an IR workbook, and the segment tables that come with it.

Facts absent from the data produce no sentence at all rather than a hedge: a
narrative that says nothing about segments is correct when no segment data was
fetched.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).resolve().parent.parent

# The dashboard's own formatters: prose that disagrees with the tile above it
# is worse than no prose.
from render import eps_fmt, money, pct  # noqa: E402
# One definition of where "mildly positive" starts, shared with the scorer.
from sentiment import label_for  # noqa: E402


# Thresholds the sentences below are written against. A move smaller than these
# is noise the prose should not dignify — "margin widened 0 basis points" reads
# as a finding and is not one.
MARGIN_MOVE_BPS = 25      # a margin move worth naming, in basis points
GROWTH_MOVE = 0.01        # a change in the growth rate worth naming


def load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _n(*vals):
    """First value that is not None — for metrics a filer may not tag."""
    for v in vals:
        if v is not None:
            return v
    return None


def _sum(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return sum(vals) if len(vals) == len(rows) and rows else None


def _ratio(a: float | None, b: float | None) -> float | None:
    return a / b if a is not None and b else None


def _bps(now: float | None, then: float | None) -> int | None:
    return round((now - then) * 10000) if now is not None and then is not None else None


# ------------------------------------------------------------------ facts --
def company_facts(fin: dict, fc: dict, est: dict, market: dict,
                  sentiment: dict | None) -> dict:
    """Every number the sentences below are allowed to use, computed once."""
    hist = sorted((q for q in fin.get("quarters", []) if q.get("revenue")),
                  key=lambda q: q["end_date"])
    f: dict = {"company": fin.get("company") or fin.get("ticker"),
               "ticker": fin.get("ticker"), "hist": hist}
    if not hist:
        return f

    last = hist[-1]
    by_key = {(q["fiscal_year"], q["fiscal_quarter"]): q for q in hist}
    f["last"] = last
    f["label"] = last["fiscal_label"]
    f["calendar"] = last.get("calendar_label")

    def yoy(q):
        prev = by_key.get((q["fiscal_year"] - 1, q["fiscal_quarter"]))
        return _ratio(q["revenue"], prev["revenue"]) - 1 if prev and prev.get("revenue") else None

    f["rev_yoy"] = yoy(last)
    f["rev_yoy_prev"] = yoy(hist[-2]) if len(hist) >= 2 else None
    if f["rev_yoy"] is not None and f["rev_yoy_prev"] is not None:
        f["growth_delta"] = f["rev_yoy"] - f["rev_yoy_prev"]

    ttm, prior_ttm = hist[-4:], hist[-8:-4]
    f["ttm_revenue"] = _sum(ttm, "revenue") if len(ttm) == 4 else None
    f["prior_ttm_revenue"] = _sum(prior_ttm, "revenue") if len(prior_ttm) == 4 else None
    f["ttm_growth"] = (_ratio(f["ttm_revenue"], f["prior_ttm_revenue"]) - 1
                       if f["ttm_revenue"] and f["prior_ttm_revenue"] else None)
    f["ttm_net_income"] = _sum(ttm, "net_income") if len(ttm) == 4 else None
    f["ttm_ocf"] = _sum(ttm, "operating_cash_flow") if len(ttm) == 4 else None
    f["ttm_capex"] = _sum(ttm, "capex") if len(ttm) == 4 else None
    f["ttm_fcf"] = (f["ttm_ocf"] - f["ttm_capex"]
                    if f["ttm_ocf"] is not None and f["ttm_capex"] is not None else None)
    f["ttm_net_margin"] = _ratio(f["ttm_net_income"], f["ttm_revenue"])
    f["ttm_fcf_margin"] = _ratio(f["ttm_fcf"], f["ttm_revenue"])
    f["ttm_capex_to_ocf"] = _ratio(f["ttm_capex"], f["ttm_ocf"])
    f["ttm_dividends"] = _sum(ttm, "dividends_paid") if len(ttm) == 4 else None
    f["ttm_buybacks"] = _sum(ttm, "buybacks") if len(ttm) == 4 else None
    returned = [v for v in (f["ttm_dividends"], f["ttm_buybacks"]) if v]
    f["ttm_returned"] = sum(returned) if returned else None
    f["payout_of_fcf"] = _ratio(f["ttm_returned"], f["ttm_fcf"])

    f["gross_margin"] = _ratio(last.get("gross_profit"), last.get("revenue"))
    f["op_margin"] = _ratio(last.get("operating_income"), last.get("revenue"))
    f["net_margin"] = _ratio(last.get("net_income"), last.get("revenue"))
    year_ago = by_key.get((last["fiscal_year"] - 1, last["fiscal_quarter"]))
    if year_ago:
        f["gross_margin_bps"] = _bps(f["gross_margin"],
                                     _ratio(year_ago.get("gross_profit"), year_ago.get("revenue")))
        f["op_margin_bps"] = _bps(f["op_margin"],
                                  _ratio(year_ago.get("operating_income"), year_ago.get("revenue")))

    f["net_cash"] = last.get("net_cash")
    f["total_liquidity"] = last.get("total_liquidity")
    f["long_term_debt"] = last.get("long_term_debt")

    shares = [q["shares_diluted"] for q in hist[-9:] if q.get("shares_diluted")]
    if len(shares) >= 5:
        # Annualised change in the diluted count: negative is a buyback that is
        # actually shrinking the share base, positive is dilution.
        f["share_change_yoy"] = shares[-1] / shares[-5] - 1

    # --- forecast: where consensus stops and the model starts ----------------
    a = (fc or {}).get("assumptions") or {}
    qs = (fc or {}).get("quarters") or []
    f["assumptions"] = a
    f["forecast_quarters"] = qs
    f["consensus_quarters"] = [q for q in qs if "consensus" in (q.get("source") or "")]
    f["modeled_quarters"] = [q for q in qs if "consensus" not in (q.get("source") or "")]
    f["first_modeled"] = f["modeled_quarters"][0] if f["modeled_quarters"] else None
    f["last_forecast"] = qs[-1] if qs else None
    f["growth_by_fy"] = a.get("growth_by_fy") or {}
    f["long_run_growth"] = a.get("long_run_growth")
    f["consensus_growth"] = a.get("consensus_growth_next_fy")
    f["margin_drift"] = a.get("margin_drift_per_year")
    f["growth_vol"] = a.get("growth_volatility_8q")
    f["cagr3"] = a.get("revenue_cagr_3y")
    f["override"] = a.get("override_applied")
    if qs and f["ttm_revenue"]:
        tail = qs[-4:]
        f["forecast_exit_revenue"] = _sum([{"revenue": q["revenue"]["base"]} for q in tail],
                                          "revenue")
        f["forecast_total_growth"] = _ratio(f["forecast_exit_revenue"], f["ttm_revenue"])

    # --- market & consensus --------------------------------------------------
    stats = (market or {}).get("stats") or {}
    prof = (market or {}).get("profile") or {}
    f["price"] = _n(stats.get("price"), stats.get("regular_market_price"))
    f["market_cap"] = stats.get("market_cap")
    f["pe"] = _n(stats.get("trailing_pe"), stats.get("pe"))
    f["forward_pe"] = stats.get("forward_pe")
    f["week52_high"], f["week52_low"] = stats.get("fifty_two_week_high"), stats.get("fifty_two_week_low")
    f["sector"], f["industry"] = prof.get("sector"), prof.get("industry")

    hist_eps = (est or {}).get("earnings_history") or []
    beats = [h for h in hist_eps if h.get("surprise_percent") is not None]
    f["eps_surprises"] = beats[-4:]
    f["last_surprise"] = beats[-1] if beats else None
    f["beat_streak"] = sum(1 for h in beats[-4:] if (h.get("surprise_percent") or 0) > 0)
    pt = (est or {}).get("price_targets") or {}
    f["target_mean"] = pt.get("mean") or pt.get("target_mean_price")
    f["upside"] = (_ratio(f["target_mean"], f["price"]) - 1
                   if f["target_mean"] and f["price"] else None)
    f["next_earnings"] = ((est or {}).get("next_earnings_dates") or [None])[0]

    s = (sentiment or {}).get("summary") or {}
    f["sentiment"] = s
    f["sentiment_by_source"] = (sentiment or {}).get("by_source") or {}
    f["sentiment_trend"] = (sentiment or {}).get("trend") or {}
    return f


# --------------------------------------------------------------- sections --
def headline(f: dict) -> str:
    """One line: the growth regime, the margin direction, what the model carries."""
    g, drift = f.get("rev_yoy"), f.get("margin_drift") or 0
    lr = f.get("long_run_growth")
    if g is None:
        return f'{f["company"]} — {f["label"]} reported; the model carries the current run rate forward.'
    pace = ("shrinking" if g < -0.01 else "flat" if g < 0.02
            else "growing modestly" if g < 0.08
            else "growing" if g < 0.20 else "growing fast")
    margin = ("with margins widening" if drift > 0.002
              else "with margins compressing" if drift < -0.002
              else "with margins holding")
    tail = (f", and the model carries {pct(lr, 0)} out to {f['last_forecast']['fiscal_label']}"
            if lr is not None and f.get("last_forecast") else "")
    return (f'Revenue {pace} at {pct(g, 1)} year on year {margin}{tail}.')


def executive_summary(f: dict) -> str:
    """Four or five sentences, each one a number that exists upstream."""
    s = []
    if f.get("rev_yoy") is not None:
        s.append(f'{f["company"]} reported {money(f["last"]["revenue"])} of revenue in '
                 f'{f["label"]}, {pct(f["rev_yoy"], 1)} against the same quarter a year '
                 f'earlier, on {money(f.get("ttm_revenue"))} over the trailing twelve months.')
    if f.get("ttm_net_margin") is not None:
        bits = f'Net margin runs {pct(f["ttm_net_margin"], 1)} on a trailing basis'
        ob = f.get("op_margin_bps")
        if ob is not None and abs(ob) >= MARGIN_MOVE_BPS:
            bits += (f', and operating margin {"widened" if ob > 0 else "narrowed"} '
                     f'{abs(ob)} basis points year on year')
        elif ob is not None:
            bits += ', with operating margin flat year on year'
        s.append(bits + ".")
    if f.get("ttm_fcf") is not None:
        cash = (f'Free cash flow is {money(f["ttm_fcf"])} over twelve months'
                + (f' — {pct(f["ttm_fcf_margin"], 1)} of revenue' if f.get("ttm_fcf_margin") else ""))
        if f.get("ttm_capex_to_ocf") is not None:
            cash += (f', with {pct(f["ttm_capex_to_ocf"], 0)} of operating cash flow '
                     f'going back out as capital expenditure')
        s.append(cash + ".")
    if f.get("net_cash") is not None:
        pos = "net cash of" if f["net_cash"] >= 0 else "net debt of"
        s.append(f'The balance sheet carries {pos} {money(abs(f["net_cash"]))}'
                 + (f' against {money(f["total_liquidity"])} of total liquidity.'
                    if f.get("total_liquidity") else "."))
    if f.get("consensus_quarters") and f.get("first_modeled"):
        s.append(f'The twelve-quarter path follows analyst consensus through '
                 f'{f["consensus_quarters"][-1]["fiscal_label"]} and is modeled from '
                 f'{f["first_modeled"]["fiscal_label"]} onward.')
    elif f.get("last_forecast"):
        s.append(f'The twelve-quarter path is modeled throughout — no usable analyst '
                 f'consensus was available to anchor it.')
    return " ".join(s)


def quarter_recap(f: dict) -> str:
    """What the last reported quarter did, against a year ago and against consensus."""
    last = f.get("last")
    if not last:
        return ""
    s = [f'{f["label"]} ({f.get("calendar") or last["end_date"]}) closed with '
         f'{money(last["revenue"])} of revenue']
    if f.get("rev_yoy") is not None:
        s[0] += f', {pct(f["rev_yoy"], 1)} year on year'
    if f.get("growth_delta") is not None and abs(f["growth_delta"]) >= GROWTH_MOVE:
        way = "an acceleration from" if f["growth_delta"] > 0 else "a deceleration from"
        s[0] += f' — {way} {pct(f["rev_yoy_prev"], 1)} the quarter before'
    s[0] += "."
    if last.get("eps_diluted") is not None:
        line = f'Diluted EPS was {eps_fmt(last["eps_diluted"])}'
        sur = f.get("last_surprise")
        if sur and sur.get("surprise_percent") is not None:
            verb = "ahead of" if sur["surprise_percent"] >= 0 else "short of"
            line += (f', {verb} the {eps_fmt(sur.get("eps_estimate"))} consensus by '
                     f'{pct(abs(sur["surprise_percent"]) / 100, 1)}')
        s.append(line + ".")
    if f.get("gross_margin") is not None:
        line = f'Gross margin was {pct(f["gross_margin"], 1)}'
        gb = f.get("gross_margin_bps")
        if gb is not None and abs(gb) >= MARGIN_MOVE_BPS:
            line += f', {"up" if gb > 0 else "down"} {abs(gb)} basis points from a year ago'
        elif gb is not None:
            line += ', level with a year ago'
        s.append(line + ".")
    if f.get("ttm_returned"):
        parts = []
        if f.get("ttm_buybacks"):
            parts.append(f'{money(f["ttm_buybacks"])} of buybacks')
        if f.get("ttm_dividends"):
            parts.append(f'{money(f["ttm_dividends"])} of dividends')
        line = f'Capital returned over twelve months: {" and ".join(parts)}'
        if f.get("payout_of_fcf") is not None:
            line += f', {pct(f["payout_of_fcf"], 0)} of free cash flow'
        s.append(line + ".")
    return " ".join(s)


def thesis(f: dict) -> dict:
    """Bull and bear cases as thresholds crossed, strongest signal first."""
    bull, bear = [], []

    g, dg = f.get("rev_yoy"), f.get("growth_delta")
    if g is not None and g >= 0.15:
        bull.append(f'Revenue is compounding at {pct(g, 1)} year on year, well above the '
                    f'{pct(f.get("cagr3"), 1)} three-year CAGR the model starts from.'
                    if f.get("cagr3") else
                    f'Revenue is compounding at {pct(g, 1)} year on year.')
    if dg is not None and dg >= 2 * GROWTH_MOVE:
        bull.append(f'Growth is accelerating — {pct(g, 1)} this quarter against '
                    f'{pct(f["rev_yoy_prev"], 1)} last.')
    if dg is not None and dg <= -2 * GROWTH_MOVE:
        bear.append(f'Growth is decelerating — {pct(g, 1)} this quarter against '
                    f'{pct(f["rev_yoy_prev"], 1)} last, and the model extrapolates from '
                    f'the slower rate.')
    if g is not None and g < 0.02:
        bear.append(f'Revenue is essentially flat at {pct(g, 1)} year on year; the '
                    f'forecast has no organic growth to compound.')

    ob = f.get("op_margin_bps")
    if ob is not None and ob >= 4 * MARGIN_MOVE_BPS:
        bull.append(f'Operating margin widened {ob} basis points year on year to '
                    f'{pct(f["op_margin"], 1)} — operating leverage is showing up in '
                    f'the reported numbers, not just the model.')
    if ob is not None and ob <= -4 * MARGIN_MOVE_BPS:
        bear.append(f'Operating margin narrowed {abs(ob)} basis points year on year to '
                    f'{pct(f["op_margin"], 1)}.')

    fm, c2o = f.get("ttm_fcf_margin"), f.get("ttm_capex_to_ocf")
    if fm is not None and fm >= 0.20:
        bull.append(f'Free cash flow is {pct(fm, 1)} of revenue ({money(f["ttm_fcf"])} '
                    f'over twelve months) — the earnings convert to cash.')
    if fm is not None and fm < 0:
        bear.append(f'Free cash flow is negative at {money(f["ttm_fcf"])} over twelve '
                    f'months; operations are consuming cash rather than producing it.')
    if c2o is not None and c2o >= 0.60:
        bear.append(f'Capital expenditure absorbs {pct(c2o, 0)} of operating cash flow, '
                    f'so reported earnings overstate what is left for shareholders.')
    elif c2o is not None and c2o <= 0.20 and fm and fm > 0:
        bull.append(f'Capex takes only {pct(c2o, 0)} of operating cash flow — growth here '
                    f'is not bought with the balance sheet.')

    nc = f.get("net_cash")
    if nc is not None and nc > 0 and f.get("ttm_revenue") and nc / f["ttm_revenue"] > 0.15:
        bull.append(f'{money(nc)} of net cash — roughly {pct(nc / f["ttm_revenue"], 0)} of '
                    f'annual revenue — funds buybacks, M&A or a bad year without new debt.')
    if nc is not None and nc < 0 and f.get("ttm_fcf") and f["ttm_fcf"] > 0:
        years = abs(nc) / f["ttm_fcf"]
        if years > 3:
            bear.append(f'Net debt of {money(abs(nc))} is {years:.1f} years of current free '
                        f'cash flow, which limits what the balance sheet can absorb.')

    sc = f.get("share_change_yoy")
    if sc is not None and sc <= -0.01:
        bull.append(f'The diluted share count is down {pct(abs(sc), 1)} year on year, so '
                    f'per-share results improve even on flat earnings.')
    if sc is not None and sc >= 0.02:
        bear.append(f'The diluted share count is up {pct(sc, 1)} year on year — dilution '
                    f'is taking a slice of every per-share figure below.')

    if f.get("beat_streak") == 4:
        bull.append('EPS has come in ahead of consensus in each of the last four reported '
                    'quarters.')
    elif f.get("eps_surprises") and f["beat_streak"] == 0:
        bear.append('EPS has missed consensus in each of the last four reported quarters, '
                    'so the estimates anchoring the near quarters have been optimistic.')

    up = f.get("upside")
    if up is not None and up >= 0.15:
        bull.append(f'The mean published price target of {eps_fmt(f["target_mean"])} sits '
                    f'{pct(up, 0)} above the last price.')
    if up is not None and up <= -0.05:
        bear.append(f'The last price is already above the mean published target of '
                    f'{eps_fmt(f["target_mean"])}.')

    vol = f.get("growth_vol")
    if vol is not None and vol >= 0.10:
        bear.append(f'Quarterly growth has a standard deviation of {pct(vol, 1)} over the '
                    f'last eight quarters — the single forecast line is a mid-point through '
                    f'a wide band.')

    if not bull:
        bull.append('Nothing in the reported fundamentals crosses this model\'s thresholds '
                    'for a bull point — the case, if there is one, is not in these numbers.')
    if not bear:
        bear.append('Nothing in the reported fundamentals crosses this model\'s thresholds '
                    'for a bear point; the risks below are the structural ones.')
    return {"bull": bull[:5], "bear": bear[:5]}


def segment_story(f: dict, segments: dict | None) -> list[dict]:
    """Per-segment growth, when segment data was actually fetched."""
    out = []
    for seg in (segments or {}).get("segments", []):
        qs = seg.get("quarters") or {}
        keys = list(qs)
        if len(keys) < 2:
            continue
        latest, prior = qs[keys[-1]], qs[keys[-2]]
        if not prior:
            continue
        change = latest / prior - 1
        direction = "grew" if change >= 0 else "declined"
        share = ""
        if f.get("last", {}).get("revenue"):
            share = (f' It is {pct(latest / f["last"]["revenue"], 0)} of the quarter\'s '
                     f'reported revenue.')
        out.append({"name": seg.get("name"),
                    "story": (f'{money(latest)} in {keys[-1]}, {direction} '
                              f'{pct(abs(change), 1)} from {keys[-2]}.{share}')})
    return out


def forecast_rationale(f: dict) -> str:
    """Where consensus ends, what the model does after it, and how wide it is."""
    if not f.get("forecast_quarters"):
        return ""
    s = []
    cons, mod = f.get("consensus_quarters") or [], f.get("modeled_quarters") or []
    if cons and mod:
        s.append(f'The first {len(cons)} quarter{"" if len(cons) == 1 else "s"} follow '
                 f'analyst consensus through {cons[-1]["fiscal_label"]}; the remaining '
                 f'{len(mod)} are modeled.')
    elif mod:
        s.append(f'All {len(mod)} forecast quarters are modeled — no analyst consensus '
                 f'was available to anchor the near quarters.')
    if f.get("growth_by_fy"):
        pairs = ", ".join(f'FY{y[2:]} {pct(v, 1)}' for y, v in
                          sorted(f["growth_by_fy"].items())[:4])
        s.append(f'Modeled revenue growth by fiscal year: {pairs}.')
    if f.get("long_run_growth") is not None:
        line = f'It decays toward a {pct(f["long_run_growth"], 1)} long-run rate'
        if f.get("cagr3") is not None:
            line += f', against a {pct(f["cagr3"], 1)} three-year historical CAGR'
        s.append(line + ".")
    if f.get("margin_drift"):
        way = "widen" if f["margin_drift"] > 0 else "compress"
        s.append(f'Net margin is carried to {way} {pct(abs(f["margin_drift"]), 2)} a year '
                 f'from the {pct(f["assumptions"].get("net_margin_recent_avg"), 1)} recent '
                 f'average.')
    if f.get("growth_vol") is not None and f["growth_vol"] >= 0.01:
        s.append(f'Realised quarterly growth has varied by {pct(f["growth_vol"], 1)} '
                 f'(standard deviation, last eight quarters), which is the honest width '
                 f'around the single line.')
    if f.get("override"):
        s.append(f'Assumptions were overridden by hand: '
                 f'{f["override"].get("notes") or "see assumptions_override.json"}.')
    s.append('These are extrapolations from reported results and published consensus, '
             'not guidance, and no management commentary has been read into them.')
    return " ".join(s)


def _trend_direction(trend: list) -> str | None:
    """Which way the daily scores moved across the window.

    sentiment.json's `trend` is a list of {date, posts, avg_compound}, one row
    per day — there is no direction in the file, so it is measured here: the
    second half of the window against the first.
    """
    days = [d for d in (trend or []) if isinstance(d, dict)
            and d.get("avg_compound") is not None]
    if len(days) < 4:
        return None
    half = len(days) // 2
    early = sum(d["avg_compound"] for d in days[:half]) / half
    late = sum(d["avg_compound"] for d in days[half:]) / (len(days) - half)
    move = late - early
    if abs(move) < 0.05:
        return "flat"
    return "improving" if move > 0 else "deteriorating"


def social_sentiment(f: dict) -> str:
    """What the scraped chatter says — and what it does not cover."""
    s = f.get("sentiment") or {}
    if not s or not s.get("post_count"):
        return ("No usable social chatter was collected — Reddit commonly blocks "
                "unauthenticated access and StockTwits returned nothing in the window, "
                "so there is no sentiment reading behind this dashboard.")
    avg = s.get("avg_compound")
    parts = [f'{s["post_count"]} posts scored over the sampling window read as '
             f'{s.get("label") or (label_for(avg) if avg is not None else "unlabelled")}'
             + (f' (mean compound score {avg:+.2f})' if avg is not None else "") + '.']
    if s.get("pct_positive") is not None and s.get("pct_negative") is not None:
        parts.append(f'{pct(s["pct_positive"], 0)} of them scored positive and '
                     f'{pct(s["pct_negative"], 0)} negative.')
    # Per-source rows carry counts and a mean score, not a label — the wording
    # comes from sentiment.label_for so both agree on where "mildly" starts.
    src_bits = []
    for name, v in (f.get("sentiment_by_source") or {}).items():
        if not isinstance(v, dict) or not v.get("post_count"):
            continue
        bit = f'{name} {v["post_count"]} posts'
        if v.get("avg_compound") is not None:
            bit += f', {label_for(v["avg_compound"])}'
        if v.get("bullish") is not None or v.get("bearish") is not None:
            bit += (f' ({v.get("bullish", 0)} tagged bullish, '
                    f'{v.get("bearish", 0)} bearish)')
        src_bits.append(bit)
    if src_bits:
        parts.append("By source: " + "; ".join(src_bits) + ".")
    direction = _trend_direction(f.get("sentiment_trend"))
    if direction:
        parts.append(f'Daily scores across the window are {direction}.')
    parts.append('This is scraped retail chatter scored by a lexicon, not a survey — '
                 'X/Twitter is not covered, and a quiet ticker reads neutral for want '
                 'of posts rather than for want of opinion.')
    return " ".join(parts)


def risks(f: dict) -> list[str]:
    out = []
    if f.get("growth_vol") is not None and f["growth_vol"] >= 0.05:
        out.append(f'Growth volatility of {pct(f["growth_vol"], 1)} over eight quarters '
                   f'means a single quarter can move the whole forecast path.')
    if f.get("modeled_quarters"):
        out.append(f'{len(f["modeled_quarters"])} of {len(f["forecast_quarters"])} forecast '
                   f'quarters are model output with no analyst estimate behind them; error '
                   f'compounds with distance.')
    if f.get("ttm_capex_to_ocf") is not None and f["ttm_capex_to_ocf"] >= 0.40:
        out.append(f'Capital intensity is high — {pct(f["ttm_capex_to_ocf"], 0)} of '
                   f'operating cash flow goes to capex — so a demand shortfall lands on '
                   f'already-committed spending.')
    if f.get("net_cash") is not None and f["net_cash"] < 0:
        out.append(f'Net debt of {money(abs(f["net_cash"]))} has to be serviced whatever '
                   f'the operating result does.')
    if f.get("op_margin_bps") is not None and f["op_margin_bps"] < 0:
        out.append(f'Operating margin is already compressing ({f["op_margin_bps"]} basis '
                   f'points year on year); the model assumes that stabilises.')
    if f.get("pe") and f["pe"] > 30:
        out.append(f'At a trailing P/E of {f["pe"]:.0f}, the price already carries a growth '
                   f'expectation that the forecast has to deliver.')
    if f.get("payout_of_fcf") is not None and f["payout_of_fcf"] > 1:
        out.append(f'Capital returns are {pct(f["payout_of_fcf"], 0)} of free cash flow — '
                   f'above what the business currently generates.')
    out.append('This dashboard reads filings and quote feeds only. Management commentary, '
               'the earnings call, competitive dynamics and regulation are not in it, and '
               'any of them can matter more than the arithmetic here.')
    return out[:6]


def catalysts(f: dict) -> list[str]:
    out = []
    if f.get("next_earnings"):
        out.append(f'Next scheduled earnings date: {str(f["next_earnings"])[:10]} — the first test '
                   f'of the consensus quarters anchoring this path.')
    if f.get("consensus_quarters"):
        c = f["consensus_quarters"][0]
        out.append(f'{c["fiscal_label"]} consensus of {money(c["revenue"]["base"])} revenue '
                   f'is the near-term bar; beating it lifts the whole modeled tail.')
    if f.get("growth_delta") is not None and f["growth_delta"] <= -GROWTH_MOVE:
        out.append('A single quarter of re-accelerating revenue would break the '
                   'deceleration the model is currently extrapolating.')
    if f.get("margin_drift") and f["margin_drift"] > 0:
        out.append(f'The model already assumes {pct(f["margin_drift"], 2)} a year of margin '
                   f'expansion — evidence of it in reported operating margin would confirm '
                   f'the largest single assumption in the forecast.')
    if f.get("share_change_yoy") is not None and f["share_change_yoy"] < 0:
        out.append(f'Continued buybacks at the current {pct(abs(f["share_change_yoy"]), 1)} '
                   f'annual pace keep lifting per-share figures independent of growth.')
    if f.get("net_cash") is not None and f["net_cash"] > 0:
        out.append(f'{money(f["net_cash"])} of net cash is undeployed optionality — '
                   f'acquisition, buyback or dividend.')
    if f.get("upside") is not None:
        out.append(f'Published price targets average {eps_fmt(f["target_mean"])}; a move '
                   f'toward them would be {pct(f["upside"], 0)} from the last price.')
    return out[:6]


# --------------------------------------------------------------- assembly --
def company_narrative(ticker: str, d: Path) -> dict:
    fin, fc = load(d / "financials.json"), load(d / "forecast.json")
    if not fin or not fc:
        raise SystemExit(f"{ticker}: need financials.json and forecast.json — "
                         f"run pipeline/fetch.py {ticker} first")
    est = load(d / "estimates.json") or {}
    market = load(d / "market.json") or {}
    sentiment = load(d / "sentiment.json")
    segments = load(d / "segments.json")
    f = company_facts(fin, fc, est, market, sentiment)
    if not f.get("hist"):
        raise SystemExit(f"{ticker}: no quarters with revenue in financials.json — "
                         f"a pre-revenue filer needs the /analyze skill for now")

    sources = [{"label": f'SEC EDGAR company facts (CIK {fin.get("cik")})',
                "url": f'https://data.sec.gov/api/xbrl/companyfacts/CIK{str(fin.get("cik") or "").zfill(10)}.json'},
               {"label": f"Yahoo Finance quote & estimates ({ticker})",
                "url": f"https://finance.yahoo.com/quote/{ticker}"}]
    if segments and segments.get("source_url"):
        sources.append({"label": "Segment data", "url": segments["source_url"]})

    return {
        "method": "rules",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "headline": headline(f),
        "executive_summary": executive_summary(f),
        "quarter_recap": quarter_recap(f),
        "thesis": thesis(f),
        "segment_story": segment_story(f, segments),
        "forecast_rationale": forecast_rationale(f),
        "social_sentiment": social_sentiment(f),
        "risks": risks(f),
        "catalysts": catalysts(f),
        "sources": sources,
    }


# ------------------------------------------------------------------ funds --
def fund_narrative(symbol: str, d: Path) -> dict:
    """The fund schema: what it costs, what is in it, how concentrated it is."""
    fu = load(d / "fund.json")
    if not fu:
        raise SystemExit(f"{symbol}: no fund.json — run pipeline/fetch.py {symbol} first")
    sentiment = load(d / "sentiment.json")

    er, cat_er = fu.get("expense_ratio"), fu.get("category_expense_ratio")
    holdings = fu.get("top_holdings") or []
    covered = sum(h.get("weight") or 0 for h in holdings)
    top10 = holdings[:10]
    top10_w = sum(h.get("weight") or 0 for h in top10)
    sectors = sorted((fu.get("sector_weightings") or {}).items(),
                     key=lambda kv: kv[1] or 0, reverse=True)
    assets = fu.get("asset_classes") or {}
    name = fu.get("name") or symbol

    cost_100k = f"${(er or 0) * 100000:,.0f}" if er is not None else "—"
    if er is not None and cat_er:
        delta = (cat_er - er) * 100000
        vs_cat = (f'That is {money(abs(delta), 0)} a year cheaper than the '
                  f'{pct(cat_er, 2)} category average on the same $100,000'
                  if delta > 0 else
                  f'That is {money(abs(delta), 0)} a year dearer than the '
                  f'{pct(cat_er, 2)} category average on the same $100,000')
    else:
        vs_cat = "No category average was available to compare it against"

    headline_txt = (f'{name} costs {pct(er, 2)} a year'
                    if er is not None else f'{name} — expense ratio not disclosed by the feed')
    if er is not None and cat_er:
        headline_txt += (' — cheaper than its category'
                         if er < cat_er else ' — dearer than its category')
    headline_txt += (f' and holds {pct(top10_w, 0)} of assets in its ten largest positions.'
                     if top10_w else '.')

    summary = [f'{name} is a {fu.get("category") or "fund"}'
               + (f' from {fu["family"]}' if fu.get("family") else "")
               + (f', {money(fu["total_assets"])} in assets' if fu.get("total_assets") else "")
               + "."]
    if er is not None:
        summary.append(f'It charges {pct(er, 2)}, which is {cost_100k} a year on $100,000.')
    perf = [(lbl, fu.get(k)) for lbl, k in
            (("year to date", "ytd_return"), ("three-year", "return_3y"),
             ("five-year", "return_5y")) if fu.get(k) is not None]
    if perf:
        summary.append("Returns: " + ", ".join(f'{lbl} {pct(v, 1)}' for lbl, v in perf) + ".")
    if fu.get("beta_3y") is not None:
        summary.append(f'Three-year beta is {fu["beta_3y"]:.2f}.')
    if top10_w:
        summary.append(f'The ten largest disclosed positions are {pct(top10_w, 0)} of the fund.')

    bull, bear = [], []
    if er is not None and cat_er and er < cat_er:
        bull.append(f'At {pct(er, 2)} it undercuts the {pct(cat_er, 2)} category average — '
                    f'{money(abs((cat_er - er) * 100000), 0)} a year kept on every $100,000.')
    if er is not None and er <= 0.001:
        bull.append(f'{pct(er, 2)} is close to the floor for the wrapper; cost is not the '
                    f'thing that will decide this holding.')
    if er is not None and er >= 0.005:
        bear.append(f'{pct(er, 2)} is {cost_100k} a year on $100,000, compounding against '
                    f'you whatever the market does.')
    if er is not None and cat_er and er > cat_er:
        bear.append(f'It costs more than the {pct(cat_er, 2)} category average, so it has '
                    f'to out-earn its peers before it breaks even against them.')
    if top10_w and top10_w >= 0.40:
        bear.append(f'{pct(top10_w, 0)} of the fund sits in ten names — this is a '
                    f'concentrated bet however broad the label sounds.')
    if top10_w and top10_w < 0.20:
        bull.append(f'The ten largest positions are only {pct(top10_w, 0)} of assets, so no '
                    f'single holding drives the result.')
    if sectors and sectors[0][1] and sectors[0][1] >= 0.30:
        bear.append(f'{sectors[0][0].replace("_", " ").title()} is {pct(sectors[0][1], 0)} of '
                    f'the portfolio — the sector call is most of the fund.')
    if assets.get("bondPosition") and assets["bondPosition"] > 0.5:
        bull.append(f'{pct(assets["bondPosition"], 0)} in bonds makes this a ballast '
                    f'holding rather than a growth one.')
    if fu.get("yield"):
        bull.append(f'It distributes a {pct(fu["yield"], 2)} yield.')
    if not bull:
        bull.append('Nothing in the fetched cost, holdings or return data crosses this '
                    'model\'s thresholds for a positive point.')
    if not bear:
        bear.append('Nothing in the fetched cost, holdings or return data crosses this '
                    'model\'s thresholds for a negative point.')

    concentration = []
    if covered:
        concentration.append(f'The feed discloses {len(holdings)} positions covering '
                             f'{pct(covered, 0)} of the fund; the rest is not visible here.')
    if top10:
        names = ", ".join(f'{h["symbol"]} {pct(h.get("weight"), 1)}' for h in top10[:5]
                          if h.get("symbol"))
        concentration.append(f'Largest disclosed holdings: {names}.')
    if sectors[:3]:
        concentration.append("Sector mix: " + ", ".join(
            f'{k.replace("_", " ")} {pct(v, 0)}' for k, v in sectors[:3] if v) + ".")
    concentration.append('If you also hold a broad index fund or these companies directly, '
                         'the overlap is real exposure counted twice — this dashboard cannot '
                         'see your other accounts.')

    fund_risks = ['Holdings, sector mix and returns come from a single quote feed and are '
                  'as stale as that feed is.']
    if covered and covered < 0.5:
        fund_risks.append(f'Only {pct(covered, 0)} of the fund is disclosed here, so the '
                          f'concentration reading is a floor, not a measurement.')
    if fu.get("beta_3y") is not None and fu["beta_3y"] > 1.1:
        fund_risks.append(f'A three-year beta of {fu["beta_3y"]:.2f} means it has moved more '
                          f'than the market, in both directions.')
    if sectors and sectors[0][1] and sectors[0][1] >= 0.25:
        fund_risks.append(f'Concentration in {sectors[0][0].replace("_", " ")} '
                          f'({pct(sectors[0][1], 0)}) is the dominant risk in the portfolio.')
    fund_risks.append('Past returns are the only performance evidence here and they are not '
                      'a forecast.')

    fund_catalysts = ['A fee cut, or a cheaper index equivalent launching in the same '
                      'category, changes the cost verdict directly.']
    if fu.get("category"):
        fund_catalysts.append(f'Anything that moves the {fu["category"]} category as a whole '
                              f'moves this fund with it — the wrapper adds no defence.')
    if sectors and sectors[0][1]:
        fund_catalysts.append(f'A rotation out of '
                              f'{sectors[0][0].replace("_", " ")} would hit '
                              f'{pct(sectors[0][1], 0)} of the portfolio at once.')

    what_it_is = (f'{name} is a {fu.get("legal_type") or fu.get("quote_type") or "fund"} in '
                  f'the {fu.get("category") or "unclassified"} category'
                  + (f', run by {fu["family"]}' if fu.get("family") else "") + '. '
                  + 'This dashboard reads the published profile, cost and disclosed '
                    'holdings; it does not read the prospectus, so the index rule and any '
                    'active discretion behind it are not verified here.')

    return {
        "method": "rules",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "headline": headline_txt,
        "executive_summary": " ".join(summary),
        "what_it_is": what_it_is,
        "cost_verdict": (f'{pct(er, 2)} a year — {cost_100k} on every $100,000. {vs_cat}.'
                         if er is not None else
                         'The feed did not disclose an expense ratio, so the cost of this '
                         'wrapper cannot be judged from the data fetched.'),
        "thesis": {"bull": bull[:5], "bear": bear[:5]},
        "concentration": " ".join(concentration),
        "risks": fund_risks[:6],
        "catalysts": fund_catalysts[:6],
        "social_sentiment": social_sentiment({"sentiment": (sentiment or {}).get("summary") or {},
                                              "sentiment_by_source": (sentiment or {}).get("by_source") or {},
                                              "sentiment_trend": (sentiment or {}).get("trend") or {}}),
        "sources": [{"label": f"Yahoo Finance fund profile ({symbol})",
                     "url": f"https://finance.yahoo.com/quote/{symbol}"}],
    }


def run(symbol: str, force: bool = False) -> dict | None:
    symbol = symbol.upper()
    d = ROOT / "data" / symbol
    out = d / "narrative.json"
    if out.exists() and not force:
        existing = load(out) or {}
        if existing.get("method") != "rules":
            # A researched narrative is worth more than a computed one, so it is
            # never clobbered. But it was written against the previous numbers,
            # and the dashboard is about to pair it with new ones.
            print(f"[narrate] {symbol}: narrative.json was written by hand or by "
                  f"/analyze, not by this module — left alone, so the dashboard will "
                  f"pair that older story with the numbers just fetched. Re-run "
                  f"/analyze {symbol} to refresh it, or --force to replace it with a "
                  f"rule-based one.")
            return None
    is_fund = (d / "fund.json").exists() and not (d / "financials.json").exists()
    n = fund_narrative(symbol, d) if is_fund else company_narrative(symbol, d)
    out.write_text(json.dumps(n, indent=1), encoding="utf-8")
    print(f'[narrate] wrote data/{symbol}/narrative.json '
          f'({"fund" if is_fund else "company"}, rule-based) — {n["headline"]}')
    return n


# --------------------------------------------------------------- selftest --
def selftest() -> int:
    """Synthetic data only — no network, no files from data/."""
    def q(fy, fq, rev, ni, end, **kw):
        row = {"fiscal_year": fy, "fiscal_quarter": fq, "end_date": end,
               "fiscal_label": f"FY{str(fy)[2:]} Q{fq}", "calendar_label": f"CQ{fq}",
               "revenue": rev, "net_income": ni, "gross_profit": rev * 0.6,
               "operating_income": rev * 0.3, "eps_diluted": round(ni / 1e9, 2),
               "shares_diluted": 1e9 - (fy - 2024) * 1e7,
               "operating_cash_flow": ni * 1.3, "capex": ni * 0.3,
               "cash": 5e10, "short_term_investments": 2e10, "long_term_debt": 1e10,
               "dividends_paid": ni * 0.2, "buybacks": ni * 0.3}
        row.update(kw)
        row["fcf"] = row["operating_cash_flow"] - row["capex"]
        row["net_cash"] = row["cash"] + row["short_term_investments"] - row["long_term_debt"]
        row["total_liquidity"] = row["cash"] + row["short_term_investments"]
        return row

    quarters = []
    rev = 20e9
    for fy in (2024, 2025, 2026):
        for fq in (1, 2, 3, 4):
            quarters.append(q(fy, fq, rev, rev * 0.25, f"{fy}-{fq * 3:02d}-28"))
            rev *= 1.04
    fin = {"ticker": "TEST", "company": "Test Corp", "cik": 1234, "quarters": quarters}
    fc = {"assumptions": {"revenue_cagr_3y": 0.17, "long_run_growth": 0.08,
                          "growth_by_fy": {"2027": 0.14, "2028": 0.11},
                          "margin_drift_per_year": 0.004, "growth_volatility_8q": 0.03,
                          "net_margin_recent_avg": 0.25},
          "quarters": [{"fiscal_label": f"FY27 Q{i}", "source": "consensus" if i < 3 else "model",
                        "revenue": {"base": 30e9 + i * 1e9}} for i in range(1, 13)]}
    est = {"earnings_history": [{"surprise_percent": 4.2, "eps_estimate": 3.1}],
           "price_targets": {"mean": 520.0},
           # Yahoo hands back a full timestamp; the prose wants a date.
           "next_earnings_dates": ["2026-10-28T16:00:00-04:00"]}
    market = {"stats": {"price": 450.0, "trailing_pe": 34.0}, "profile": {"sector": "Technology"}}
    # Shaped exactly as pipeline/sentiment.py writes it: avg_compound (not
    # mean_compound), per-source rows with no label of their own, and trend as a
    # list of daily rows. Inventing this fixture is how the real shapes got
    # missed the first time.
    senti = {"summary": {"post_count": 120, "avg_compound": 0.21, "pct_positive": 0.55,
                         "pct_neutral": 0.3, "pct_negative": 0.15,
                         "label": "mildly positive"},
             "by_source": {"stocktwits": {"post_count": 120, "avg_compound": 0.21,
                                          "bullish": 40, "bearish": 9}},
             "trend": [{"date": "2026-08-2%d" % i, "posts": 20,
                        "avg_compound": 0.05 * i} for i in range(1, 7)]}

    f = company_facts(fin, fc, est, market, senti)
    assert f["rev_yoy"] is not None and 0.16 < f["rev_yoy"] < 0.18, f["rev_yoy"]
    assert f["ttm_revenue"] and f["ttm_net_margin"] and 0.24 < f["ttm_net_margin"] < 0.26
    assert f["net_cash"] == 6e10, f["net_cash"]
    assert len(f["consensus_quarters"]) == 2 and len(f["modeled_quarters"]) == 10
    assert f["share_change_yoy"] is not None and f["share_change_yoy"] < 0

    n = {"headline": headline(f), "executive_summary": executive_summary(f),
         "quarter_recap": quarter_recap(f), "thesis": thesis(f),
         "forecast_rationale": forecast_rationale(f), "social_sentiment": social_sentiment(f),
         "risks": risks(f), "catalysts": catalysts(f)}
    for key in ("headline", "executive_summary", "quarter_recap", "forecast_rationale",
                "social_sentiment"):
        assert n[key] and len(n[key]) > 40, (key, n[key])
        # A formatter that silently produced "—" would read as a fact.
        assert "None" not in n[key], (key, n[key])
    assert n["thesis"]["bull"] and n["thesis"]["bear"]
    assert 1 <= len(n["risks"]) <= 6 and 1 <= len(n["catalysts"]) <= 6
    assert "consensus" in n["forecast_rationale"]
    # A move of zero is not a finding: the prose says "flat", never "0 basis points".
    assert "0 basis points" not in n["executive_summary"], n["executive_summary"]
    assert "flat year on year" in n["executive_summary"], n["executive_summary"]
    assert "0 basis points" not in n["quarter_recap"], n["quarter_recap"]
    # A real spread earns the width sentence; a zero one does not get "varied by 0.0%".
    assert "standard deviation" in n["forecast_rationale"]
    flat = dict(f, growth_vol=0.0)
    assert "standard deviation" not in forecast_rationale(flat), forecast_rationale(flat)
    # Dates are dates, and a share price keeps its cents.
    assert "2026-10-28 " in n["catalysts"][0] and "T16:00" not in n["catalysts"][0], n["catalysts"][0]
    assert any("$520.00" in c for c in n["catalysts"]), n["catalysts"]
    assert any("net cash" in b or "buyback" in b or "share count" in b
               for b in n["thesis"]["bull"]), n["thesis"]["bull"]

    # Empty inputs must produce no sentence rather than a broken one.
    empty = company_facts({"quarters": []}, {}, {}, {}, None)
    assert not empty.get("hist") and quarter_recap(empty) == ""
    assert "No usable social chatter" in social_sentiment({"sentiment": {}})

    # The sentiment section reads three differently-shaped fields; each one was
    # wrong at first in a way that crashed or lied.
    soc = n["social_sentiment"]
    assert "mildly positive" in soc and "+0.21" in soc, soc
    assert "55% of them scored positive" in soc, soc
    assert "stocktwits 120 posts, mildly positive" in soc, soc
    assert "40 tagged bullish, 9 bearish" in soc, soc
    assert "improving" in soc, soc
    # trend is a list of daily rows, and a short or flat one earns no sentence
    assert _trend_direction([]) is None
    assert _trend_direction([{"date": "d", "avg_compound": 0.1}]) is None
    assert _trend_direction([{"date": str(i), "avg_compound": 0.1}
                             for i in range(6)]) == "flat"
    assert _trend_direction([{"date": str(i), "avg_compound": -0.05 * i}
                             for i in range(6)]) == "deteriorating"
    # A source row carries no label of its own; the wording comes from the scorer.
    only_counts = social_sentiment({"sentiment": {"post_count": 5, "avg_compound": -0.4},
                                    "sentiment_by_source": {"reddit": {"post_count": 5}},
                                    "sentiment_trend": []})
    assert "strongly negative" in only_counts and "reddit 5 posts." in only_counts, only_counts

    # Segment stories only exist when segment data does.
    segs = {"segments": [{"name": "Cloud", "quarters": {"FY26 Q3": 10e9, "FY26 Q4": 12e9}}]}
    story = segment_story(f, segs)
    assert len(story) == 1 and "20.0%" in story[0]["story"], story
    assert segment_story(f, None) == []

    # Fund path, synthetic.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "fund.json").write_text(json.dumps({
            "symbol": "TFND", "name": "Test Index Fund", "category": "Large Blend",
            "family": "Testrock", "total_assets": 5e11, "expense_ratio": 0.0003,
            "category_expense_ratio": 0.0075, "ytd_return": 0.12, "return_3y": 0.14,
            "beta_3y": 1.02, "yield": 0.013,
            "top_holdings": [{"symbol": "AAA", "name": "A", "weight": 0.07},
                             {"symbol": "BBB", "name": "B", "weight": 0.06}],
            "sector_weightings": {"technology": 0.34, "financial_services": 0.13},
            "asset_classes": {"stockPosition": 0.99, "bondPosition": 0.0},
        }), encoding="utf-8")
        fn = fund_narrative("TFND", d)
        assert fn["method"] == "rules" and "0.03%" in fn["cost_verdict"], fn["cost_verdict"]
        assert any("category average" in b for b in fn["thesis"]["bull"]), fn["thesis"]
        assert any("Technology is 34%" in b for b in fn["thesis"]["bear"]), fn["thesis"]["bear"]
        assert "13.0%" not in fn["headline"]  # headline is cost + concentration, not yield
        assert fn["risks"] and fn["catalysts"] and fn["concentration"]

    print("narrate self-test OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", help="tickers or fund symbols")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a narrative.json this module did not write")
    ap.add_argument("--stdout", action="store_true", help="print the JSON instead of writing")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.symbols:
        ap.error("give at least one symbol, or --selftest")
    failed = 0
    for symbol in args.symbols:
        try:
            if args.stdout:
                d = ROOT / "data" / symbol.upper()
                is_fund = (d / "fund.json").exists() and not (d / "financials.json").exists()
                n = fund_narrative(symbol.upper(), d) if is_fund else company_narrative(symbol.upper(), d)
                print(json.dumps(n, indent=1))
            else:
                run(symbol, force=args.force)
        except SystemExit as e:
            print(f"! {e}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
