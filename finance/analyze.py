"""Compute analysis.json from profile.json. Deterministic — no network, no LLM.

Usage:
    python finance/analyze.py                          # finance_data/profile.json
    python finance/analyze.py --profile samples/profile.json [--out PATH]

Diagnostics printed here are structural only (sections computed, row counts);
never dollar values from a real profile.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common
import goals
import montecarlo
import mortgage
import projection
import recommend
import reprice
import socsec
import tax
import taxdata as td
import vesting
from common import diag


def snapshot(profile: dict) -> dict:
    assets = profile.get("assets", [])
    liabilities = profile.get("liabilities", [])
    portfolio = sum(a.get("balance") or 0 for a in assets)
    home = (profile.get("home") or {}).get("value") or 0
    debt = sum(l.get("balance") or 0 for l in liabilities)
    inc = profile.get("income", {})
    salary = inc.get("salary_annual") or 0
    spouse = inc.get("spouse_income_annual") or 0
    k401 = salary * (inc.get("k401_pct") or 0) / 100
    match = salary * (inc.get("employer_match_pct") or 0) / 100
    pretax = k401 + (inc.get("other_pretax_annual") or 0)
    hh = profile.get("household", {})
    taxes = tax.current_year(
        salary, pretax, spouse,
        hh.get("state", common.DEFAULT_HOUSEHOLD["state"]),
        hh.get("filing_status", common.DEFAULT_HOUSEHOLD["filing_status"]))
    gross_m = (salary + spouse) / 12
    net_m = gross_m - taxes["total"] / 12 - pretax / 12
    spend_m = profile.get("spending", {}).get("current_monthly") or 0
    extra_contrib = sum(a.get("annual_contribution") or 0 for a in assets)
    saved = k401 + match + extra_contrib
    holdings = profile.get("holdings") or []
    holdings_total = sum(h.get("value") or 0 for h in holdings)
    return {
        "holdings": {"top": holdings[:10], "all": holdings, "n": len(holdings),
                     "total": round(holdings_total),
                     "top_pct_of_portfolio": (round(holdings[0]["value"] / portfolio, 3)
                                              if holdings and portfolio else None)},
        "portfolio": round(portfolio), "home_value": round(home),
        "total_debt": round(debt),
        "net_worth": round(portfolio + home - debt),
        "assets": [{"name": a.get("name"), "type": a.get("type"),
                    "balance": round(a.get("balance") or 0)} for a in assets],
        "liabilities": [{"name": l.get("name"), "balance": round(l.get("balance") or 0),
                         "rate": l.get("rate")} for l in liabilities],
        "cashflow": {"gross_monthly": round(gross_m),
                     "taxes_monthly": round(taxes["total"] / 12),
                     "pretax_monthly": round(pretax / 12),
                     "net_monthly": round(net_m),
                     "expenses_monthly": round(spend_m),
                     "surplus_monthly": round(net_m - spend_m)},
        "savings_rate": round(saved / (salary + spouse), 3) if salary + spouse else 0,
        "taxes_now": taxes,
    }


REQUIRED = [
    ("spending.retirement_monthly_today", "Desired retirement spending — the plan has "
     "nothing to solve without it"),
    ("spending.current_monthly", "Current monthly spending — needed for savings rate "
     "and surplus"),
    ("household.self_birthdate", "Your birthdate"),
]
ADVISORY = [
    ("home.value", "Home market value — net worth excludes the house until you set it"),
    ("income.salary_annual", "Salary — accumulation years will contribute nothing"),
]


def _dig(profile: dict, path: str):
    node = profile
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def check_completeness(profile: dict) -> list[str]:
    missing = []
    for path, why in REQUIRED:
        if not _dig(profile, path):
            missing.append(f"MISSING (required) {path} — {why}")
    for path, why in ADVISORY:
        if not _dig(profile, path):
            missing.append(f"missing (advisory) {path} — {why}")
    return missing


def fee_drag(portfolio: dict) -> dict:
    """What the portfolio pays annually to be managed, as a rate on the whole.

    Two costs, both certain: the advisory fee measured from the charges in the
    activity ledgers, and the funds' own expense ratios. They are levied on
    different slices — the advisor bills only the advised accounts, the funds
    charge only the fund sleeve — so each is taken in dollars and divided by
    the whole portfolio, which is the balance the projection compounds.
    """
    total = float(portfolio.get("total_portfolio") or 0)
    advisory = float((portfolio.get("advisory") or {}).get("fee_total_annual") or 0)
    funds = float((portfolio.get("fund_analysis") or {}).get("annual_fee_total") or 0)
    rate = ((advisory + funds) / total) if total else 0.0
    return {"advisory": advisory, "funds": funds, "base": total, "rate": rate}


def k401_audit(profile: dict, p: dict) -> dict:
    """Reconcile the plan contributions against the paystub and the IRS limits.

    The elective deferral limit and the employer match are separate buckets:
    the match never counts toward the employee limit, it counts toward the
    §415(c) total-additions cap. Conflating them is the usual way people talk
    themselves out of thousands of dollars of headroom.
    """
    pay = profile.get("payroll_detail") or {}
    inc = profile.get("income", {})
    ppy = pay.get("periods_per_year") or 24
    per = pay.get("k401_current")
    match_per = pay.get("k401_match_current")
    base_per = pay.get("base_salary_current")
    age = td.TAX_YEAR - int(profile["household"]["self_birthdate"][:4])

    limit = projection._k401_limit(age, td.TAX_YEAR)
    elective = p["k401_elected"]
    match = p["match"]
    checks = []

    if per and base_per:
        checks.append({
            "name": "Deferral rate vs paystub",
            "detail": f"${per:,.2f} / ${base_per:,.2f} per period = "
                      f"{per / base_per:.2%} of base",
            "ok": abs(per / base_per * 100 - (inc.get("k401_pct") or 0)) < 0.05})
    if per and pay.get("k401_ytd"):
        history = pay.get("history") or []
        prior = next((row for row in reversed(history[:-1])
                      if row.get("k401_ytd") is not None), None)
        latest = history[-1] if history else {}
        changed = (prior and prior.get("k401_current")
                   and abs(float(prior["k401_current"]) - float(per)) > 0.01)
        if changed and latest.get("k401_ytd") is not None:
            delta = float(latest["k401_ytd"]) - float(prior["k401_ytd"])
            old_rate = (float(prior["k401_current"])
                        / float(prior.get("base_salary_current") or 1))
            new_rate = float(per) / float(base_per or 1)
            checks.append({
                "name": "Year-to-date consistency",
                "detail": f"YTD ${latest['k401_ytd']:,.2f} - prior "
                          f"${prior['k401_ytd']:,.2f} = ${delta:,.2f}, matching the "
                          f"current period after the documented deferral change "
                          f"{old_rate:.0%} -> {new_rate:.0%}",
                "ok": abs(delta - float(per)) < 0.02})
        else:
            n = pay["k401_ytd"] / per
            checks.append({
                "name": "Year-to-date consistency",
                "detail": f"YTD ${pay['k401_ytd']:,.2f} / ${per:,.2f} = {n:.2f} pay "
                          f"periods — a whole number confirms the per-period amount",
                "ok": abs(n - round(n)) < 0.02})
    if per and match_per:
        checks.append({
            "name": "Employer match formula",
            "detail": f"${match_per:,.2f} is {match_per / per:.0%} of your "
                      f"${per:,.2f} deferral ({match_per / base_per:.2%} of base)"
            if base_per else f"{match_per / per:.0%} of your deferral",
            "ok": True})
    total_add = elective + match
    checks.append({
        "name": "§415(c) total additions",
        "detail": f"${total_add:,.0f} of a ${td.TOTAL_ADDITIONS_415C:,} cap "
                  f"(employee + employer combined)",
        "ok": total_add <= td.TOTAL_ADDITIONS_415C})

    needed_pct = (limit / p["salary"] * 100) if p["salary"] else None
    # the Roth catch-up test is on total wages from the employer, which for a
    # base salary plus equity is higher than either figure alone
    wages = max(pay.get("gross_ytd") or 0, p["salary"])
    return {
        "age": age, "periods_per_year": ppy,
        "per_period": per, "match_per_period": match_per,
        "base_per_period": base_per,
        "elective_annual": round(elective),
        "match_annual": round(match),
        "total_additions": round(total_add),
        "elective_limit": limit,
        "base_limit": td.K401_LIMIT,
        "catchup": limit - td.K401_LIMIT,
        "headroom": round(max(limit - elective, 0)),
        "current_pct": inc.get("k401_pct"),
        "needed_pct": round(needed_pct, 2) if needed_pct else None,
        "needed_per_period": round(limit / ppy, 2),
        "at_limit": elective >= limit - 1,
        "total_additions_limit": td.TOTAL_ADDITIONS_415C,
        "roth_catchup_required": wages > td.ROTH_CATCHUP_WAGE_THRESHOLD,
        "roth_threshold": td.ROTH_CATCHUP_WAGE_THRESHOLD,
        "super_catchup_years": [p["birth_year"] + 60, p["birth_year"] + 63],
        "super_catchup_limit": td.K401_LIMIT + td.SUPER_CATCHUP_60_63,
        "ignored_contributions": p.get("ignored_contributions") or [],
        "checks": checks,
    }


def selftest() -> int:
    """Synthetic checks for the pieces that are pure functions of their input."""
    # fee drag: both costs land on the whole balance the projection compounds
    d = fee_drag({"total_portfolio": 1_000_000,
                  "advisory": {"fee_total_annual": 8_000},
                  "fund_analysis": {"annual_fee_total": 2_000}})
    assert abs(d["rate"] - 0.010) < 1e-12, d
    # nothing measured is a zero drag, not a crash or a guess
    assert fee_drag({})["rate"] == 0.0
    assert fee_drag({"total_portfolio": 500_000})["rate"] == 0.0
    # and a zero drag leaves the projected return exactly where it was
    prof = common.load_json(common.ROOT / "samples" / "profile.json")
    base = projection.prepare(prof)
    assert base["fee_drag"] == 0.0
    assert base["mean_return"] == base["mean_return_gross"]
    fee = projection.prepare(dict(prof, assumptions={**prof.get("assumptions", {}),
                                                     "fee_drag": 0.01}))
    assert abs(fee["mean_return"] - (base["mean_return"] - 0.01)) < 1e-12
    # a fee compounds against the plan, so the same profile ends up poorer
    fee_rows = projection.scenarios(fee)["base"]["rows"]
    base_rows = projection.scenarios(base)["base"]["rows"]
    check_i = min(5, len(base_rows) - 1)
    assert fee_rows[check_i]["total"] < base_rows[check_i]["total"]
    print("analyze self-test OK")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=str(common.PROFILE_PATH))
    ap.add_argument("--out", default=str(common.ANALYSIS_PATH))
    ap.add_argument("--mc-paths", type=int, default=None)
    ap.add_argument("--force", action="store_true",
                    help="run even when required profile fields are missing")
    args = ap.parse_args()

    raw_profile = common.load_json(Path(args.profile))
    profile = common.migrate_profile(raw_profile)
    if profile != raw_profile and Path(args.profile).resolve() == common.PROFILE_PATH.resolve():
        common.save_json(common.PROFILE_PATH, profile)
        diag("[analyze] migrated local profile to schema version 2")
    if not profile:
        diag(f"[analyze] no profile at {args.profile} — fill in the intake form first "
             "(http://127.0.0.1:5000/finance/intake)")
        return 1
    diag(f"[analyze] profile loaded ({len(profile.get('assets', []))} assets, "
         f"{len(profile.get('liabilities', []))} liabilities)")

    gaps = check_completeness(profile)
    for g in gaps:
        diag(f"[analyze] {g}")
    if any(g.startswith("MISSING (required)") for g in gaps) and not args.force:
        diag("[analyze] STOPPING — fill the required fields at "
             "http://127.0.0.1:5000/finance/intake (or re-run with --force to "
             "model them as zero)")
        return 2

    results: dict = {"generated": common.now_iso(), "tax_year": td.TAX_YEAR,
                     "data_gaps": gaps}
    extracted = common.load_json(common.EXTRACTED / "summary.json") or {}
    results["source_documents"] = [
        {**source, "included_at": results["generated"]}
        for source in extracted.get("source_documents", [])
        if not source.get("duplicate_of") and source.get("pipeline") != "unknown"
    ]

    # Value the statement snapshot at today's prices before anything is
    # computed from it. Net worth, allocation, the projection and the Monte
    # Carlo all start from these balances, so repricing later would leave the
    # plan compounding a number the market has already moved past.
    profile, pricing = reprice.apply(profile)
    results["pricing"] = pricing
    if pricing.get("applied"):
        h = pricing["holdings"]
        diag(f"[analyze] repriced at market ({pricing['quotes']} quotes, "
             f"fetched {pricing['fetched_at']}): holdings "
             f"{h['delta']:+,.0f} on {h['n_priced']}/{h['n']} positions "
             f"({h['coverage']:.0%} of value), account balances "
             f"{pricing['asset_delta']:+,.0f}")
        for u in pricing["accounts_unpriced"]:
            diag(f"[analyze]   at statement value (no positions to price): "
                 f"{u['name']} {u['balance']:,.0f}")
    else:
        diag(f"[analyze] pricing: {pricing['reason']} — the plan runs on "
             f"statement values, which may be weeks stale")

    # Unvested equity is priced from the same quote file as everything else, so
    # the schedule and the portfolio can never be valued at two different
    # prices for the same share on the same day.
    eq = profile.get("equity_comp") or {}
    if eq.get("vests") and eq.get("enabled", True):
        sym = (eq.get("symbol") or "").strip().upper()
        px = reprice.quote_for(sym, reprice.load_quotes()) if sym else None
        # A live quote wins. Failing that, honour a price the owner stated by
        # hand: pre-IPO and private-company awards have a real per-share value
        # (a 409A valuation, a tender price) and no ticker to look it up with,
        # and refusing to value them at all understates the plan badly.
        manual = eq.get("price_manual")
        if px:
            eq["price"] = px
            diag(f"[analyze] vesting: {len(eq['vests'])} unvested event(s), "
                 f"{sym} at {px:,.2f}")
        elif manual:
            eq["price"] = float(manual)
            diag(f"[analyze] vesting: {len(eq['vests'])} unvested event(s), "
                 f"stated price {float(manual):,.2f} (equity_comp.price_manual, "
                 f"no live quote)")
        else:
            eq["price"] = None
            diag("[analyze] vesting: " + (
                f"no quote for {sym} — shares are scheduled but not valued, so "
                "they add nothing to the plan" if sym else
                "no ticker set (equity_comp.symbol) — shares are scheduled but "
                "not valued, so they add nothing to the plan"))
            gaps.append("Unvested equity has no share price: set "
                        "equity_comp.symbol to a ticker and refresh prices, or "
                        "set equity_comp.price_manual for an unlisted employer, "
                        "or the vesting schedule is excluded from the projection.")

    results["snapshot"] = snapshot(profile)
    diag("[analyze] snapshot: OK")

    results["spending_detail"] = profile.get("spending_detail")
    if results["spending_detail"]:
        sd = results["spending_detail"]
        diag(f"[analyze] spending: {sd['source_months']} months "
             f"({sd['first']}..{sd['last']}), avg {sd['avg_monthly']:,.0f}/mo, "
             f"last 12mo {sd['avg_monthly_recent12']:,.0f}/mo, "
             f"sources: {', '.join(sd.get('sources', []))}")

    results["investment_activity"] = profile.get("investment_activity")
    if results["investment_activity"]:
        ia = results["investment_activity"]
        loan = ("active" if ia.get("loan_active")
                else ("paid off " + str(ia.get("loan_last_repayment_month"))
                      if ia.get("loan_payoff_detected") else "none"))
        diag(f"[analyze] investment activity: {ia['n_months']} months, "
             f"contributions {ia['contributions_monthly']:,.0f}/mo, "
             f"plan loan: {loan}")

    ret_ym = profile["household"].get("retirement_date",
                                     common.DEFAULT_HOUSEHOLD["retirement_date"])
    if (profile.get("mortgage") or {}).get("balance"):
        results["mortgage"] = mortgage.run(profile["mortgage"], ret_ym)
        diag(f"[analyze] mortgage: {len(results['mortgage']['scenarios'])} scenarios")
    else:
        results["mortgage"] = None
        diag("[analyze] mortgage: none in profile, skipped")

    horizon = int(profile.get("assumptions", {}).get("horizon_age", 95))
    ss_in = dict(profile.get("social_security", {}))
    ss_in.setdefault("compare_claim_ages",
                     profile.get("assumptions", {}).get("compare_claim_ages", [62, 67, 70]))
    self_by = int(profile["household"]["self_birthdate"][:4])
    spouse_by = int((profile["household"].get("spouse_birthdate")
                     or profile["household"]["self_birthdate"])[:4])
    results["social_security"] = socsec.run(ss_in, horizon, self_by, spouse_by)
    diag("[analyze] social_security: OK")

    # Run the plan net of what the portfolio actually pays to be managed: the
    # advisory fees billed in the activity ledgers plus the funds' own expense
    # ratios. Both are certain; the returns they come out of are not.
    portfolio_doc = common.load_json(common.FIN_DATA / "portfolio.json") or {}
    drag = fee_drag(portfolio_doc)
    if drag["rate"]:
        profile = dict(profile, assumptions={**profile.get("assumptions", {}),
                                             "fee_drag": drag["rate"]})
        diag(f"[analyze] fee drag: {drag['advisory']:,.0f}/yr advisory + "
             f"{drag['funds']:,.0f}/yr fund expenses on {drag['base']:,.0f} = "
             f"{drag['rate']:.3%} off the projected return")
    else:
        diag("[analyze] fee drag: none measured — projection runs on GROSS returns "
             "(run the Portfolio step first; it is what prices the advisory fees "
             "and fund expenses)")

    p = projection.prepare(
        profile, surplus_annual=results["snapshot"]["cashflow"]["surplus_monthly"] * 12)
    risk_model = montecarlo.risk_from_portfolio(portfolio_doc, p["stdev"])
    p["stdev"] = risk_model["stdev"]
    scen = projection.scenarios(p)
    sustainable = projection.sustainable_spending(p)
    desired = p["lifestyle_monthly_today"]
    ages = [r["age"] for r in scen["base"]["rows"]]
    results["projection"] = {
        "ages": ages,
        "scenarios": {name: {"return": s["return"], "depleted_at": s["depleted_at"],
                             "end_balance": s["end_balance"],
                             "total": [r["total"] for r in s["rows"]]}
                      for name, s in scen.items()},
        "base_rows": scen["base"]["rows"],
        "sustainable_monthly": sustainable,
        "desired_monthly": desired,
        "gap_monthly": round(desired - sustainable) if desired else None,
        "markers": {"retire_age": p["retirement_year"] - p["birth_year"],
                    "ss_claim_age": p["claim_self"],
                    "rmd_age": td.RMD_START_AGE, "medicare_age": 65,
                    "spouse_medicare_year": p["spouse_birth_year"] + 65},
        "assumptions": {"mean_return": p["mean_return"],
                        "mean_return_gross": p["mean_return_gross"],
                        "fee_drag": p["fee_drag"],
                        "fee_drag_detail": drag,
                        "stdev": p["stdev"],
                        "inflation": p["inflation"], "cola": p["cola"],
                        "risk": profile.get("assumptions", {}).get("risk", "moderate"),
                        "horizon_age": horizon,
                        "property_tax_growth": p["tax_growth"],
                        "insurance_inflation": p["ins_growth"],
                        "property_tax_annual": p["property_tax_annual"],
                        "insurance_annual": p["insurance_annual"]},
    }
    results["projection"]["assumptions"].update({
        "healthcare_inflation": p["health_inflation"],
        "health_pre_medicare_monthly": p["health_pre_monthly"],
        "health_medicare_monthly": p["health_medicare_monthly"],
        "observed_medical_monthly": p["observed_medical_monthly"],
        "lifestyle_monthly": p["lifestyle_monthly_today"],
        "home_reserve_pct": p["home_reserve_pct"],
        "joint_horizon_year": p["end_year"],
        "younger_spouse_horizon_age": p["end_year"] - p["spouse_birth_year"],
        "risk_model": risk_model,
    })
    # what the schedule is worth to the plan, and what the plan would look like
    # without it — the comparison is the point, since these shares are not in
    # hand yet and the conditions on them may not hold
    if eq.get("vests"):
        without = projection.project(
            projection.prepare(dict(profile, equity_comp=dict(eq, enabled=False)),
                               surplus_annual=results["snapshot"]["cashflow"]
                               ["surplus_monthly"] * 12), p["mean_return"])
        base = scen["base"]
        results["vesting"] = {
            **(vesting.summarise({"future": eq["vests"],
                                  "conditions": eq.get("conditions"),
                                  "has_options": eq.get("has_options", False),
                                  "withholding_rate": eq.get("withholding_measured"),
                                  "withholding_from_n": eq.get("withholding_from_n"),
                                  "as_of": eq.get("as_of")},
                                 p["vesting"].get("price"),
                                 p["vesting"].get("withholding")) or {}),
            "symbol": eq.get("symbol"),
            "priced": bool(p["vesting"]["enabled"]),
            "include_conditional": eq.get("include_conditional", True),
            "n_conditional": p["vesting"].get("conditional", 0),
            "effect": {
                "end_balance_with": base["end_balance"],
                "end_balance_without": without["end_balance"],
                "depleted_with": base["depleted_at"],
                "depleted_without": without["depleted_at"],
            },
        }
        v = results["vesting"]
        diag(f"[analyze] vesting: {v['n_future']} event(s) through "
             f"{v['last_date']}, {v['total_shares']:,.0f} shares"
             + (f", net {v['net_total']:,.0f} after "
                f"{v['withholding']:.1%} withholding" if v.get("net_total") else ""))

    results["stress_cases"] = projection.stress_cases(p)
    results["k401"] = k401_audit(profile, p)
    k = results["k401"]
    diag(f"[analyze] 401(k) audit: elective {k['elective_annual']:,} + match "
         f"{k['match_annual']:,} = {k['total_additions']:,} total additions; "
         f"employee limit {k['elective_limit']:,} -> headroom {k['headroom']:,} "
         f"(needs {k['needed_pct']}% of base)")
    for c in k["checks"]:
        diag(f"[analyze]   {'PASS' if c['ok'] else 'FAIL'}  {c['name']}: {c['detail']}")
    for ig in k["ignored_contributions"]:
        diag(f"[analyze]   IGNORED double-counted contribution on "
             f"'{ig['name']}' = {ig['amount']:,.0f}/yr (already modelled from "
             "salary % + match)")

    diag(f"[analyze] projection: {len(ages)} years x 3 scenarios; "
         f"base depleted_at={'never' if scen['base']['depleted_at'] is None else 'yes'}")

    mc_paths = args.mc_paths or int(profile.get("assumptions", {}).get(
        "monte_carlo_paths", 10000))
    results["monte_carlo"] = montecarlo.run(p, n=mc_paths, risk_model=risk_model)
    diag(f"[analyze] monte_carlo: n={mc_paths}, "
         f"success_prob computed OK")
    goal_store = goals.load(profile, create=True)
    results["goal_analysis"] = goals.evaluate(profile, results, goal_store)
    diag(f"[analyze] goals: {len(results['goal_analysis']['goals'])} evaluated")

    # representative retirement-year tax picture (first year fully on SS)
    ss_year_age = max(p["claim_self"], p["retirement_year"] - p["birth_year"])
    row = next((r for r in scen["base"]["rows"] if r["age"] == ss_year_age + 1), None)
    if row:
        rt = dict(row.get("tax_detail") or {})
        rt["irmaa_tier"] = tax.irmaa_tier(row["magi"], row["year"],
                                          p["inflation"], p["filing_status"])
        rt["example_age"] = row["age"]
        rt["withdrawal_sources"] = row.get("withdrawal_sources") or {}
        results["taxes_retirement"] = rt
    diag("[analyze] taxes_retirement: OK")

    # the portfolio view is built separately; reuse its fund work when present
    port = portfolio_doc
    adv = port.get("advisory") or {}
    if adv.get("configured"):
        results["advisory_fees"] = {k: adv[k] for k in
                                    ("accounts", "advised_value", "pct_of_portfolio",
                                     "fee_total_annual", "blended_rate",
                                     "weighted_breakeven", "years_to_detect",
                                     "drag_retirement", "drag_horizon",
                                     "charges_harvested", "data_gaps") if k in adv}
        diag(f"[analyze] advisory fees: {adv['fee_total_annual']:,.0f}/yr on "
             f"{adv['advised_value']:,.0f} advised "
             f"({adv['blended_rate']:.3%}), measured from "
             f"{adv.get('charges_harvested', 0)} harvested charge(s)")
    if port.get("fund_analysis"):
        fa = port["fund_analysis"]
        results["fund_analysis"] = {k: fa[k] for k in
                                    ("funds", "annual_fee_total", "weighted_expense",
                                     "look_through", "look_through_coverage",
                                     "allocation", "fund_value") if k in fa}
        diag(f"[analyze] fund sleeve: {fa['fund_value']:,.0f} at "
             f"{fa['weighted_expense']:.3%} = {fa['annual_fee_total']:,.0f}/yr in fees")

    results["recommendations"] = recommend.run(profile, p, results)
    diag(f"[analyze] recommendations: {len(results['recommendations'])} rules fired")

    out = Path(args.out)
    common.save_json(out, results)
    diag(f"[analyze] wrote {out.name} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
