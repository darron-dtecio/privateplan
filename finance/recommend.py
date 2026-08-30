"""Rule-based recommendations computed from the analysis results."""

from __future__ import annotations

import projection
import tax
import taxdata as td


def _find(results: dict, *keys, default=None):
    cur = results
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def run(profile: dict, p: dict, results: dict) -> list[dict]:
    """profile = raw profile, p = projection.prepare(profile), results = analysis dict."""
    recs: list[dict] = []
    year0 = p["start_year"]
    age0 = year0 - p["birth_year"]

    # -- 401k contribution headroom -------------------------------------------
    limit_now = projection._k401_limit(age0)
    elected = p["k401_elected"]
    k = results.get("k401") or {}
    if elected < limit_now - 500:
        gap = limit_now - elected
        regular_gap = max((k.get("base_limit") or td.K401_LIMIT) - elected, 0)
        roth_gap = max(gap - regular_gap, 0)
        pct_bits = ""
        if (k.get("needed_pct") is not None and k.get("current_pct") is not None
                and k.get("needed_per_period") is not None
                and k.get("per_period") is not None):
            pct_bits = (f" Raising the election from {k['current_pct']}% to about "
                        f"{k['needed_pct']}% of base "
                        f"(${k['needed_per_period']:,.0f} a paycheck instead of "
                        f"${k['per_period']:,.0f}) reaches it.")
        recs.append({
            "id": "k401-headroom", "severity": "action",
            "title": f"You have ${gap:,.0f}/yr of unused 401(k) headroom",
            "detail": (f"Your elective deferral is ${elected:,.0f}/yr against a "
                       f"${limit_now:,.0f} employee limit at age {age0} "
                       f"(${k.get('base_limit', 0):,} base + "
                       f"${k.get('catchup', 0):,} catch-up). "
                       "The employer match does NOT count toward that limit — it "
                       f"falls under the separate §415(c) total-additions cap of "
                       f"${k.get('total_additions_limit', 0):,}, so the match cannot "
                       "close this gap for you." + pct_bits +
                       f" Of the unused amount, ${regular_gap:,.0f} is regular deferral "
                       f"headroom and ${roth_gap:,.0f} is catch-up headroom that must be "
                       "Roth when the wage threshold applies. Only the pre-tax election "
                       "reduces today's taxable income."),
            "impact_usd": round(gap)})
    if k.get("roth_catchup_required"):
        recs.append({
            "id": "roth-catchup", "severity": "info",
            "title": f"Your ${k.get('catchup', 0):,} catch-up must be Roth, not pre-tax",
            "detail": (f"SECURE 2.0 requires catch-up contributions to be designated "
                       f"Roth once prior-year wages from the employer exceed "
                       f"${k.get('roth_threshold', 0):,}, which yours do. That is not a "
                       "reason to skip it — the dollars still go in, they are simply "
                       "taxed now and grow tax-free, which is a reasonable trade given "
                       "you expect a lower bracket in retirement. Just check the plan "
                       "election is set up, since some payrolls silently cap deferrals "
                       "at the base limit if no Roth catch-up election exists."),
            "impact_usd": None})

    # -- super catch-up window --------------------------------------------------
    win_start, win_end = p["birth_year"] + 60, p["birth_year"] + 63
    recs.append({
        "id": "super-catchup", "severity": "info",
        "title": f"Super catch-up window: {win_start}–{win_end} (ages 60–63)",
        "detail": (f"The official 2026 limit raises the age-60-to-63 catch-up to ${td.SUPER_CATCHUP_60_63:,} "
                   f"(vs ${td.CATCHUP_50:,}) for tax years {win_start}–{win_end} — your "
                   "final full working years. Limits for later years are indexed estimates, "
                   "not fixed published amounts. If your prior-year wages exceed "
                   "$145k (indexed), catch-up contributions must be Roth from 2026."),
        "impact_usd": round((td.SUPER_CATCHUP_60_63 - td.CATCHUP_50) * 4)})

    # -- mortgage payoff vs invest ----------------------------------------------
    mrate = _find(results, "mortgage", "rate")
    if mrate is not None:
        base_r = p["mean_return"]
        payoff = next((s for s in _find(results, "mortgage", "scenarios", default=[])
                       if "Lump-sum" in s["name"]), None)
        if mrate >= base_r - 0.005:
            detail = (f"Your mortgage rate ({mrate:.2%}) is close to or above the "
                      f"assumed portfolio return ({base_r:.2%}) — paying it down early "
                      "is a risk-free return that beats the expected market return.")
            sev = "action"
        else:
            detail = (f"Your mortgage rate ({mrate:.2%}) is below the assumed portfolio "
                      f"return ({base_r:.2%}); mathematically, investing wins on average, "
                      "but paying off before retirement removes a fixed cost and "
                      "sequence-of-returns exposure.")
            sev = "info"
        if payoff and payoff.get("interest_saved"):
            detail += (f" Paying off at retirement would need "
                       f"${payoff['cash_required']:,.0f} cash and save "
                       f"${payoff['interest_saved']:,.0f} of remaining interest.")
        recs.append({"id": "mortgage-vs-invest", "severity": sev,
                     "title": "Mortgage payoff vs invest", "detail": detail,
                     "impact_usd": round(payoff["interest_saved"]) if payoff else None})

    # -- SS claiming --------------------------------------------------------------
    bes = _find(results, "social_security", "breakevens", default=[])
    be_67_70 = next((b["breakeven_age"] for b in bes
                     if b["early"] == 67 and b["late"] == 70), None)
    recs.append({
        "id": "ss-claiming", "severity": "info",
        "title": "Social Security: delaying likely pays for a married couple",
        "detail": ("Retiring at 65 doesn't require claiming at 65 — bridging 2 years "
                   "from the portfolio and claiming at 67 (or 70) buys an 8%/yr "
                   "inflation-protected annuity. "
                   + (f"The 67-vs-70 breakeven is about age {be_67_70}; " if be_67_70 else "")
                   + "because the survivor keeps the larger check, joint life "
                   "expectancy usually favors the higher earner delaying."),
        "impact_usd": None})

    # -- Roth conversion window -----------------------------------------------------
    recs.append({
        "id": "roth-window", "severity": "info",
        "title": "Roth-conversion window: retirement (65) to RMDs (75)",
        "detail": ("Between retiring and RMD age 75 — especially before claiming SS — "
                   "your taxable income drops; converting traditional dollars to Roth "
                   "up to the top of the 12% or 22% bracket can cut lifetime taxes and "
                   "future RMDs. Watch IRMAA (2-yr lookback from 63 for Medicare at 65) "
                   "and note CA taxes conversions as ordinary income."),
        "impact_usd": None})

    # -- success probability ----------------------------------------------------------
    prob = _find(results, "monte_carlo", "success_prob")
    gap = _find(results, "projection", "gap_monthly")
    if prob is not None:
        if prob >= 0.85:
            recs.append({"id": "success", "severity": "info",
                         "title": f"Plan success probability: {prob:.0%}",
                         "detail": "At or above the common 85% comfort threshold under "
                                   "current assumptions.", "impact_usd": None})
        else:
            fixes = []
            if gap and gap > 0:
                fixes.append(f"trim desired spending by about ${gap:,.0f}/mo")
            fixes.append("work 12–24 months longer")
            fixes.append("shift one notch more conservative on spending flexibility")
            sev = "warning" if prob < 0.7 else "action"
            recs.append({"id": "success", "severity": sev,
                         "title": f"Plan success probability: {prob:.0%} — below 85%",
                         "detail": "Levers, in order of impact: " + "; ".join(fixes) + ".",
                         "impact_usd": None})

    # -- cash flow -------------------------------------------------------------
    cf = _find(results, "snapshot", "cashflow", default={}) or {}
    surplus = cf.get("surplus_monthly")
    sd = results.get("spending_detail") or {}
    if surplus is not None and surplus < 0:
        recs.append({
            "id": "negative-cashflow", "severity": "warning",
            "title": f"Spending exceeds take-home pay by ${abs(surplus):,.0f}/mo",
            "detail": (f"Take-home is about ${cf.get('net_monthly', 0):,.0f}/mo after tax "
                       f"and pre-tax deductions, against ${cf.get('expenses_monthly', 0):,.0f}/mo "
                       "of actual spending — the difference is already being funded from "
                       "the portfolio while you are still working. That is survivable "
                       "with your asset base, but it means the accumulation years are "
                       "not accumulating outside the 401(k). Closing the gap before 2032 "
                       "is the single highest-leverage change available."),
            "impact_usd": round(abs(surplus) * 12)})
    if sd.get("avg_monthly") and sd.get("avg_monthly_recent12"):
        drift = sd["avg_monthly_recent12"] - sd["avg_monthly"]
        if drift > sd["avg_monthly"] * 0.05:
            recs.append({
                "id": "spending-drift", "severity": "action",
                "title": f"Spending is trending up: last 12 months run "
                         f"${drift:,.0f}/mo above the {sd.get('source_months')}-month average",
                "detail": (f"Recent 12-month average ${sd['avg_monthly_recent12']:,.0f}/mo vs "
                           f"${sd['avg_monthly']:,.0f}/mo across the whole history. The plan "
                           "uses the recent figure, which is the conservative choice, but "
                           "worth confirming the increase is deliberate rather than drift."),
                "impact_usd": round(drift * 12)})

    # -- 401(k) plan loan ---------------------------------------------------------
    ia = results.get("investment_activity") or {}
    if ia.get("loan_active"):
        recs.append({
            "id": "plan-loan", "severity": "action",
            "title": f"Active 401(k) loan — repaying about "
                     f"${ia['loan_repayment_monthly']:,.0f}/mo",
            "detail": ("A plan loan removes that balance from the market while it is "
                       "outstanding, and it is repaid with after-tax dollars that get "
                       "taxed again at withdrawal. The bigger risk at your stage is "
                       "timing: an unpaid balance when you separate from the employer "
                       "is treated as a taxable distribution, so it should be cleared "
                       "well before you retire in 2032. The outstanding balance is not "
                       "in the activity export — add it in the intake form."),
            "impact_usd": round(ia["loan_repayment_monthly"] * 12)})
    elif ia.get("loan_payoff_detected"):
        recs.append({
            "id": "plan-loan-cleared", "severity": "info",
            "title": "401(k) loan is paid off — nothing outstanding",
            "detail": (f"Repayments ran through {ia['loan_last_repayment_month']}, "
                       f"ending with a final payment of "
                       f"${ia['loan_final_payment']:,.0f} against a usual "
                       f"${ia['loan_typical_monthly_repayment']:,.0f}, followed by "
                       f"closing maintenance fees and "
                       f"{ia['loan_months_since_last_repayment']} months with no "
                       "further activity. That timing lines up with the rollover — a "
                       "plan balance cannot be rolled over with a loan outstanding. "
                       "No liability is carried in the plan and the "
                       "separation-from-service tax trap no longer applies."),
            "impact_usd": None})

    # -- lumpy costs excluded from the baseline -----------------------------------
    if sd.get("one_offs"):
        biggest = sd["one_offs"][0]
        recs.append({
            "id": "capital-replacements", "severity": "info",
            "title": "Big-ticket replacements are outside the spending baseline — "
                     "keep a sinking fund",
            "detail": (
                f"${sd.get('one_off_total', 0):,.0f} of confirmed non-recurring items "
                f"(largest: {biggest['description'].strip()} at "
                f"${biggest['amount']:,.0f}) are excluded from the run rate, so the "
                "plan is not projecting them forward every year. Future home replacements "
                f"are covered by the modeled {p.get('home_reserve_pct', 0):.1%} annual "
                "home-value reserve; vehicle replacements are modeled as dated capital "
                "events. Keep those reserves liquid enough for their scheduled use."),
            "impact_usd": None})

    # -- fund fees ------------------------------------------------------------------
    fa = results.get("fund_analysis") or {}
    if fa.get("annual_fee_total"):
        dear = [f for f in fa.get("funds", [])
                if (f.get("vs_category") or 0) > 0.001]
        dear.sort(key=lambda f: -(f.get("annual_fee") or 0))
        detail = (f"The fund sleeve costs about "
                  f"${fa['annual_fee_total']:,.0f} a year at a weighted "
                  f"{fa['weighted_expense']:.2%} expense ratio — charged whether the "
                  "funds perform or not.")
        if dear:
            names = ", ".join(f"{f['symbol']} ({f['expense_ratio']:.2%})"
                              for f in dear[:3])
            cost = sum(f.get("annual_fee") or 0 for f in dear)
            detail += (f" {len(dear)} fund(s) sit above their category average — "
                       f"{names} — together ${cost:,.0f}/yr. Those are the ones that "
                       "need a specific reason to stay, since a cheaper index "
                       "equivalent almost always exists.")
        recs.append({
            "id": "fund-fees",
            "severity": "action" if dear else "info",
            "title": f"Fund fees run ${fa['annual_fee_total']:,.0f}/yr "
                     f"({fa['weighted_expense']:.2%} weighted)",
            "detail": detail,
            "impact_usd": round(fa["annual_fee_total"])})

    # -- advisory fees ----------------------------------------------------------------
    adv = results.get("advisory_fees") or {}
    if adv.get("fee_total_annual"):
        drag = (adv.get("drag_retirement") or {}).get("drag")
        detail = (f"${adv['fee_total_annual']:,.0f} a year comes out of "
                  f"${adv['advised_value']:,.0f} of advised accounts — a blended "
                  f"{adv['blended_rate']:.2%}, measured from the fee charges on your "
                  f"statements, not a published schedule. On top of fund expenses the "
                  f"advisor has to beat a passive portfolio of the same risk by "
                  f"{adv['weighted_breakeven']:.2%} a year just to leave you level.")
        if adv.get("years_to_detect"):
            detail += (f" Proving an edge that size against normal tracking error would "
                       f"take about {adv['years_to_detect']:,.0f} years of returns, so "
                       "the question is what else the fee buys — planning, tax work, "
                       "someone to call in a drawdown — priced against buying those "
                       "directly.")
        # A billed losing quarter is the concrete version of the argument: the
        # fee does not participate in the downside it is charged against.
        losses = [p for a in (adv.get("accounts") or [])
                  for p in (a.get("periods") or []) if p.get("loss")]
        if losses:
            billed = sum(p["fee"] for p in losses)
            lost = abs(sum(p["total_return"] for p in losses))
            n = len(losses)
            detail += (f" {n} billing period{'' if n == 1 else 's'} on record "
                       f"lost money and {'was' if n == 1 else 'were'} charged "
                       f"anyway — ${billed:,.0f} on ${lost:,.0f} of losses, which "
                       f"is the fee's own shape: fixed on the way down, a small "
                       f"share on the way up.")
        if drag:
            detail += (f" Left as is, the fee alone gives up about ${drag:,.0f} of "
                       "portfolio value by retirement.")
        recs.append({
            "id": "advisory-fees", "severity": "action",
            "title": f"Advisory fees run ${adv['fee_total_annual']:,.0f}/yr "
                     f"({adv['blended_rate']:.2%} of advised assets)",
            "detail": detail,
            "impact_usd": round(adv["fee_total_annual"])})

    # -- look-through concentration ---------------------------------------------------
    look = fa.get("look_through") or []
    hidden = [e for e in look if e.get("direct") and e.get("via_funds")]
    if hidden:
        top = max(look, key=lambda e: e["total"])
        recs.append({
            "id": "look-through", "severity": "info",
            "title": f"Look-through: {top['symbol']} is "
                     f"{top['pct_portfolio']:.1%} of the portfolio once funds are unwrapped",
            "detail": (f"{len(hidden)} companies are owned both directly and inside your "
                       "index funds, so the real position is larger than the direct "
                       "holding suggests. Fund providers publish only their biggest "
                       f"positions ({fa.get('look_through_coverage', 0):.0%} of the "
                       "sleeve here), so treat these as a floor on true exposure, not a "
                       "ceiling."),
            "impact_usd": None})

    # -- single-position concentration -------------------------------------------
    hold = _find(results, "snapshot", "holdings", default={}) or {}
    top_pct = hold.get("top_pct_of_portfolio")
    if top_pct and top_pct >= 0.15 and hold.get("top"):
        top = hold["top"][0]
        recs.append({
            "id": "concentration", "severity": "action",
            "title": f"Concentrated position: {top['symbol']} is "
                     f"{top_pct:.0%} of your investable portfolio",
            "detail": (f"{top['symbol']} (~${top['value']:,.0f}) exceeds the common "
                       "10–15% single-position guideline. Six years from retirement, "
                       "a drawdown in one name hits harder — consider trimming toward "
                       "diversified funds, mindful of capital-gains timing (a lower-"
                       "income year, or post-retirement pre-SS window, cuts the tax)."),
            "impact_usd": round(top["value"])})

    # -- sequence of returns ---------------------------------------------------------
    if profile.get("assumptions", {}).get("risk") == "aggressive":
        recs.append({
            "id": "sequence-risk", "severity": "action",
            "title": "Sequence-of-returns risk: consider a glidepath before 2032",
            "detail": ("An aggressive allocation into a Feb-2032 retirement date means a "
                       "bad 2030–2033 could permanently impair the plan. A 2–3 year "
                       "cash/bond spending buffer or gradual de-risking from age 62 "
                       "materially reduces failure modes."),
            "impact_usd": None})

    # -- healthcare bridge -----------------------------------------------------------
    spouse_age_retire = p["retirement_year"] - p["spouse_birth_year"]
    recs.append({
        "id": "medicare", "severity": "info",
        "title": "Healthcare bridge is explicitly funded",
        "detail": (f"At retirement you are Medicare-eligible while your spouse is about "
                   f"{spouse_age_retire}. The baseline applies the configured pre-Medicare "
                   "cost to the younger spouse until 65, Medicare plus out-of-pocket costs "
                   "separately to each enrollee, 6% healthcare inflation, and IRMAA using "
                   "projected MAGI from two years earlier."),
        "impact_usd": None})

    order = {"warning": 0, "action": 1, "info": 2}
    recs.sort(key=lambda r: order.get(r["severity"], 3))
    return recs


if __name__ == "__main__":
    prof = {
        "household": {"self_birthdate": "1978-04-12", "state": "CA",
                      "retirement_date": "2040-06"},
        "income": {"salary_annual": 150000, "k401_pct": 5, "employer_match_pct": 4},
        "assets": [{"type": "401k", "balance": 600000}],
        "social_security": {"self": {"67": 3000}, "claim_age_self": 67},
        "spending": {"retirement_monthly_today": 7000},
        "assumptions": {"risk": "aggressive", "horizon_age": 95},
    }
    import projection as pj
    p = pj.prepare(prof)
    results = {"k401": {"needed_pct": 14.75, "current_pct": 5, "per_period": 500,
                        "needed_per_period": 1354, "base_limit": 24500,
                        "catchup": 8000, "total_additions_limit": 72000,
                        "roth_catchup_required": True, "roth_threshold": 150000},
               "snapshot": {"cashflow": {"surplus_monthly": -1500, "net_monthly": 11000,
                                         "expenses_monthly": 12500}},
               "spending_detail": {"avg_monthly": 10000, "avg_monthly_recent12": 12500,
                                   "source_months": 25, "one_off_total": 40000,
                                   "one_offs": [{"description": "HVAC", "amount": 27000}]},
               "investment_activity": {
                   "has_plan_loan": True, "loan_active": False,
                   "loan_payoff_detected": True,
                   "loan_last_repayment_month": "2025-09",
                   "loan_final_payment": 4290.0,
                   "loan_typical_monthly_repayment": 500.0,
                   "loan_months_since_last_repayment": 9},
               "fund_analysis": {
                   "annual_fee_total": 4800.0, "weighted_expense": 0.0063,
                   "look_through_coverage": 0.46,
                   "funds": [{"symbol": "ABCFX", "expense_ratio": 0.0095,
                              "vs_category": 0.0019, "annual_fee": 800.0}],
                   "look_through": [{"symbol": "MSFT", "direct": 250000.0,
                                     "via_funds": 5000.0, "total": 255000.0,
                                     "pct_portfolio": 0.111}]},
               "advisory_fees": {
                   "fee_total_annual": 12000.00, "advised_value": 1200000.00,
                   "blended_rate": 0.01, "weighted_breakeven": 0.0145,
                   "years_to_detect": 59.9, "charges_harvested": 4,
                   "drag_retirement": {"drag": 150000.00},
                   "accounts": [{"periods": [
                       {"label": "Q1 2026", "fee": 3000.00,
                        "total_return": 9000.00, "loss": False},
                       {"label": "Q2 2026", "fee": 3000.00,
                        "total_return": -40000.00, "loss": True}]}]},
               "mortgage": {"rate": 0.0475, "scenarios": []},
               "social_security": {"breakevens": [
                   {"early": 67, "late": 70, "breakeven_age": 82}]},
               "monte_carlo": {"success_prob": 0.78},
               "projection": {"gap_monthly": 900}}
    recs = run(prof, p, results)
    ids = {r["id"] for r in recs}
    assert {"k401-headroom", "super-catchup", "ss-claiming", "roth-window",
            "success", "sequence-risk", "medicare", "negative-cashflow",
            "spending-drift", "plan-loan-cleared", "capital-replacements",
            "roth-catchup", "fund-fees", "look-through", "advisory-fees"} <= ids
    af = next(r for r in recs if r["id"] == "advisory-fees")
    # the rate has to be presented as measured, and the break-even is the point
    assert "on your statements" in af["detail"] and "1.45%" in af["detail"], af
    assert af["impact_usd"] == 12000
    # the losing quarter that was billed anyway is named, with both figures
    assert "$3,000 on $40,000 of losses" in af["detail"], af["detail"]
    # ...and stays out of the text when no period lost money
    quiet = dict(results["advisory_fees"],
                 accounts=[{"periods": [{"fee": 1.0, "total_return": 5.0,
                                         "loss": False}]}])
    af2 = next(r for r in run(prof, p, dict(results, advisory_fees=quiet))
               if r["id"] == "advisory-fees")
    assert "losses" not in af2["detail"], af2["detail"]
    # no advisory fee measured -> no rule, rather than a zero-dollar one
    assert not any(r["id"] == "advisory-fees" for r in
                   run(prof, p, dict(results, advisory_fees={})))
    hr = next(r for r in recs if r["id"] == "k401-headroom")
    assert "does NOT count toward that limit" in hr["detail"]
    assert "plan-loan" not in ids   # a cleared loan must not raise an action
    assert recs[0]["severity"] in ("warning", "action")  # sorted by severity
    assert recs[0]["id"] in ("negative-cashflow", "success")
    print("recommend self-test OK")
