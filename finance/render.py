"""Render finance_data/dashboard.html from analysis.json.

Usage:
    python finance/render.py [--analysis PATH] [--out PATH]

Reuses the stock pipeline's SVG chart machinery (Frame, formatters) so the
finance dashboard matches the ticker dashboards visually.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
from common import ROOT, diag

# finance/render.py shadows the name "render", so load pipeline/render.py by path
_spec = importlib.util.spec_from_file_location("pipeline_render",
                                               ROOT / "pipeline" / "render.py")
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

money, pct, nice_ticks, Frame = pr.money, pr.pct, pr.nice_ticks, pr.Frame
S1, S2, S3, S4 = pr.S1, pr.S2, pr.S3, pr.S4


def signed_money(v: float | None, dec: int = 2) -> str:
    """money() with the sign always shown — a change of zero is still news."""
    if v is None:
        return "—"
    return ("+" if v >= 0 else "") + money(v, dec)


# The two success-probability thresholds. They decide the colour of the headline
# number, so the page draws them on a scale rather than leaving the reader to
# guess what amber meant.
PROB_COMFORTABLE, PROB_AT_RISK = 0.85, 0.70

# Which numbers stop being answers when a profile field is missing. A banner at
# the top saying "some figures are placeholders" is only useful if the figures
# themselves say which ones they are.
GAP_LABELS = {
    "spending.retirement_monthly_today": "Retirement spending",
    "spending.current_monthly": "Current monthly spending",
    "household.self_birthdate": "Your birthdate",
    "home.value": "Home market value",
    "income.salary_annual": "Salary",
}
GAP_TARGETS = {
    "spending.retirement_monthly_today": (
        {"success", "headroom"}, {"answer", "plan", "markets"}),
    "spending.current_monthly": (
        {"savings_rate", "surplus"}, {"today", "spending"}),
    "household.self_birthdate": ({"to_retirement"}, {"plan"}),
    "home.value": ({"net_worth"}, {"today"}),
    "income.salary_annual": ({"savings_rate", "net_worth"}, {"today", "plan"}),
}


def parse_gaps(raw: list[str]) -> list[dict]:
    """Split analyze.py's gap strings back into path, reason and severity.

    They are written as "MISSING (required) <path> — <why>", which reads fine in
    a log and not at all in a list the user is meant to act on.
    """
    gaps = []
    for text in raw:
        head, _, why = text.partition(" — ")
        path = head.rsplit(") ", 1)[-1].strip()
        gaps.append({"text": text, "path": path, "why": why or text,
                     "required": text.startswith("MISSING (required)"),
                     "label": GAP_LABELS.get(path, path)})
    return gaps


def gap_targets(gaps: list[dict]) -> tuple[set, set]:
    """The tile keys and section ids that a missing field turns into estimates."""
    tiles: set[str] = set()
    sections: set[str] = set()
    for g in gaps:
        t, s = GAP_TARGETS.get(g["path"], (set(), set()))
        tiles |= t
        sections |= s
    return tiles, sections


def build_verdict(prob: float, n: int, sustainable: float, desired: float | None,
                  est: bool) -> dict:
    """The one thing the reader opened the page to find out, as a sentence.

    Six tiles of equal weight leave them to work out which number is the answer;
    this states it, says where it sits against the thresholds, and names the
    lever that moves it.
    """
    headroom = sustainable - (desired or 0)
    if prob >= PROB_COMFORTABLE:
        cls, band = "good", "comfortable"
        standing = (f"That is at or above the {PROB_COMFORTABLE:.0%} this model "
                    f"treats as comfortable.")
    elif prob >= PROB_AT_RISK:
        cls, band = "warn", "tight"
        standing = (f"That is short of the {PROB_COMFORTABLE:.0%} this model treats "
                    f"as comfortable, though clear of the {PROB_AT_RISK:.0%} it "
                    f"treats as at risk.")
    else:
        cls, band = "bad", "at risk"
        standing = (f"That is below the {PROB_AT_RISK:.0%} this model treats as "
                    f"at risk.")
    if headroom < 0:
        lever = (f"The gap is spending, not returns: the plan asks for "
                 f"${desired or 0:,.0f}/mo and supports ${sustainable:,.0f}/mo.")
    else:
        lever = (f"Spending has room: the plan asks for ${desired or 0:,.0f}/mo "
                 f"and supports ${sustainable:,.0f}/mo.")
    return {
        "prob": prob, "pct": f"{prob:.0%}", "n": n, "cls": cls, "band": band,
        "headline": f"Your plan holds in {prob:.0%} of {n:,} simulated markets.",
        "standing": standing, "lever": lever,
        "at_risk_pct": round(PROB_AT_RISK * 100),
        "mid_pct": round((PROB_COMFORTABLE - PROB_AT_RISK) * 100),
        "good_pct": round((1 - PROB_COMFORTABLE) * 100),
        "marker": round(min(max(prob, 0.02), 0.98) * 100, 1),
        "est": est,
    }


def _pricing_context(p: dict) -> dict | None:
    """How the balances on this page were valued.

    Every figure downstream — net worth, allocation, the projection — is only
    as current as the prices behind it, so the page says which it used and
    what the market has done since the statement.
    """
    if not p:
        return None
    if not p.get("applied"):
        return {"applied": False, "reason": p.get("reason") or "not repriced"}
    h = p.get("holdings") or {}
    when = (p.get("fetched_at") or "")[:16].replace("T", " ")
    return {
        "applied": True, "when": when,
        "quotes": p.get("quotes"),
        "delta": signed_money(p.get("asset_delta")),
        "delta_raw": p.get("asset_delta") or 0,
        "dir": "up" if (p.get("asset_delta") or 0) >= 0 else "down",
        "holdings_delta": signed_money(h.get("delta")),
        "n_priced": h.get("n_priced"), "n": h.get("n"),
        "coverage": pct(h.get("coverage"), 0) if h.get("coverage") else None,
        "statement_total": money(p.get("statement_portfolio"), 2),
        "market_total": money(p.get("market_portfolio"), 2),
        "repriced": [{"name": r["name"], "delta": signed_money(r["delta"]),
                      "dir": "up" if r["delta"] >= 0 else "down"}
                     for r in (p.get("accounts_repriced") or [])],
        "unpriced": [{"name": u["name"], "balance": money(u["balance"], 2)}
                     for u in (p.get("accounts_unpriced") or [])],
    }


# ------------------------------------------------------------- chart builders --
def _markers(fr: Frame, x_vals: list, markers: list[tuple]) -> None:
    for xv, label in markers:
        if xv not in x_vals:
            continue
        xx = fr.x(x_vals.index(xv))
        fr.svg.line(xx, fr.top, xx, fr.top + fr.ph, "var(--baseline)", 1, dash="4 4")
        fr.svg.text(xx, fr.top - 8, label, 11)


def _path(fr: Frame, pts: list[tuple], color: str, width: float = 2) -> None:
    if not pts:
        return
    fr.svg.add(f'<path fill="none" stroke="{color}" stroke-width="{width}" '
               'stroke-linejoin="round" stroke-linecap="round" d="M' +
               " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>')


def age_line_chart(x_vals, series, chart_id, title, subtitle, markers=None,
                   x_prefix="", vbw=960, y_fmt=None, val_fmt=None):
    """series: list of (name, color, [values-or-None per x]). x_vals: ages/years."""
    y_fmt = y_fmt or (lambda v: money(v, 1))
    val_fmt = val_fmt or (lambda v: money(v, 2))
    vals = [v for _, _, vs in series for v in vs if v is not None]
    ticks = nice_ticks(0, max(vals) * 1.06 if vals else 1)
    fr = Frame(vbw=vbw, y_lo=0, y_hi=ticks[-1], n_slots=len(x_vals), y_fmt=y_fmt)
    fr.grid(ticks)
    _markers(fr, x_vals, markers or [])
    for name, color, vs in series:
        seg = []
        for i, v in enumerate(vs):
            if v is None:
                _path(fr, seg, color)
                seg = []
            else:
                seg.append((fr.x(i), fr.y(v)))
        _path(fr, seg, color)
        last_i = max(i for i, v in enumerate(vs) if v is not None)
        ex, ey = fr.x(last_i), fr.y(vs[last_i])
        fr.svg.add(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4" fill="{color}" '
                   f'stroke="var(--surface)" stroke-width="2"/>')
    for i, xv in enumerate(x_vals):
        fr.slots.append({"label": f"{x_prefix}{xv}",
                         "items": [[name, val_fmt(vs[i]) if vs[i] is not None else "—", color]
                                   for name, color, vs in series]})
    fr.x_labels([str(x) for x in x_vals], every=max(1, len(x_vals) // 9))
    fr.crosshair()
    rows = [[f"{x_prefix}{x_vals[i]}"] + [val_fmt(vs[i]) if vs[i] is not None else "—"
                                          for _, _, vs in series]
            for i in range(len(x_vals))]
    return {"id": chart_id, "title": title, "subtitle": subtitle,
            "legend": [{"kind": "line", "color": c, "label": n} for n, c, _ in series],
            "svg": fr.svg.render(), "data_json": fr.data_json(),
            "table": {"head": ["Age" if not x_prefix else "Period"]
                      + [n for n, _, _ in series], "rows": rows}}


def band_chart(ages, bands, chart_id, title, subtitle, markers=None):
    """Monte Carlo fan: p10-p90 and p25-p75 filled, median line."""
    ticks = nice_ticks(0, max(bands["p90"]) * 1.06)
    fr = Frame(y_lo=0, y_hi=ticks[-1], n_slots=len(ages), y_fmt=lambda v: money(v, 1))
    fr.grid(ticks)
    _markers(fr, ages, markers or [])

    def poly(hi_key, lo_key, opacity):
        pts = ([(fr.x(i), fr.y(v)) for i, v in enumerate(bands[hi_key])]
               + [(fr.x(i), fr.y(v)) for i, v in reversed(list(enumerate(bands[lo_key])))])
        fr.svg.add(f'<path fill="var(--s1)" opacity="{opacity}" d="M' +
                   " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + ' Z"/>')

    poly("p90", "p10", 0.10)
    poly("p75", "p25", 0.16)
    _path(fr, [(fr.x(i), fr.y(v)) for i, v in enumerate(bands["p50"])], S1)
    for i, age in enumerate(ages):
        fr.slots.append({"label": f"Age {age}",
                         "items": [["90th pct", money(bands["p90"][i], 2), ""],
                                   ["75th pct", money(bands["p75"][i], 2), ""],
                                   ["median", money(bands["p50"][i], 2), S1],
                                   ["25th pct", money(bands["p25"][i], 2), ""],
                                   ["10th pct", money(bands["p10"][i], 2), ""]]})
    fr.x_labels([str(a) for a in ages], every=max(1, len(ages) // 9))
    fr.crosshair()
    rows = [[ages[i]] + [money(bands[k][i], 2) for k in ("p10", "p25", "p50", "p75", "p90")]
            for i in range(0, len(ages), 5)]
    return {"id": chart_id, "title": title, "subtitle": subtitle,
            "legend": [{"kind": "line", "color": "var(--s1)", "label": "median"},
                       {"kind": "band", "color": "var(--s1)", "label": "25th–75th pct"},
                       {"kind": "band", "color": "var(--s1)", "label": "10th–90th pct"}],
            "svg": fr.svg.render(), "data_json": fr.data_json(),
            "table": {"head": ["Age", "p10", "p25", "p50", "p75", "p90"], "rows": rows}}


# ------------------------------------------------------------------- context --
def months_until(ym: str) -> int:
    today = date.today()
    return max((int(ym[:4]) - today.year) * 12 + int(ym[5:7]) - today.month, 0)


def build_context(a: dict, profile: dict) -> dict:
    snap, pj, mc = a["snapshot"], a["projection"], a["monte_carlo"]
    cf = snap["cashflow"]
    # how these balances were valued — needed by the composition card, the
    # holdings table and the projection subtitle alike
    pricing = _pricing_context(a.get("pricing") or {})
    ret_ym = profile["household"].get("retirement_date",
                                     common.DEFAULT_HOUSEHOLD["retirement_date"])
    m_left = months_until(ret_ym)
    sustainable, desired = pj["sustainable_monthly"], pj["desired_monthly"]
    headroom = sustainable - (desired or 0)

    prob = mc["success_prob"]
    gaps = parse_gaps(a.get("data_gaps") or [])
    est_tiles, est_sections = gap_targets(gaps)

    # Every tile that carries a judgement carries the word for it too: red and
    # green is the one distinction a large minority of readers cannot make.
    tiles = [
        {"key": "net_worth", "label": "Net worth (incl. home)",
         "value": money(snap["net_worth"], 2),
         "sub": f'{money(snap["portfolio"], 2)} investable'},
        {"key": "success", "label": "Modeled success probability",
         "value": f"{prob:.0%}",
         "cls": "good" if prob >= PROB_COMFORTABLE else ("warn" if prob >= PROB_AT_RISK else "bad"),
         "status": ("comfortable" if prob >= PROB_COMFORTABLE
                    else ("tight" if prob >= PROB_AT_RISK else "at risk")),
         "sub": (f'{mc["n"]:,} fat-tailed paths through {pj["assumptions"]["joint_horizon_year"]}; '
                 f'younger spouse age {pj["assumptions"]["younger_spouse_horizon_age"]}')},
        {"key": "savings_rate", "label": "Savings rate",
         "value": pct(snap["savings_rate"], 1),
         "sub": "of gross household income"},
        {"key": "surplus", "label": "Monthly surplus",
         "value": f'${cf["surplus_monthly"]:,}',
         "cls": "good" if cf["surplus_monthly"] >= 0 else "bad",
         "status": "positive" if cf["surplus_monthly"] >= 0 else "negative",
         "sub": "take-home minus current spending"},
        {"key": "to_retirement", "label": "Time to retirement",
         "value": f"{m_left // 12}y {m_left % 12}m",
         "sub": f"target {ret_ym}"},
        {"key": "headroom",
         "label": "Spending headroom" if headroom >= 0 else "Spending gap",
         "value": f"${abs(headroom):,.0f}/mo",
         "cls": "good" if headroom >= 0 else "bad",
         "status": "room to spare" if headroom >= 0 else "short",
         "sub": f"adjustable lifestyle ${sustainable:,.0f} vs modeled ${desired:,.0f}"},
    ]
    for t in tiles:
        t["est"] = t["key"] in est_tiles

    verdict = build_verdict(prob, mc["n"], sustainable, desired,
                            est="answer" in est_sections)

    # The three assumptions that move the answer most, said once under the
    # verdict. The full list stays where it is, at the foot of the page.
    pja = pj["assumptions"]
    key_assumptions = [
        f'{pja["mean_return"]:.1%} nominal return',
        f'{pja["inflation"]:.1%} inflation',
    ]
    drag = pja.get("fee_drag_detail") or {}
    if drag.get("advisory") or drag.get("funds"):
        key_assumptions.append(
            f'net of ${drag.get("advisory", 0) + drag.get("funds", 0):,.0f}/yr in fees')

    # cash-flow breakdown of gross pay
    gross = cf["gross_monthly"]
    parts = [("Taxes (fed+CA+FICA)", cf["taxes_monthly"], S2),
             ("Pre-tax savings & deductions", cf["pretax_monthly"], S4),
             ("Spending", cf["expenses_monthly"], S3),
             ("Surplus", max(cf["surplus_monthly"], 0), S1)]
    cash_dist = [{"name": n, "value": f"${v:,}", "w": round(v / gross * 100, 1), "color": c}
                 for n, v, c in parts if v > 0]

    # net-worth composition horizontal bars
    comp, max_bal = [], max([x["balance"] for x in snap["assets"]]
                            + [snap["home_value"], 1])
    for asset in snap["assets"]:
        comp.append({"name": asset["name"], "value": money(asset["balance"], 2),
                     "w": round(asset["balance"] / max_bal * 100, 1), "neg": False})
    if snap["home_value"]:
        comp.append({"name": "Home (market value)", "value": money(snap["home_value"], 2),
                     "w": round(snap["home_value"] / max_bal * 100, 1), "neg": False})
    for liab in snap["liabilities"]:
        comp.append({"name": liab["name"], "value": "−" + money(liab["balance"], 2),
                     "w": round(liab["balance"] / max_bal * 100, 1), "neg": True})

    charts = {}
    # The projection compounds from today's balance, so the chart states which
    # balance that is — a plan drawn from a stale statement is a different plan.
    start_note = ""
    if pricing and pricing.get("applied"):
        start_note = (f' Starts from {pricing["market_total"]} at market prices '
                      f'({pricing["when"]}), {pricing["delta"]} against the '
                      f'statement.')
    ages = pj["ages"]
    markers = [(pj["markers"]["retire_age"], "retire"),
               (pj["markers"]["ss_claim_age"], "SS"),
               (pj["markers"]["rmd_age"], "RMDs")]
    charts["projection"] = age_line_chart(
        ages,
        [("Base", S1, pj["scenarios"]["base"]["total"]),
         ("Optimistic", S3, pj["scenarios"]["optimistic"]["total"]),
         ("Pessimistic", S2, pj["scenarios"]["pessimistic"]["total"])],
        "chart-proj", "Portfolio projection through the joint longevity horizon",
        "Nominal dollars. Base return {:.1%} (optimistic/pessimistic ±2pp). "
        "Dashed lines: retirement, SS claim, RMDs.{}".format(
            pj["assumptions"]["mean_return"], start_note),
        markers=markers, x_prefix="Age ")

    charts["mc"] = band_chart(mc["bands"]["ages"], mc["bands"], "chart-mc",
                              "Monte Carlo — portfolio percentile bands",
                              f'{mc["n"]:,} seeded, fat-tailed return paths with stochastic '
                              f'inflation (σ = {pj["assumptions"]["stdev"]:.1%}/yr, allocation-derived).',
                              markers=markers)

    ss = a["social_security"]
    ss_ages = ss["cumulative_ages"]
    ss_series = [(f"Claim at {age}", [S1, S3, S2][i],
                  [v or None for v in ss["cumulative_by_age"][age]])
                 for i, age in enumerate(sorted(ss["cumulative_by_age"]))]
    charts["ss"] = age_line_chart(
        ss_ages, ss_series, "chart-ss", "Social Security — cumulative benefits by claim age",
        "Today's dollars, your record only. Where lines cross = breakeven age.",
        x_prefix="Age ", val_fmt=lambda v: money(v, 0))

    k = a.get("k401")
    k401 = None
    if k:
        k401 = dict(k)
        k401.update({
            "elective_f": money(k["elective_annual"], 2),
            "match_f": money(k["match_annual"], 2),
            "total_f": money(k["total_additions"], 2),
            "limit_f": money(k["elective_limit"], 2),
            "base_limit_f": money(k["base_limit"], 2),
            "catchup_f": money(k["catchup"], 2),
            "headroom_f": money(k["headroom"], 2),
            "add_limit_f": money(k["total_additions_limit"], 2),
            "per_f": f'${k["per_period"]:,.2f}' if k.get("per_period") else "—",
            "match_per_f": (f'${k["match_per_period"]:,.2f}'
                            if k.get("match_per_period") else "—"),
            "needed_per_f": f'${k["needed_per_period"]:,.2f}',
            "super_f": money(k["super_catchup_limit"], 2),
            "regular_headroom_f": money(max(k["base_limit"] - k["elective_annual"], 0), 2),
            "roth_headroom_f": money(max(k["headroom"] - max(k["base_limit"] - k["elective_annual"], 0), 0), 2),
            "fill": min(round(k["elective_annual"] / k["elective_limit"] * 100), 100),
        })

    esc = (profile.get("mortgage") or {}).get("escrow_detail") or {}
    esc_ctx = None
    if esc.get("property_tax_annual") or esc.get("insurance_annual"):
        pja = pj["assumptions"]
        tax, ins = esc.get("property_tax_annual", 0), esc.get("insurance_annual", 0)
        yrs = pj["markers"]["retire_age"] - (pj["ages"][0])
        horizon = pj["ages"][-1] - pj["ages"][0]
        esc_ctx = {
            "tax": money(tax, 2), "ins": money(ins, 2),
            "total": money(tax + ins, 2),
            "monthly": money((tax + ins) / 12, 2),
            "reserve": money(esc.get("reserve_monthly"), 2)
            if esc.get("reserve_monthly") else None,
            "effective": esc.get("effective_date"),
            "tax_growth": pct(pja.get("property_tax_growth", 0.02), 1),
            "ins_growth": pct(pja.get("insurance_inflation", 0.06), 1),
            "at_retire": money(
                tax * (1 + pja.get("property_tax_growth", 0.02)) ** yrs
                + ins * (1 + pja.get("insurance_inflation", 0.06)) ** yrs, 2),
            "at_horizon": money(
                tax * (1 + pja.get("property_tax_growth", 0.02)) ** horizon
                + ins * (1 + pja.get("insurance_inflation", 0.06)) ** horizon, 2),
            "horizon_age": pj["ages"][-1],
        }

    mort = a.get("mortgage")
    if mort:
        years = sorted({y for s in mort["scenarios"] for y, _ in s["annual_balances"]})
        m_series = []
        for i, s in enumerate(mort["scenarios"]):
            by_year = dict(s["annual_balances"])
            m_series.append((s["name"], [S1, S3, S4, S2][i % 4],
                             [by_year.get(y) for y in years]))
        charts["mortgage"] = age_line_chart(
            years, m_series, "chart-mort", "Mortgage balance by payoff strategy",
            f'Rate {mort["rate"]:.3%}, P&I ${mort["monthly_pi"]:,.0f}/mo.',
            val_fmt=lambda v: money(v, 0))

    ss_table = [{"age": age, "monthly": f'${ss["monthly_by_claim_age"][str(age)]:,.0f}',
                 "household": (f'${ss["household_monthly_by_claim_age"][str(age)]:,.0f}'
                               if str(age) in ss["household_monthly_by_claim_age"] else "—")}
                for age in range(62, 71)]

    component_labels = {
        "lifestyle": "Adjustable lifestyle", "healthcare": "Healthcare total",
        "health_pre_medicare": "Pre-Medicare coverage", "health_medicare": "Medicare + OOP",
        "mortgage_pi": "Mortgage P&I", "property_tax": "Property tax",
        "insurance": "Homeowners insurance", "home_reserve": "Home-capital reserve",
        "scheduled": "Scheduled capital purchases", "irmaa": "IRMAA surcharge",
        "long_term_care": "Long-term care stress"
    }
    retirement_rows = [r for r in pj.get("base_rows", []) if r.get("spending")]
    annual_expenses = []
    for r in retirement_rows:
        c = r.get("spending_components") or {}
        housing = sum(c.get(k, 0) for k in ("mortgage_pi", "property_tax", "insurance"))
        annual_expenses.append({
            "year": r["year"], "ages": f'{r["age"]} / {r.get("spouse_age", "—")}',
            "lifestyle": money(c.get("lifestyle", 0), 1),
            "healthcare": money(c.get("healthcare", 0) + c.get("irmaa", 0), 1),
            "housing": money(housing, 1), "home": money(c.get("home_reserve", 0), 1),
            "capital": money(c.get("scheduled", 0) + c.get("long_term_care", 0), 1),
            "taxes": money(r.get("taxes", 0), 1), "total": money(r["spending"], 1),
            "events": ", ".join(e.get("name", "") for e in r.get("events", [])) or "—"})
    first_ret = retirement_rows[0] if retirement_rows else None
    expense_bridge = []
    if first_ret:
        for key, value in first_ret.get("spending_components", {}).items():
            if value:
                source = ("Document-derived" if key in ("mortgage_pi", "property_tax", "insurance")
                          else "Derived from transaction history" if key == "lifestyle"
                          else "Editable planning estimate")
                expense_bridge.append({"name": component_labels.get(key, key),
                                       "annual": money(value, 1),
                                       "monthly": money(value / 12, 0), "source": source})
    health_years = {pj["markers"].get("spouse_medicare_year")}
    if first_ret:
        health_years.add(first_ret["year"])
    health_transitions = []
    for r in retirement_rows:
        if r["year"] in health_years:
            c = r.get("spending_components") or {}
            hd = r.get("healthcare_detail") or {}
            health_transitions.append({"year": r["year"], "ages": f'{r["age"]} / {r.get("spouse_age")}',
                                       "pre": money(hd.get("pre_medicare", 0), 1),
                                       "medicare": money(hd.get("medicare", 0), 1),
                                       "irmaa": money(c.get("irmaa", 0), 1),
                                       "magi_2yr": money(next((x.get("magi", 0) for x in pj.get("base_rows", [])
                                                                if x["year"] == r["year"] - 2), 0), 1)})
    stress_labels = {"sequence_shock": "Early-retirement return shock",
                     "high_inflation": "10 years high inflation",
                     "social_security_minus_10pct": "Social Security −10%",
                     "long_term_care": "3-year long-term care event",
                     "combined": "Combined adverse case"}
    stress_rows = [{"name": stress_labels.get(k, k),
                    "depletion": (f'Age {v["depleted_at"]}' if v.get("depleted_at") else "No depletion"),
                    "terminal": money(v.get("end_balance", 0), 1),
                    "reserve": money(v.get("min_liquid", 0), 1)}
                   for k, v in (a.get("stress_cases") or {}).items()]
    rm = pj["assumptions"].get("risk_model") or {}
    risk_rows = [{"name": k.title(), "value": pct(v, 1)}
                 for k, v in (rm.get("allocation") or {}).items()]
    mc_metrics = {
        "near": pct(mc.get("near_failure_prob"), 1),
        "terminal_median": money(mc.get("median_end_balance"), 1),
        "terminal_p10": money(mc.get("p10_end_balance"), 1),
        "reserve_median": money(mc.get("median_min_liquid"), 1),
        "reserve_p10": money(mc.get("p10_min_liquid"), 1),
        "depletion": mc.get("depletion_ages") or {},
    }

    tn, tr = snap["taxes_now"], a.get("taxes_retirement", {})
    tax_cards = {
        "now": {"effective": pct(tn["effective_rate"], 1),
                "marginal": pct(tn["marginal_federal"] + tn["marginal_state"], 1),
                "detail": (f'Federal ${tn["federal"]:,} + CA ${tn["state"]:,} '
                           f'+ FICA ${tn["fica"]:,} = ${tn["total"]:,}/yr')},
        "ret": {"effective": pct(tr.get("effective_rate"), 1),
                "detail": (f'At age {tr.get("example_age")}: federal ${tr.get("federal", 0):,} '
                           f'+ CA ${tr.get("state", 0):,} on withdrawals + SS'),
                "ss_pct": pct(tr.get("ss_taxable_pct"), 0),
                "irmaa": tr.get("irmaa_tier", "—")},
    }

    sd = a.get("spending_detail")
    spend_rows, oneoff_rows = [], []
    if sd:
        months = sorted(sd["monthly_total"])
        vals = [sd["monthly_total"][m] for m in months]
        charts["spending"] = age_line_chart(
            months, [("Monthly spending", S2, vals)], "chart-spend",
            f'Actual monthly spending — {sd["source_months"]} months of checking activity',
            f'Transfers to investment accounts and income are excluded. '
            f'Average {money(sd["avg_monthly"], 2)}/mo; last 12 months '
            f'{money(sd["avg_monthly_recent12"], 2)}/mo.',
            y_fmt=lambda v: money(v, 0), val_fmt=lambda v: money(v, 2))
        total_m = sd["avg_monthly"] or 1
        spend_rows = [{"name": c["name"], "monthly": money(c["monthly"], 2),
                       "pct": pct(c["monthly"] / total_m, 1), "n": c["n"]}
                      for c in sd["categories"] if c["monthly"] > 0]
        oneoff_rows = [{"month": o["month"], "category": o["category"],
                        "amount": money(o["amount"], 2),
                        "description": o["description"]}
                       for o in (sd.get("one_offs") or [])[:12]]

    ia = a.get("investment_activity")
    inv_rows = []
    if ia:
        inv_rows = [{"name": g["name"], "n": g["n"], "total": money(g["total"], 2)}
                    for g in ia.get("groups", []) if abs(g["total"]) >= 1]

    vest = a.get("vesting")
    vest_ctx = None
    if vest:
        eff = vest.get("effect") or {}
        vest_ctx = {
            "symbol": vest.get("symbol"), "priced": vest.get("priced"),
            "n": vest["n_future"], "shares": f'{vest["total_shares"]:,.0f}',
            "first": vest["first_date"], "last": vest["last_date"],
            "gross": money(vest["gross_total"], 2) if vest.get("gross_total") else None,
            "net": money(vest["net_total"], 2) if vest.get("net_total") else None,
            "price": f'${vest["price"]:,.2f}' if vest.get("price") else None,
            "withholding": pct(vest["withholding"], 1),
            "withholding_measured": vest.get("withholding_measured"),
            "withholding_n": vest.get("withholding_from_n") or 0,
            "conditions": vest.get("conditions") or [],
            "n_conditional": vest.get("n_conditional") or 0,
            "include_conditional": vest.get("include_conditional", True),
            "has_options": vest.get("has_options"),
            "rows": [{"year": y,
                      "shares": f'{v["shares"]:,.0f}',
                      "dates": len(v["dates"]),
                      "gross": money(v["gross"], 2) if v["gross"] else "—",
                      "net": money(v["net"], 2) if v["net"] else "—"}
                     for y, v in (vest.get("by_year") or {}).items()],
            # the plan with these shares against the plan without them
            "depleted_with": eff.get("depleted_with"),
            "depleted_without": eff.get("depleted_without"),
            "years_bought": ((eff["depleted_with"] - eff["depleted_without"])
                             if eff.get("depleted_with") and eff.get("depleted_without")
                             else None),
            "end_with": money(eff.get("end_balance_with"), 2),
            "end_without": money(eff.get("end_balance_without"), 2),
            "end_delta": money((eff.get("end_balance_with") or 0)
                               - (eff.get("end_balance_without") or 0), 2),
        }

    goal_rows = []
    for g in (a.get("goal_analysis") or {}).get("goals", []):
        kind, actual, wanted = g.get("type"), g.get("actual"), g.get("wanted")
        if kind == "retirement":
            actual_text = (f"{actual.get('date')}; {money(actual.get('monthly_spending_today'))}/mo"
                           if isinstance(actual, dict) else "—")
            wanted_text = (f"{wanted.get('date')}; {money(wanted.get('monthly_spending_today'))}/mo"
                           if isinstance(wanted, dict) else "—")
        elif kind == "success_probability":
            actual_text, wanted_text = pct(actual, 1), pct(wanted, 1)
        elif kind == "cash_reserve":
            actual_text = f"{actual:.1f} months" if actual is not None else "—"
            wanted_text = f"{wanted:.1f} months" if wanted is not None else "—"
        elif kind in {"legacy", "major_purchase"}:
            actual_text, wanted_text = money(actual), money(wanted)
        else:
            actual_text, wanted_text = str(actual or "—"), str(wanted or "—")
        goal_rows.append({"title": g.get("title"), "type": kind.replace("_", " "),
                          "priority": g.get("priority"), "actual": actual_text,
                          "target": wanted_text, "status": g.get("status")})

    recon = (sd or {}).get("reconciliation") or []
    recon_rows = [{
        "label": r["label"], "issuer": (r["issuer"] or "—").upper(),
        "window": f'{r["first_month"]} – {r["last_month"]}',
        "bank": money(r["bank_payments_in_window"], 2),
        "card": money(r["card_payments_recorded"], 2),
        "delta": money(r["delta"], 2),
        "delta_pct": (pct(r["delta"] / r["bank_payments_in_window"], 2)
                      if r["bank_payments_in_window"] else "—"),
        "removed": money(r["payments_removed_from_checking"], 2),
        "added": money(r["card_net_charges"], 2),
        "uncovered": len(r["uncovered_months"]),
    } for r in recon]

    hold = snap.get("holdings") or {}
    hold_rows = [{"symbol": h["symbol"], "value": money(h["value"], 2),
                  "pct": (pct(h["value"] / snap["portfolio"], 1)
                          if snap.get("portfolio") else "—"),
                  # what the market did to this position since the statement,
                  # so a balance that moved is attributable rather than mysterious
                  "change": (signed_money(h["price_change"])
                             if h.get("price_change") else None),
                  "dir": ("up" if (h.get("price_change") or 0) > 0 else "down"),
                  "price": (f'${h["live_price"]:,.2f}' if h.get("live_price")
                            else None)}
                 for h in (hold.get("all") or hold.get("top", []))]

    # The rail. Thirty-odd cards in one column is a document without a contents
    # page; this is the contents page, and it only lists what actually rendered.
    nav_sections = [
        ("answer", "The answer", True),
        ("today", "Today's picture", True),
        ("k401", "401(k)", bool(k401)),
        ("spending", "Where the money goes", bool(sd)),
        ("equity", "Unvested equity", bool(vest_ctx)),
        ("investments", "Investments", bool(inv_rows or hold_rows)),
        ("plan", "The plan to {}".format(pj["ages"][-1]), True),
        ("markets", "Ten thousand markets", True),
        ("socsec", "Social Security", bool(charts.get("ss"))),
        ("mortgage", "Mortgage & escrow", bool(charts.get("mortgage") or esc_ctx)),
        ("taxes", "Taxes & assumptions", True),
        ("recs", "What to do next", bool(a.get("recommendations"))),
    ]
    nav = [{"id": f"sec-{sid}", "label": label, "est": sid in est_sections,
            "count": len(a["recommendations"]) if sid == "recs" else None}
           for sid, label, shown in nav_sections if shown]

    return {"a": a, "snap": snap, "cf": cf, "tiles": tiles, "cash_dist": cash_dist,
            "sd": sd, "spend_rows": spend_rows, "oneoff_rows": oneoff_rows,
            "ia": ia, "inv_rows": inv_rows,
            "vest": vest_ctx,
            "goal_rows": goal_rows,
            "recon_rows": recon_rows,
            "holdings": hold, "hold_rows": hold_rows, "pricing": pricing,
            "holdings_total": money(hold.get("total"), 2) if hold.get("total") else None,
            "gross_monthly": f"${gross:,}", "comp": comp, "charts": charts,
            "ss": ss, "ss_table": ss_table, "mort": mort, "tax_cards": tax_cards,
            "esc": esc_ctx, "k401": k401,
            "pj": pj, "mc": mc, "recs": a["recommendations"],
            "expense_bridge": expense_bridge, "annual_expenses": annual_expenses,
            "health_transitions": health_transitions, "stress_rows": stress_rows,
            "risk_rows": risk_rows, "risk_model": rm, "mc_metrics": mc_metrics,
            "assumptions": pj["assumptions"], "tax_year": a.get("tax_year"),
            "data_gaps": a.get("data_gaps") or [], "gaps": gaps,
            "est_sections": est_sections, "nav": nav,
            "verdict": verdict, "key_assumptions": key_assumptions,
            "generated_at": a.get("generated", "")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--analysis", default=str(common.ANALYSIS_PATH))
    ap.add_argument("--profile", default=str(common.PROFILE_PATH))
    ap.add_argument("--out", default=str(common.DASHBOARD_PATH))
    args = ap.parse_args()

    a = common.load_json(Path(args.analysis))
    profile = common.load_json(Path(args.profile))
    if not a or not profile:
        diag("[render] missing analysis.json or profile.json — run analyze first")
        return 1
    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      autoescape=select_autoescape(["html", "j2"]))
    html = env.get_template("finance.html.j2").render(**build_context(a, profile))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    diag(f"[render] wrote {out.name} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
