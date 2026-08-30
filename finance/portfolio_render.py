"""Render the combined portfolio dashboard from portfolio.json."""

from __future__ import annotations

import importlib.util
import re
import sys
from collections import defaultdict
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
from common import ROOT, diag

_spec = importlib.util.spec_from_file_location("pipeline_render",
                                               ROOT / "pipeline" / "render.py")
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)
money, pct, nice_ticks, Frame = pr.money, pr.pct, pr.nice_ticks, pr.Frame
rounded_bar = pr.rounded_bar
S1, S2, S3, S4 = pr.S1, pr.S2, pr.S3, pr.S4


def exact_money(value, signed: bool = False) -> str:
    """Currency for ledger amounts where cents are meaningful."""
    if value is None:
        return "—"
    value = float(value)
    sign = "+" if signed and value > 0 else ("−" if value < 0 else "")
    return f"{sign}${abs(value):,.2f}"


# Sector labels are the widest text in the holdings table and push the other
# columns off screen — abbreviate them there, keeping the full name on hover.
# Covers the Yahoo taxonomy the analyzer stores plus the GICS names funds use.
SECTOR_ABBR = {
    "basic materials": "Materials",
    "communication services": "Comm Svcs",
    "consumer cyclical": "Cons Cyc",
    "consumer discretionary": "Cons Disc",
    "consumer defensive": "Cons Def",
    "consumer staples": "Cons Stpl",
    "energy": "Energy",
    "financial services": "Financials",
    "financials": "Financials",
    "health care": "Health",
    "healthcare": "Health",
    "industrials": "Industrials",
    "information technology": "Info Tech",
    "materials": "Materials",
    "real estate": "Real Est",
    "technology": "Tech",
    "telecommunication services": "Telecom",
    "utilities": "Utilities",
}


# The fund table's Morningstar categories run even longer than sector names;
# shorten them word by word so unseen categories still come out readable.
CATEGORY_WORDS = {
    "limited partnership": "LP",
    "high yield": "HY",
    "allocation": "Alloc",
    "conservative": "Consv",
    "corporate": "Corp",
    "diversified": "Divers",
    "government": "Govt",
    "intermediate": "Interm",
    "international": "Intl",
    "moderate": "Mod",
    "municipal": "Muni",
    "preferred": "Pref",
    "technology": "Tech",
}


def abbr_sector(name):
    """Short form of a sector name for the holdings table."""
    if not name:
        return "—"
    return SECTOR_ABBR.get(name.strip().lower(), name.strip()[:12])


def abbr_category(name):
    """Short form of a fund's category for the fund table."""
    if not name:
        return "—"
    out = name.strip()
    for long, short in CATEGORY_WORDS.items():
        out = re.sub(rf"\b{long}\b", short, out, flags=re.I)
    return out[:18]


def weight_chart(rows, chart_id, title, subtitle, vbw=960):
    """Columns of position value, tallest first."""
    vals = [r["value"] for r in rows]
    ticks = nice_ticks(0, max(vals) * 1.08)
    fr = Frame(vbw=vbw, y_lo=0, y_hi=ticks[-1], n_slots=len(rows),
               y_fmt=lambda v: money(v, 0), bottom=44)
    fr.grid(ticks)
    bw = min(26.0, fr.step * 0.62)
    for i, r in enumerate(rows):
        fr.svg.add(rounded_bar(fr.x(i) - bw / 2, fr.y(r["value"]), bw, fr.y(0), S1, i))
        fr.slots.append({"label": f'{r["ticker"]} · {r.get("company", "")[:30]}',
                         "items": [["value", money(r["value"], 2), "var(--s1)"],
                                   ["weight", pct(r["weight"], 1), ""],
                                   ["fwd P/E", f'{r["forward_pe"]:.1f}'
                                    if r.get("forward_pe") else "—", ""]]})
    fr.x_labels([r["ticker"] for r in rows], every=1)
    fr.crosshair()
    return {"id": chart_id, "title": title, "subtitle": subtitle, "legend": None,
            "svg": fr.svg.render(), "data_json": fr.data_json(),
            "table": {"head": ["Ticker", "Company", "Value", "Weight"],
                      "rows": [[r["ticker"], r.get("company", "")[:38],
                                money(r["value"], 2), pct(r["weight"], 1)]
                               for r in rows]}}


def fee_vs_return_chart(periods, label, chart_id, vbw=960):
    """Per billing period: what the account made, and what it was billed.

    Both series are dollars on one axis — the fee columns are meant to look
    small next to a good quarter and impossible to miss next to a bad one,
    which is the whole comparison. Fees are drawn below the baseline because
    that is the direction they move the balance; a second axis for them would
    be a lie about their size.
    """
    usable = [p for p in periods if p.get("total_return") is not None]
    if len(usable) < 2:
        return None
    vals = [p["total_return"] for p in usable] + [-p["fee"] for p in usable]
    lo, hi = min(vals + [0]), max(vals + [0])
    pad = (hi - lo) * 0.12 or 1
    ticks = nice_ticks(lo - pad, hi + pad)
    fr = Frame(vbw=vbw, y_lo=ticks[0], y_hi=ticks[-1], n_slots=len(usable),
               y_fmt=lambda v: money(v, 0), bottom=44)
    fr.grid(ticks)

    # paired columns, 2px apart, so period and its fee read as one unit
    bw = min(30.0, fr.step * 0.30)
    for i, p in enumerate(usable):
        ret, fee = p["total_return"], p["fee"]
        x_ret = fr.x(i) - bw - 1
        fr.svg.add(rounded_bar(x_ret, fr.y(ret), bw, fr.y(0),
                               S3 if ret >= 0 else S2, i))
        fr.svg.add(rounded_bar(fr.x(i) + 1, fr.y(-fee), bw, fr.y(0), S4, i))
        # A losing quarter must read as a loss everywhere it is described.
        # "account made -$68K" invites the eye to see a number next to a fee
        # and call it a gain; naming the loss and what the fee did to it does
        # not.
        if ret < 0:
            items = [["account lost", money(abs(ret), 2), "var(--s2)"],
                     ["advisory fee", money(-fee, 2), "var(--s4)"],
                     ["fee deepened the loss by", pct(abs(p["fee_impact"]), 1)
                      if p.get("fee_impact") else "—", ""],
                     ["down after the fee", money(p["net"], 2), ""]]
        else:
            items = [["account made", money(ret, 2), "var(--s3)"],
                     ["advisory fee", money(-fee, 2), "var(--s4)"],
                     ["fee share of gain", pct(p["fee_share"], 1)
                      if p.get("fee_share") else "—", ""],
                     ["kept", money(p["net"], 2), ""]]
        if p.get("fee_vs_value") is not None:
            items.append(["fee vs account value", pct(p["fee_vs_value"], 2, True), ""])
        if p.get("income"):
            items.insert(1, ["of which income", money(p["income"], 2), ""])
        fr.slots.append({"label": f'{p["label"]} · billed {p["end"]}', "items": items})

    # the fee is the same size every quarter whatever happened; a direct label
    # on the worst period is what makes that land
    worst = max(range(len(usable)),
                key=lambda i: (usable[i]["fee"] / usable[i]["total_return"])
                if usable[i]["total_return"] > 0 else float("inf"))
    wp = usable[worst]
    fr.svg.text(fr.x(worst), fr.y(max(wp["total_return"], 0)) - 10,
                (f'fee = {pct(wp["fee_share"], 0)} of the gain'
                 if wp.get("fee_share")
                 else f'{money(wp["fee"], 2)} billed on a '
                      f'{money(abs(wp["total_return"]), 2)} loss'),
                11.5, "var(--ink2)" if wp.get("fee_share") else "var(--s2)",
                weight=600)

    fr.x_labels([p["label"] for p in usable], every=1)
    fr.crosshair()
    total_ret = sum(p["total_return"] for p in usable)
    total_fee = sum(p["fee"] for p in usable)
    return {
        "id": chart_id,
        "title": f"What {label} made, and what it was billed",
        "subtitle": (
            f'Each billing period: the account\'s gain or loss against the '
            f'advisory fee charged at the end of it. Over these '
            f'{len(usable)} periods the account produced {money(total_ret, 2)} '
            f'and was billed {money(total_fee, 2)}. Valued at today\'s share '
            f'counts, so this is market movement, not money paid in or out — '
            f'and a rising market pays the fee too, so this compares the two, '
            f'it does not credit either to the advisor.'),
        "legend": [{"kind": "swatch", "color": "var(--s3)", "label": "period gain"},
                   {"kind": "swatch", "color": "var(--s2)", "label": "period loss"},
                   {"kind": "swatch", "color": "var(--s4)", "label": "advisory fee"}],
        "svg": fr.svg.render(), "data_json": fr.data_json(),
        "table": {"head": ["Period", "From", "To", "Market gain/loss", "Income",
                           "Total", "Fee", "Fee % of gain", "Kept"],
                  "rows": [[p["label"], p["start"], p["end"],
                            money(p["market_gain"], 2),
                            (money(p["income"], 2) + ("" if p["income_complete"]
                                                      else " (partial)"))
                            if p.get("income") else "—",
                            money(p["total_return"], 2), money(p["fee"], 2),
                            pct(p["fee_share"], 1) if p.get("fee_share") else "—",
                            money(p["net"], 2)] for p in usable]},
    }


def fee_share_chart(periods, label, chart_id, vbw=960):
    """The fee against what each quarter actually did, as a signed proportion.

    The dollar chart is honest about size and therefore says almost nothing
    about a good quarter — a $5k fee beside a $170k gain is a sliver. This is
    the same data as a proportion, which is where the answer lives.

    One signed measure covers both directions. Above the line, the share of a
    gain handed over. Below it, a losing quarter: there is no gain to take a
    share of, so the fee is measured against the loss it was added to — the
    percentage by which being billed made a bad quarter worse. Leaving that
    period blank, or drawing it as if nothing happened, would hide the only
    case where the fee is unambiguously working against you.
    """
    usable = [p for p in periods if p.get("total_return") is not None]
    if len(usable) < 2:
        return None
    vals = [p.get("fee_impact") or 0 for p in usable]
    ticks = nice_ticks(min(min(vals) * 1.35, 0), max(max(vals) * 1.15, 0.05))
    fr = Frame(vbw=vbw, y_lo=ticks[0], y_hi=ticks[-1], n_slots=len(usable),
               y_fmt=lambda v: pct(v, 0), bottom=44)
    fr.grid(ticks)
    bw = min(34.0, fr.step * 0.42)
    for i, p in enumerate(usable):
        s, loss = p.get("fee_impact"), p.get("loss")
        if s is None:                       # a flat quarter: no ratio exists
            fr.svg.text(fr.x(i), fr.y(0) - 12, "no change to measure against",
                        11.5, "var(--muted)")
        else:
            y = fr.y(max(min(s, ticks[-1]), ticks[0]))
            fr.svg.add(rounded_bar(fr.x(i) - bw / 2, y, bw, fr.y(0), S4, i))
            fr.svg.text(fr.x(i), y + (16 if loss else -8), pct(s, 0), 11.5,
                        "var(--s2)" if loss else "var(--ink2)", weight=600)
            if loss:
                fr.svg.text(fr.x(i), y + 30, "deeper loss", 11,
                            "var(--s2)", weight=600)
        made = ([f'account lost', money(abs(p["total_return"]), 2), "var(--s2)"]
                if loss else
                ["account made", money(p["total_return"], 2), "var(--s3)"])
        fr.slots.append({
            "label": f'{p["label"]} · billed {p["end"]}',
            "items": [["advisory fee", money(-p["fee"], 2), "var(--s4)"], made,
                      (["fee deepened the loss by", pct(abs(s), 1), ""] if loss
                       else ["fee share of gain", pct(s, 1) if s else "—", ""]),
                      ["fee vs account value",
                       pct(p.get("fee_vs_value"), 2, True)
                       if p.get("fee_vs_value") is not None else "—", ""],
                      ["the quarter after the fee", money(p["net"], 2), ""]]})
    fr.x_labels([p["label"] for p in usable], every=1)
    fr.crosshair()
    losses = [p for p in usable if p.get("loss")]
    return {
        "id": chart_id,
        "title": "What each quarter's fee cost, against what the quarter did",
        "subtitle": (
            f'The same billing periods for {label} as a proportion. Above the '
            f'line: the share of the quarter\'s gain the fee took — small when '
            f'the market ran, large when it did not. Below the line: a quarter '
            f'that lost money and was billed anyway, shown as how much deeper '
            f'the fee made the loss. '
            + (f'{len(losses)} of {len(usable)} periods here fall below the '
               f'line, together '
               f'{money(sum(p["fee"] for p in losses), 2)} charged on '
               f'{money(abs(sum(p["total_return"] for p in losses)), 2)} of '
               f'losses, {pct(abs(sum(p["fee_vs_value"] or 0 for p in losses)), 2)} '
               f'taken out of the balance in quarters it was already down.'
               if losses else
               'No losing quarter in this window — the fee has not yet been '
               'tested against one.')),
        "legend": [{"kind": "swatch", "color": "var(--s4)",
                    "label": "advisory fee vs the quarter's result"}],
        "svg": fr.svg.render(), "data_json": fr.data_json(),
        "table": {"head": ["Period", "Result", "Fee", "Fee vs result",
                           "Fee vs account value", "After the fee"],
                  "rows": [[p["label"],
                            (f'−{money(abs(p["total_return"]), 2)} loss'
                             if p.get("loss")
                             else f'{money(p["total_return"], 2)} gain'),
                            money(p["fee"], 2),
                            (f'{pct(abs(p["fee_impact"]), 1)} deeper loss'
                             if p.get("loss") else
                             pct(p["fee_impact"], 1) if p.get("fee_impact")
                             else "—"),
                            pct(p.get("fee_vs_value"), 2, True)
                            if p.get("fee_vs_value") is not None else "—",
                            money(p["net"], 2)] for p in usable]},
    }


def mix_bar(groups, total):
    out = []
    for name, value, color in groups:
        if value > 0:
            out.append({"name": name, "value": money(value, 2), "color": color,
                        "w": round(value / total * 100, 1),
                        "pct": pct(value / total, 1)})
    return out


def risk_matched_context(p: dict) -> dict | None:
    """The same-shape benchmark, formatted — or why it could not be built.

    Reported separately from the headline index rather than replacing it: one
    asks whether indexing would have been better, the other whether the advice
    beat a portfolio taking the same risk, and the answers can disagree.
    """
    if not p.get("blend"):
        return None
    mix = p.get("blend_mix") or {}
    r, ex = p.get("blend_return"), p.get("excess_net_blend")
    return {
        "label": p.get("blend_label"),
        "missing": ", ".join(p.get("blend_missing") or []) or None,
        "bench": pct(r, 1, True) if r is not None else None,
        "excess_net": pct(ex, 1, True) if ex is not None else None,
        "excess_gross": pct(p["excess_gross_blend"], 1, True)
        if p.get("excess_gross_blend") is not None else None,
        "ahead": (ex > 0) if ex is not None else None,
        "dir": ("up" if ex > 0 else "down") if ex is not None else "",
        "dollars": money(abs(p["blend_dollars"]), 2)
        if p.get("blend_dollars") is not None else None,
        "equity": pct(mix.get("stocks"), 0) if mix.get("stocks") else None,
        "bonds": pct((mix.get("bonds") or 0) + (mix.get("preferred") or 0)
                     + (mix.get("convertible") or 0), 0),
        # a sliver of cash that rounds to 0% is noise, not a sleeve worth naming
        "cash": pct(mix.get("cash"), 0) if (mix.get("cash") or 0) >= 0.005 else None,
        "unclassified": pct(mix.get("unclassified_share"), 0)
        if (mix.get("unclassified_share") or 0) >= 0.005 else None,
    }


def performance_context(p: dict | None, label: str | None = None) -> dict | None:
    """Format one return record — an account's, or all the advised money's.

    Annualising is withheld under a year of history: turning six months into an
    annual rate multiplies whatever happened in those six months, and a fee
    argument does not need that kind of help.
    """
    if not p:
        return None
    years = p.get("years") or 0
    net, gross, bench = p["net_return"], p["gross_return"], p.get("benchmark_return")
    v = p.get("verdict") or {}
    verdict = {
        "answer": v.get("answer", "unknown"),
        "yes": v.get("answer") == "yes",
        "known": v.get("answer") in ("yes", "no"),
        "because": v.get("because"),
        # the gap in money, stated without a sign: "ahead by"/"behind by" reads
        # better than a signed number people have to interpret
        "dollars": money(abs(v["dollars"]), 2) if v.get("dollars") is not None else None,
        "gap": pct(abs(v["excess_net"]), 1) if v.get("excess_net") is not None else None,
        "fee_flipped": v.get("fee_flipped"),
        "conclusive": v.get("conclusive"),
        "years_needed": (f"{v['years_needed']:,.0f}"
                         if v.get("years_needed") else None),
    } if v else None
    return {
        "verdict": verdict,
        "label": label,
        "start": p["start"], "end": p["end"], "years": f"{years:.1f}",
        "long_enough": years >= 1,
        "start_value": money(p["start_value"], 2),
        "end_value": money(p["end_value"], 2),
        "net": pct(net, 1, True), "gross": pct(gross, 1, True),
        # tiles colour with .good/.bad, table cells with .up/.down — two
        # different rules in this stylesheet, so both spellings are supplied
        "net_tone": "good" if net > 0 else ("bad" if net < 0 else ""),
        "gross_tone": "good" if gross > 0 else ("bad" if gross < 0 else ""),
        "net_dir": "up" if net > 0 else ("down" if net < 0 else ""),
        "gross_dir": "up" if gross > 0 else ("down" if gross < 0 else ""),
        "net_ann": pct(p["net_annualised"], 1, True) if years >= 1 else None,
        "gross_ann": pct(p["gross_annualised"], 1, True) if years >= 1 else None,
        "fee_drag": pct(p["fee_drag"], 1),
        "fee": money(p["fee"], 2),
        "gain": money(p["market_gain"], 2),
        "income": money(p["income"], 2),
        "made": money(p["total_return_dollars"], 2),
        "kept": money(p["net_dollars"], 2),
        "benchmark": p.get("benchmark"),
        "bench": pct(bench, 1, True) if bench is not None else None,
        # the second yardstick: same asset mix, so the comparison stops
        # rewarding whoever simply held more stock
        "rm": risk_matched_context(p),
        "excess_net": pct(p["excess_net"], 1, True) if p.get("excess_net") is not None else None,
        # after the fee is the only comparison that reflects the real choice
        "ahead": (p["excess_net"] > 0) if p.get("excess_net") is not None else None,
        "fee_share": pct(p.get("fee_share_of_return"), 0)
        if p.get("fee_share_of_return") else None,
        "n_accounts": p.get("n_accounts"),
        "accounts": p.get("accounts") or [],
        "aligned": p.get("aligned", True),
        "income_complete": p.get("income_complete", True),
        "coverage": pct(p.get("coverage"), 0) if p.get("coverage") is not None else None,
        "partial": (p.get("coverage") or 1) < 0.999,
    }


def advisory_context(a: dict) -> dict | None:
    """Format the advisory-fee evaluation for the page.

    Returns a dict even when nothing is configured — the page then explains how
    to switch it on, which is more useful than silently omitting the section.
    """
    if not a.get("configured"):
        # Also the path for a portfolio.json built before this analysis existed:
        # show how to switch it on rather than leaving a silent hole.
        return {"configured": False, "config_path": a.get("config_path"),
                "sample_path": a.get("sample_path")}

    def yrs(v):
        if v is None:
            return None
        return f"{v:,.0f}" if v >= 10 else f"{v:.1f}"

    accounts = []
    for r in a["accounts"]:
        odds = r.get("odds_beat_passive") or {}
        obs = r.get("observed")
        accounts.append({
            "label": r["label"], "institution": r.get("institution"),
            "billed": r.get("billed"),
            # an account we know is advised because it was billed, not because
            # anyone typed it into the config
            "discovered": r.get("discovered"),
            "charges_from": ", ".join(r.get("charges_from") or []) or None,
            "charges_dates": ", ".join(r.get("charges_dates") or []) or None,
            "unmatched": r["matched_accounts"] == 0,
            "value": money(r["value"], 2),
            "pct": pct(r["pct_portfolio"], 1) if r["pct_portfolio"] else "—",
            "rate": pct(r["rate"], 3),
            "rate_source": r["rate_source"],
            "declared": pct(r["declared_rate"], 3),
            "measured": pct(r["measured_rate"], 3) if r["measured_rate"] else None,
            "rate_gap": pct(r["rate_gap"], 3, True) if r.get("rate_gap") else None,
            "rate_gap_dir": ("up" if (r.get("rate_gap") or 0) > 0 else "down"),
            "charges_n": r["charges_n"],
            "charges_total": money(r["charges_total"], 2) if r["charges_total"] else None,
            "charges_years": r["charges_years"],
            "run_rate": money(r["run_rate_annual"], 2) if r.get("run_rate_annual") else None,
            "run_rate_rate": pct(r["run_rate_rate"], 3) if r.get("run_rate_rate") else None,
            "fee": money(r["fee_annual"], 2) if r["fee_annual"] else "—",
            "fund_er": pct(r["fund_er"], 3) if r["fund_er"] is not None else "—",
            "all_in": pct(r["all_in"], 3),
            "breakeven": pct(r["breakeven_alpha"], 2),
            "gain": money(r["gain"], 2) if r.get("gain") is not None else None,
            "fee_share_of_gain": pct(r["fee_share_of_gain"], 1)
            if r.get("fee_share_of_gain") else None,
            "detect": yrs(r["years_to_detect"]),
            # portfolio.json round-trips through JSON, so these keys arrive as
            # strings and a plain sort puts 10y between 1y and 5y
            "odds": [{"years": int(k), "p": pct(v, 0)}
                     for k, v in sorted(odds.items(), key=lambda kv: int(kv[0]))
                     if v is not None],
            "observed": {
                "n": obs["n"], "mean": pct(obs["mean"], 2, True),
                "ci": f'{pct(obs["ci_lo"], 2, True)} to {pct(obs["ci_hi"], 2, True)}',
                "t": f'{obs["t"]:.2f}', "p": f'{obs["p"]:.2f}',
                "significant": obs["significant"],
            } if obs else None,
            "perf": performance_context(r.get("performance"), r["label"]),
        })

    dr, dh = a.get("drag_retirement") or {}, a.get("drag_horizon") or {}

    # the same figures the per-account rows carry, priced across the whole
    # advised stack — the number that answers "what does the advice cost me"
    total = {
        "value": money(a["advised_value"], 2),
        "pct": pct(a["pct_of_portfolio"], 1) if a["pct_of_portfolio"] else "—",
        "rate": pct(a["blended_rate"], 3) if a["blended_rate"] else "—",
        "fee": money(a["fee_total_annual"], 2) if a["fee_total_annual"] else "—",
        "fund_er": pct(a["weighted_fund_er"], 3)
        if a.get("weighted_fund_er") is not None else "—",
        "fund_er_coverage": pct(a["fund_er_coverage"], 0)
        if (a.get("fund_er_coverage") or 1) < 0.999 else None,
        "all_in": pct(a.get("all_in_blended"), 3),
        "breakeven": pct(a["weighted_breakeven"], 2) if a["weighted_breakeven"] else "—",
        "detect": yrs(a["years_to_detect"]),
        "odds": [{"years": int(k), "p": pct(v, 0)}
                 for k, v in sorted((a.get("odds_beat_passive") or {}).items(),
                                    key=lambda kv: int(kv[0])) if v is not None],
        "accounts_n": a.get("accounts_n") or len(accounts),
        "billed_n": a.get("accounts_billed_n") or 0,
        "charges_n": a.get("charges_n") or 0,
        "charges_total": money(a["charges_total"], 2) if a.get("charges_total") else None,
        "run_rate": money(a["run_rate_annual"], 2) if a.get("run_rate_annual") else None,
        "run_rate_rate": pct(a["run_rate_rate"], 3) if a.get("run_rate_rate") else None,
        "gain": money(a["gain"], 2) if a.get("gain") else None,
        "fee_share_of_gain": pct(a["fee_share_of_gain"], 1)
        if a.get("fee_share_of_gain") else None,
    }

    return {
        "configured": True, "accounts": accounts, "total": total,
        "perf": performance_context(a.get("performance")),
        "advised_value": money(a["advised_value"], 2),
        "pct_of_portfolio": pct(a["pct_of_portfolio"], 1)
        if a["pct_of_portfolio"] else "—",
        "fee_total": money(a["fee_total_annual"], 2),
        "blended": pct(a["blended_rate"], 3) if a["blended_rate"] else "—",
        "breakeven": pct(a["weighted_breakeven"], 2) if a["weighted_breakeven"] else "—",
        "detect": yrs(a["years_to_detect"]),
        "te": pct(a["tracking_error"], 1),
        "passive_er": pct(a["passive_er"], 3),
        "growth": pct(a["growth"], 1),
        "yrs_ret": f'{a["years_to_retirement"]:.0f}',
        "yrs_hor": f'{a["years_horizon"]:.0f}',
        "drag_ret": money(dr.get("drag"), 2) if dr else None,
        "drag_hor": money(dh.get("drag"), 2) if dh else None,
        "drag_hor_pct": pct(dh.get("drag_pct"), 0) if dh.get("drag_pct") else None,
        "gaps": a.get("data_gaps") or [],
        "charges_harvested": a.get("charges_harvested") or 0,
        "discovered_accounts": a.get("discovered_accounts") or 0,
    }


def build_context(p: dict) -> dict:
    stocks = p["stocks"]
    total = p["total_portfolio"]

    mix = mix_bar([("Individual stocks", p["stocks_total"], S1),
                   ("Funds & ETFs", p["funds_total"], S3),
                   ("Cash & money market", p["cash_total"], S4),
                   ("Other", p["other_total"], S2)], total)

    charts = {"weights": weight_chart(
        stocks, "chart-weights", "Position sizes — individual stocks",
        f'{len(stocks)} companies, {money(p["stocks_total"], 2)} of a '
        f'{money(total, 2)} portfolio.')}

    # one fee chart per advised account that has enough billing history
    fee_charts = []
    for i, acct in enumerate((p.get("advisory") or {}).get("accounts") or []):
        per = acct.get("periods") or []
        for c in (fee_vs_return_chart(per, acct["label"], f"chart-fee-{i}"),
                  fee_share_chart(per, acct["label"], f"chart-fee-share-{i}")):
            if c:
                fee_charts.append(c)

    # weighted valuation and growth, over the names that report each metric
    def weighted(key):
        num = den = 0.0
        for r in stocks:
            v = r.get(key)
            if v is not None:
                num += v * r["value"]
                den += r["value"]
        return (num / den if den else None), den

    fpe, fpe_cov = weighted("forward_pe")
    tpe, _ = weighted("trailing_pe")
    beta, beta_cov = weighted("beta")
    growth, growth_cov = weighted("forecast_2y_cagr")
    upside, upside_cov = weighted("upside")
    dy, _ = weighted("dividend_yield")

    sectors = defaultdict(float)
    for r in stocks:
        sectors[r.get("sector") or "Unclassified"] += r["value"]
    sector_rows = [{"name": k, "value": money(v, 2),
                    "pct_stocks": pct(v / p["stocks_total"], 1)
                    if p["stocks_total"] else "—",
                    "pct_total": pct(v / total, 1),
                    "w": round(v / max(sectors.values()) * 100, 1)}
                   for k, v in sorted(sectors.items(), key=lambda kv: -kv[1])]

    top5 = sum(r["value"] for r in stocks[:5])
    tiles = [
        {"label": "Total portfolio", "value": money(total, 2),
         "sub": f'{len(stocks)} stocks · {len(p["funds"])} funds · '
                f'{len(p["cash"])} cash'},
        {"label": "In individual stocks", "value": money(p["stocks_total"], 2),
         "sub": pct(p["stocks_total"] / total, 1) + " of portfolio"},
        {"label": "Top-5 concentration", "value": pct(top5 / total, 1),
         "cls": "bad" if top5 / total > 0.4 else ("warn" if top5 / total > 0.3 else ""),
         "sub": ", ".join(r["ticker"] for r in stocks[:5])},
        {"label": "Weighted forward P/E", "value": f"{fpe:.1f}" if fpe else "—",
         "sub": f'covers {pct(fpe_cov / p["stocks_total"], 0)} of stock value'},
        {"label": "Weighted 2-yr revenue CAGR",
         "value": pct(growth, 1) if growth is not None else "—",
         "sub": "from the modelled 12-quarter forecasts"},
        {"label": "Weighted analyst upside",
         "value": pct(upside, 1, True) if upside is not None else "—",
         "cls": "good" if (upside or 0) > 0 else "bad",
         "sub": "mean price target vs last close"},
    ]

    # ---- your performance against cost basis ---------------------------------
    perf = p.get("performance") or {}
    if perf.get("cost_basis"):
        cov = perf.get("covered_pct")
        tiles += [
            {"label": "Cost basis", "value": money(perf["cost_basis"], 2),
             "sub": f'{perf["positions_with_cost"]} of {perf["positions_total"]} '
                    f'positions report cost'},
            {"label": "Unrealised gain/loss", "value": money(perf["gain"], 2),
             "cls": "good" if (perf["gain"] or 0) >= 0 else "bad",
             "sub": f'on {money(perf["value"], 2)} of market value'},
            {"label": "Total return on cost",
             "value": pct(perf["gain_pct"], 1, True),
             "cls": "good" if (perf["gain_pct"] or 0) >= 0 else "bad",
             "sub": (f'covers {pct(cov, 0)} of the portfolio'
                     if cov is not None else "positions reporting cost")},
        ]

    rows = [{
        "ticker": r["ticker"], "company": (r.get("company") or "")[:34],
        "sector": abbr_sector(r.get("sector")),
        "sector_full": (r.get("sector") or "—")[:40],
        "value": money(r["value"], 2), "weight": pct(r["weight"], 1),
        "cost": money(r["cost_basis"], 2) if r.get("cost_basis") is not None else "—",
        "gain": money(r["gain"], 2) if r.get("gain") is not None else "—",
        "gain_pct": pct(r["gain_pct"], 1, True) if r.get("gain_pct") is not None else "—",
        "gain_dir": "up" if (r.get("gain") or 0) >= 0 else "down",
        # a position whose accounts do not all report cost is measured on the
        # part that does — flag it rather than showing a gain that looks whole
        "cost_partial": (pct(r["cost_coverage"], 0)
                         if r.get("cost_coverage") is not None
                         and r["cost_coverage"] < 0.999 else None),
        # a live quote, where we have one, beats the price stored with the last
        # analyzer run — that snapshot can be days old
        "price": (f'${r["live_price"]:,.2f}' if r.get("live_price")
                  else (f'${r["price"]:,.2f}' if r.get("price") else "—")),
        "price_live": bool(r.get("live_price")),
        "mcap": money(r.get("market_cap"), 1),
        "fpe": f'{r["forward_pe"]:.1f}' if r.get("forward_pe") else "—",
        "rev_yoy": pct(r["revenue_yoy"], 1, True) if r.get("revenue_yoy") is not None else "—",
        "rev_dir": "up" if (r.get("revenue_yoy") or 0) >= 0 else "down",
        "margin": pct(r["net_margin"], 1) if r.get("net_margin") is not None else "—",
        "cagr": pct(r["forecast_2y_cagr"], 1) if r.get("forecast_2y_cagr") is not None else "—",
        "upside": pct(r["upside"], 0, True) if r.get("upside") is not None else "—",
        "upside_dir": "up" if (r.get("upside") or 0) >= 0 else "down",
        "beta": f'{r["beta"]:.2f}' if r.get("beta") else "—",
        "last_q": r.get("last_quarter") or "—",
        "link": f'/dashboard/{r["ticker"]}' if r.get("has_dashboard") else None,
    } for r in stocks]

    # ---- fund sleeve: cost, look-through, true allocation ---------------------
    import funds as funds_mod
    fa = p.get("fund_analysis") or {}
    fund_rows, look_rows, alloc_rows, true_sectors, fees = [], [], [], [], None
    if fa.get("funds"):
        fund_rows = [{
            "symbol": r["symbol"], "name": (r["name"] or "")[:34],
            "category": abbr_category(r["category"]),
            "category_full": r["category"] or "—",
            "value": money(r["value"], 2), "pct": pct(r["weight"], 1),
            "cost": money(r["cost_basis"], 2) if r.get("cost_basis") is not None else "—",
            "gain": money(r["gain"], 2) if r.get("gain") is not None else "—",
            "gain_pct": pct(r["gain_pct"], 1, True) if r.get("gain_pct") is not None else "—",
            "gain_dir": "up" if (r.get("gain") or 0) >= 0 else "down",
            "er": pct(r["expense_ratio"], 3) if r["expense_ratio"] is not None else "—",
            "er_zero": r["expense_zero"],
            "cat_er": pct(r["category_expense_ratio"], 3)
            if r["category_expense_ratio"] else "—",
            "vs": pct(r["vs_category"], 3, True) if r["vs_category"] is not None else "—",
            "vs_dir": ("down" if (r["vs_category"] or 0) <= 0 else "up"),
            "fee": money(r["annual_fee"], 2) if r["annual_fee"] is not None else "—",
            "yield": pct(r["yield"], 2) if r["yield"] is not None else "—",
            "r3": pct(r["return_3y"], 1) if r["return_3y"] is not None else "—",
            "beta": f'{r["beta_3y"]:.2f}' if r["beta_3y"] is not None else "—",
            "link": (f'/dashboard/{r["symbol"]}'
                     if (ROOT / "dashboards" / f'{r["symbol"]}.html').exists() else None),
        } for r in sorted(fa["funds"], key=lambda x: -(x["annual_fee"] or 0))]

        # borrow the plan's own horizon when the finance app has produced one
        yrs_ret, yrs_hor = 6, 36
        an = common.load_json(common.FIN_DATA / "analysis.json") or {}
        pj = an.get("projection") or {}
        if pj.get("ages") and pj.get("markers"):
            yrs_ret = max(pj["markers"]["retire_age"] - pj["ages"][0], 0)
            yrs_hor = max(pj["ages"][-1] - pj["ages"][0], 0)
        fees = {
            "annual": money(fa["annual_fee_total"], 2),
            "weighted": pct(fa["weighted_expense"], 3) if fa["weighted_expense"] else "—",
            "coverage": pct(fa["fee_coverage"], 0),
            "to_retirement": money(
                funds_mod.fee_projection(fa["annual_fee_total"], yrs_ret), 2),
            "to_horizon": money(
                funds_mod.fee_projection(fa["annual_fee_total"], yrs_hor), 2),
            "yrs_ret": yrs_ret, "yrs_hor": yrs_hor,
            "missing": fa.get("missing") or [],
        }
        # sleeve performance, over the funds that actually reported a cost basis
        _wc = [r for r in fa["funds"] if r.get("cost_basis") is not None]
        if _wc:
            _c = sum(r["cost_basis"] for r in _wc)
            _v = sum(r["value"] for r in _wc)
            _all = sum(r["value"] for r in fa["funds"])
            fees.update({
                "cost": money(_c, 2), "gain": money(_v - _c, 2),
                "gain_dir": "up" if _v >= _c else "down",
                "gain_pct": pct(_v / _c - 1, 1, True) if _c else "—",
                "perf_coverage": pct(_v / _all, 0) if _all else None,
                "perf_n": len(_wc), "perf_total": len(fa["funds"]),
            })

        # true allocation folds the direct stocks and cash back in
        alloc = dict(fa["allocation"])
        alloc["stocks"] = alloc.get("stocks", 0) + p["stocks_total"]
        alloc["cash"] = alloc.get("cash", 0) + p["cash_total"]
        colors = {"stocks": S1, "bonds": S3, "cash": S4, "preferred": S2,
                  "convertible": "var(--muted)", "other": "var(--grid)",
                  "unclassified": "var(--grid)"}
        alloc_rows = [{"name": k.replace("_", " ").title(), "value": money(v, 2),
                       "pct": pct(v / total, 1), "w": round(v / total * 100, 1),
                       "color": colors.get(k, "var(--grid)")}
                      for k, v in sorted(alloc.items(), key=lambda kv: -kv[1]) if v > 0]

        look_rows = [{
            "symbol": e["symbol"], "name": (e["name"] or "")[:30],
            "direct": money(e["direct"], 2) if e["direct"] else "—",
            "via": money(e["via_funds"], 2) if e["via_funds"] else "—",
            "total": money(e["total"], 2), "pct": pct(e["pct_portfolio"], 2),
            "sources": ", ".join(s["fund"] for s in e["sources"]) or "—",
            "both": bool(e["direct"] and e["via_funds"]),
        } for e in fa["look_through"][:20]]

        sbase = fa["sector_base"] or 1
        smax = max(fa["sectors"].values()) if fa["sectors"] else 1
        true_sectors = [{"name": k, "value": money(v, 2),
                         "pct_eq": pct(v / sbase, 1), "pct_tot": pct(v / total, 1),
                         "w": round(v / smax * 100, 1)}
                        for k, v in fa["sectors"].items()]

    advisory = advisory_context(p.get("advisory") or {})

    funds = [{"symbol": f["symbol"], "note": f["note"],
              "value": money(f["value"], 2), "pct": pct(f["value"] / total, 1)}
             for f in p["funds"]]
    cash = [{"symbol": c["symbol"], "value": money(c["value"], 2),
             "pct": pct(c["value"] / total, 1)} for c in p["cash"]]
    other = [{"symbol": o["symbol"], "value": money(o["value"], 2),
              "pct": pct(o["value"] / total, 1)} for o in p["other"]]

    # freshness line under the title: says plainly whether the values on this
    # page are live quotes or the market values off the last statement
    pr = p.get("pricing") or {}
    if pr.get("fetched_at") and pr.get("priced"):
        when = pr["fetched_at"].replace("T", " ")[:16]
        label = f'Prices updated {when} · {pr["priced"]} of {pr["priceable"]} holdings repriced live'
        if pr.get("errors"):
            label += f' · {len(pr["errors"])} could not be quoted'
    else:
        label = "Showing market values as of your last statement — click Update prices for live quotes"
    pricing = {"label": label, **pr}

    activity = p.get("investment_activity") or {}
    trade_rows = []
    for trade in reversed(activity.get("trades") or []):
        side = str(trade.get("side") or "").lower()
        gain = trade.get("realized_gain")
        basis_lots = trade.get("basis_lots") or []
        basis_detail = "; ".join(
            f'{lot.get("acquired", "—")}: '
            f'{float(lot.get("quantity") or 0):,.4f}'.rstrip("0").rstrip(".") +
            f' shares, {exact_money(lot.get("cost_basis"))} basis, '
            f'{exact_money(lot.get("gain_loss"), signed=True)} gain/loss'
            for lot in basis_lots)
        trade_rows.append({
            "date": trade.get("date") or "—",
            "account": str(trade.get("account") or "—").replace("[NAME] ", ""),
            "side": side.title() or "—",
            "side_cls": "up" if side == "buy" else "down",
            "symbol": trade.get("symbol") or "—",
            "quantity": f'{trade.get("quantity", 0):,.4f}'.rstrip("0").rstrip("."),
            "price": exact_money(trade.get("price")),
            "amount": exact_money(trade.get("amount")),
            "cost_basis": (exact_money(trade.get("cost_basis"))
                           if side == "sell" and trade.get("cost_basis") is not None
                           else "—"),
            "basis_source": ((str(trade.get("basis_source") or "—").title() +
                              (f" ({len(basis_lots)} lots)" if basis_lots else ""))
                             if side == "sell" else "—"),
            "basis_detail": basis_detail or None,
            "gain": (exact_money(gain, signed=True) if gain is not None else "—"),
            "gain_pct": (pct(trade.get("gain_pct"), 1, True)
                         if trade.get("gain_pct") is not None else "—"),
            "outcome": str(trade.get("outcome") or "—").title(),
            "gain_dir": ("up" if (gain or 0) > 0 else "down" if (gain or 0) < 0 else ""),
        })
    trade_summary = {
        "buy_count": activity.get("buy_count") or 0,
        "buy_total": exact_money(activity.get("buy_total") or 0),
        "sell_count": activity.get("sales_count") or 0,
        "sell_total": exact_money(activity.get("sales_proceeds") or 0),
        "known": activity.get("sales_with_known_basis") or 0,
        "unknown": activity.get("unknown_sales") or 0,
        "cost_basis": exact_money(activity.get("realized_cost_basis") or 0),
        "realized_gain": (exact_money(activity.get("realized_gain"), signed=True)
                          if activity.get("realized_gain") is not None else "—"),
        "gain_dir": ("up" if (activity.get("realized_gain") or 0) > 0 else
                     "down" if (activity.get("realized_gain") or 0) < 0 else ""),
        "profitable": activity.get("profitable_sales") or 0,
        "losses": activity.get("loss_sales") or 0,
        "break_even": activity.get("break_even_sales") or 0,
        "method": activity.get("realized_basis_method") or "—",
        "reversals": activity.get("reversed_trade_pairs_excluded") or 0,
    }

    return {"p": p, "pricing": pricing,
            "tiles": tiles, "charts": charts, "mix": mix, "rows": rows,
            "sector_rows": sector_rows, "funds": funds, "cash": cash, "other": other,
            "beta": f"{beta:.2f}" if beta else "—",
            "dy": pct(dy, 2) if dy else "—",
            "tpe": f"{tpe:.1f}" if tpe else "—",
            "failed": [{"ticker": f["ticker"], "reason": f["reason"],
                        "value": money(f["value"], 2),
                        "pct": pct(f["value"] / total, 1) if total else "—",
                        "link": (f'/dashboard/{f["ticker"]}'
                                 if f.get("has_dashboard") else None)}
                       for f in (p.get("failed") or [])],
            "fund_rows": fund_rows, "look_rows": look_rows, "alloc_rows": alloc_rows,
            "true_sectors": true_sectors, "fees": fees, "advisory": advisory,
            "fee_charts": fee_charts,
            "trade_rows": trade_rows, "trade_summary": trade_summary,
            "look_coverage": pct(fa.get("look_through_coverage"), 0)
            if fa.get("look_through_coverage") else None,
            "generated_at": p.get("generated", "")}


def render(p: dict, out: Path | None = None) -> Path:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      autoescape=select_autoescape(["html", "j2"]))
    html = env.get_template("portfolio.html.j2").render(**build_context(p))
    out = out or common.FIN_DATA / "portfolio.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    diag(f"[portfolio] wrote {out.name} ({out.stat().st_size // 1024} KB)")
    return out


if __name__ == "__main__":
    data = common.load_json(common.FIN_DATA / "portfolio.json")
    if not data:
        diag("[portfolio] no portfolio.json — run finance/portfolio.py first")
        raise SystemExit(1)
    render(data)
