"""Deterministic retirement projection: accumulate to retirement, then draw down.

Annual model, nominal dollars. Buckets: taxable (brokerage/cash), tax-deferred
(401k/trad IRA/HSA), Roth. Withdrawal order taxable -> deferred -> Roth, with
RMDs forced from the deferred bucket at 75+.

Simplifications (echoed on the dashboard): retirement year is modeled as fully
retired; all withdrawals taxed as ordinary income (conservative for taxable-
account dollars); desired retirement spending EXCLUDES mortgage P&I, which the
model adds as a fixed nominal cost until the loan's payoff date.
"""

from __future__ import annotations

import mortgage
import common
import socsec
import tax
import taxdata as td
import vesting

# A non-qualified annuity grows tax-deferred but carries no RMD, so it is
# bucketed with taxable rather than deferred to avoid overstating forced
# distributions. Retype it to trad_ira in the intake form if it is held
# inside an IRA.
TAXABLE = ("brokerage", "cash", "other", "annuity")
DEFERRED = ("401k", "trad_ira")
ROTH = ("roth",)
HSA = ("hsa",)


def _bucket(asset_type: str) -> str:
    if asset_type in DEFERRED:
        return "deferred"
    if asset_type in HSA:
        return "hsa"
    if asset_type in ROTH:
        return "roth"
    return "taxable"


def _indexed_limit(value: float, year: int) -> float:
    if year <= td.TAX_YEAR:
        return value
    # Statutory retirement limits are announced annually in $500 increments.
    return round(value * (1 + td.INFLATION_DEFAULT) ** (year - td.TAX_YEAR) / 500) * 500


def _k401_limit(age: int, year: int = td.TAX_YEAR) -> float:
    base = _indexed_limit(td.K401_LIMIT, year)
    if age < 50:
        return base
    if 60 <= age <= 63:
        return base + _indexed_limit(td.SUPER_CATCHUP_60_63, year)
    return base + _indexed_limit(td.CATCHUP_50, year)


def _vesting_plan(profile: dict) -> dict:
    """Unvested equity, reduced to what the year loop needs: dollars per year.

    Priced at today's share price with no drift. Growing one company's stock at
    the plan's return assumption would compound a single position at the market
    rate on top of the portfolio that already does, and would make the whole
    plan lean on a forecast of one employer's share price.

    A vest is income first and an asset second, so what reaches the year loop
    is the gross (which is taxed as ordinary income) and the net (which is what
    can actually be invested).
    """
    eq = (profile.get("equity_comp") or {})
    vests = eq.get("vests") or []
    if not eq.get("enabled", True) or not vests:
        return {"enabled": False, "by_year": {}, "symbol": None}
    if not eq.get("include_conditional", True):
        vests = [v for v in vests if not v.get("condition")]
    price = eq.get("price")
    rate = eq.get("withholding")
    if rate is None:
        rate = eq.get("withholding_measured")
    if rate is None:
        rate = vesting.DEFAULT_WITHHOLDING
    by_year = vesting.by_year(vests, float(price) if price else None, float(rate))
    return {
        "enabled": bool(price),   # no price -> shares are known, dollars are not
        "symbol": eq.get("symbol"),
        "price": price,
        "withholding": rate,
        "by_year": {int(y): v for y, v in by_year.items()},
        "n_vests": len(vests),
        "conditional": sum(1 for v in vests if v.get("condition")),
    }


def prepare(profile: dict, surplus_annual: float = 0.0) -> dict:
    """surplus_annual: leftover cash after tax, pre-tax deductions and spending.
    Negative means the household already draws on the portfolio while working,
    which must be modelled or the accumulation phase is overstated."""
    profile = common.migrate_profile(profile)
    hh = profile["household"]
    birth_year = int(hh["self_birthdate"][:4])
    spouse_by = int(hh["spouse_birthdate"][:4]) if hh.get("spouse_birthdate") else birth_year
    birth_month = int(hh["self_birthdate"][5:7])
    spouse_bm = int(hh["spouse_birthdate"][5:7]) if hh.get("spouse_birthdate") else birth_month
    ret_ym = hh.get("retirement_date", common.DEFAULT_HOUSEHOLD["retirement_date"])
    a = profile.get("assumptions", {})
    risk = a.get("risk", "moderate")
    mean, stdev = td.RISK_PRESETS.get(risk, td.RISK_PRESETS["moderate"])
    if a.get("return_override"):
        mean = float(a["return_override"])
    # The preset returns are what the market delivers, not what lands in the
    # account. Advisory fees and fund expenses come out every year regardless
    # of how the market did, so the plan has to be run net of them or it
    # projects money that was already paid to somebody else. Supplied by the
    # caller (analyze.py) from the fees actually billed; 0 when unknown.
    fee_drag = float(a.get("fee_drag") or 0.0)
    mean_gross = mean
    mean -= fee_drag

    inc = profile.get("income", {})
    salary = float(inc.get("salary_annual") or 0)
    k401_elected = salary * float(inc.get("k401_pct") or 0) / 100

    buckets = {"taxable": 0.0, "deferred": 0.0, "roth": 0.0, "hsa": 0.0}
    contrib = {"taxable": 0.0, "deferred": 0.0, "roth": 0.0, "hsa": 0.0}
    taxable_basis = 0.0
    ignored: list[dict] = []
    for asset in profile.get("assets", []):
        b = _bucket(asset.get("type", "other"))
        buckets[b] += float(asset.get("balance") or 0)
        if b == "taxable":
            basis = asset.get("cost_basis")
            taxable_basis += (float(basis) if basis is not None
                              else (0.0 if a.get("taxable_basis_unknown_is_gain", True)
                                    else float(asset.get("balance") or 0)))
        extra = float(asset.get("annual_contribution") or 0)
        # A contribution typed onto the employer plan is the same money as the
        # salary-driven elective + match, so counting both doubles it.
        if extra and asset.get("type") == "401k" and k401_elected > 0:
            ignored.append({"name": asset.get("name"), "amount": extra})
            continue
        if asset.get("annual_contribution") is not None:
            contrib[b] += extra
    # dollars/yr (from the paystub) is unambiguous and wins; the percent field
    # means percent OF SALARY, which cannot express a "50% of contributions" plan
    if inc.get("employer_match_annual"):
        match = float(inc["employer_match_annual"])
    else:
        match = salary * float(inc.get("employer_match_pct") or 0) / 100

    ss = profile.get("social_security", {})
    claim_self = int(ss.get("claim_age_self") or 67)
    claim_spouse = int(ss.get("claim_age_spouse") or claim_self)
    self_monthly = socsec.benefit_at(claim_self, ss.get("self", {}))
    spouse_stmt = ss.get("spouse") or {}
    spouse_own = float(spouse_stmt.get("67")
                       or ss.get("spouse_own_monthly_fra") or 0)
    self_pia = float(ss.get("self", {}).get("67") or 0)
    worker_file_year = birth_year + claim_self
    spouse_age_at_worker_file = worker_file_year - spouse_by
    spouse_components = socsec.spouse_components(
        self_pia, spouse_own, claim_spouse, claim_self, spouse_age_at_worker_file,
        spouse_stmt)

    m = profile.get("mortgage") or {}
    # Property tax and hazard insurance ride inside the mortgage payment today
    # but outlive the loan, and they grow at their own rates — so they are
    # modelled as separate streams rather than folded into general spending.
    esc = m.get("escrow_detail") or {}
    tax_annual = float(esc.get("property_tax_annual") or 0)
    ins_annual = float(esc.get("insurance_annual") or 0)
    if not (tax_annual or ins_annual) and m.get("escrow_payment"):
        tax_annual = float(m["escrow_payment"]) * 12   # unsplit fallback

    pi_annual, payoff_year = 0.0, None
    if m.get("balance"):
        # assume the current payment behaviour (including any extra principal)
        # continues until the loan is gone
        extra = float(m.get("extra_monthly") or 0)
        rows = mortgage.amortize(m["balance"], m["rate"], m["pi_payment"],
                                 m.get("next_due") or "2026-08",
                                 extra_monthly=extra)
        pi_annual = (float(m["pi_payment"]) + extra) * 12
        payoff_year = int(rows[-1]["month"][:4]) if rows else None

    spend = profile.get("spending", {})
    observed_med = float(spend.get("observed_medical_monthly") or 0)
    retirement_total = float(spend.get("retirement_monthly_today") or 0)
    healthcare = profile.get("healthcare") or {}
    cap = profile.get("capital_expenses") or {}
    horizon_age = int(a.get("horizon_age", 95))
    end_year = max(birth_year + horizon_age, spouse_by + horizon_age)

    return {
        "start_year": td.TAX_YEAR,
        "end_year": end_year,
        "birth_year": birth_year, "birth_month": birth_month,
        "spouse_birth_year": spouse_by, "spouse_birth_month": spouse_bm,
        "retirement_year": int(ret_ym[:4]),
        "retirement_month": int(ret_ym[5:7]),
        "state": hh.get("state", common.DEFAULT_HOUSEHOLD["state"]),
        "filing_status": hh.get("filing_status",
                                common.DEFAULT_HOUSEHOLD["filing_status"]),
        "buckets": buckets, "taxable_basis": min(taxable_basis, buckets["taxable"]),
        "extra_contrib": contrib,
        "salary": salary, "k401_elected": k401_elected, "match": match,
        "ignored_contributions": ignored,
        "surplus_annual": float(surplus_annual or 0.0),
        "spend_ret_monthly_today": retirement_total,
        "lifestyle_monthly_today": max(retirement_total - observed_med, 0),
        "observed_medical_monthly": observed_med,
        "inflation": float(a.get("inflation") or td.INFLATION_DEFAULT),
        "cola": float(ss.get("cola") or td.SS_COLA_DEFAULT),
        "mean_return": mean, "mean_return_gross": mean_gross,
        "fee_drag": fee_drag, "stdev": stdev,
        "claim_self": claim_self, "claim_spouse": claim_spouse,
        "self_ss_monthly": self_monthly, "self_pia": self_pia,
        "spouse_own_pia": spouse_own,
        "spouse_own_monthly": spouse_components["own"],
        "spouse_topup_monthly": spouse_components["spousal_topup"],
        "pi_annual": pi_annual, "mortgage_payoff_year": payoff_year,
        "property_tax_annual": tax_annual, "insurance_annual": ins_annual,
        "tax_growth": float(a.get("property_tax_growth") or td.PROPERTY_TAX_GROWTH),
        "ins_growth": float(a.get("insurance_inflation") or td.INSURANCE_INFLATION),
        "health_enabled": healthcare.get("enabled", True),
        "health_pre_monthly": float(healthcare.get("pre_medicare_monthly_per_person") or 0),
        "health_medicare_monthly": float(healthcare.get("medicare_monthly_per_person") or 0),
        "health_inflation": float(healthcare.get("inflation") or 0.06),
        "home_value": float((profile.get("home") or {}).get("value") or 0),
        "home_reserve_pct": float(cap.get("home_reserve_pct") or 0),
        "home_reserve_inflation": float(cap.get("home_reserve_inflation") or 0.035),
        "scheduled_expenses": [s for s in cap.get("schedules", []) if s.get("enabled", True)],
        "vesting": _vesting_plan(profile),
        "working_magi": max(salary + float(inc.get("spouse_income_annual") or 0)
                            - float(inc.get("other_pretax_annual") or 0) - k401_elected, 0),
    }


def _active_months(year: int, start_year: int, start_month: int) -> int:
    if year < start_year:
        return 0
    return 13 - start_month if year == start_year else 12


def _benefit_months(year: int, birth_year: int, birth_month: int,
                    claim_age: int) -> int:
    return _active_months(year, birth_year + claim_age, birth_month)


def _health_for_year(p: dict, year: int, retired_start: int,
                     health_factor: float) -> tuple[float, int, dict]:
    if not p["health_enabled"] or not retired_start:
        return 0.0, 0, {"pre_medicare": 0.0, "medicare": 0.0}
    pre = med = 0.0
    medicare_months = 0
    for month in range(retired_start, 13):
        for by, bm in ((p["birth_year"], p["birth_month"]),
                       (p["spouse_birth_year"], p["spouse_birth_month"])):
            age_months = (year - by) * 12 + month - bm
            if age_months >= 65 * 12:
                med += p["health_medicare_monthly"] * health_factor
                medicare_months += 1
            else:
                pre += p["health_pre_monthly"] * health_factor
    return pre + med, medicare_months, {"pre_medicare": pre, "medicare": med}


def _scheduled_for_year(p: dict, year: int, inflation_factor: float,
                        retired_start: int) -> tuple[float, list[dict]]:
    total, events = 0.0, []
    for s in p["scheduled_expenses"]:
        first = s.get("first_date") or ""
        if len(first) < 4:
            continue
        first_year = int(first[:4])
        interval = s.get("interval_years")
        due = year == first_year or (interval and year > first_year
                                     and (year - first_year) % int(interval) == 0)
        if not due or (s.get("end_year") and year > int(s["end_year"])):
            continue
        custom = s.get("inflation")
        factor = ((1 + float(custom)) ** (year - p["start_year"])
                  if custom is not None else inflation_factor)
        amount = float(s.get("amount_today") or 0) * factor
        total += amount
        events.append({"name": s.get("name") or "Scheduled expense",
                       "amount": round(amount)})
    return total, events


def project(p: dict, returns, inflation_rates=None, health_inflation_rates=None,
            overrides: dict | None = None) -> dict:
    """Project household cash flows with source-aware withdrawals."""
    overrides = overrides or {}
    bk = dict(p["buckets"])
    basis = float(p.get("taxable_basis") or 0)
    years = list(range(p["start_year"], p["end_year"] + 1))
    rows, depleted_at = [], None
    infl_factor = health_factor = cola_factor = 1.0
    magi_history: dict[int, float] = {}
    min_liquid = float("inf")

    for idx, year in enumerate(years):
        r = returns[idx] if hasattr(returns, "__getitem__") else returns
        inf_rate = (inflation_rates[idx] if inflation_rates is not None else p["inflation"])
        health_rate = (health_inflation_rates[idx]
                       if health_inflation_rates is not None else p["health_inflation"])
        age = year - p["birth_year"]
        sp_age = year - p["spouse_birth_year"]
        retired_start = (p["retirement_month"] if year == p["retirement_year"]
                         else (1 if year > p["retirement_year"] else 0))
        retired_months = (13 - retired_start) if retired_start else 0
        work_fraction = (12 - retired_months) / 12
        ret_fraction = retired_months / 12

        contributions = withdrawal = ss_annual = taxes = spending = 0.0
        vest_gross = vest_net = 0.0
        withdrawal_sources = {"taxable": 0, "taxable_gain": 0, "deferred": 0,
                              "roth": 0, "hsa": 0, "forced_rmd": 0}
        tax_detail = {"total": 0, "magi": magi_history.get(year, 0)}
        components = {k: 0.0 for k in ("lifestyle", "healthcare", "mortgage_pi",
                                       "property_tax", "insurance", "home_reserve",
                                       "scheduled", "irmaa", "long_term_care")}
        healthcare_detail = {"pre_medicare": 0.0, "medicare": 0.0}
        events: list[dict] = []

        # Equity vests land on their own dates, not on the retirement date, so
        # they are applied whether or not the year is a working one — a
        # schedule running past retirement still pays out. The gross is
        # ordinary income in the year it vests; only the net can be invested.
        vslot = p["vesting"]["by_year"].get(year) if p["vesting"]["enabled"] else None
        if vslot and vslot["gross"]:
            vest_gross = vslot["gross"]
            vest_net = vslot["net"]
            bk["taxable"] += vest_net
            # shares arrive already taxed at their vest-day value, so that
            # value is their cost basis — taxing it again on withdrawal would
            # double-count
            basis += vest_net
            events.append({"name": f"Equity vest ({vslot['shares']:,.0f} shares)",
                           "amount": round(vest_net)})

        if work_fraction:
            k401 = min(p["k401_elected"], _k401_limit(age, year)) * work_fraction
            total_limit = _indexed_limit(td.TOTAL_ADDITIONS_415C, year)
            employer = min(p["match"] * work_fraction, max(total_limit - k401, 0.0))
            surplus = p["surplus_annual"] * infl_factor * work_fraction
            contributions = ((k401 + employer) + sum(p["extra_contrib"].values())
                             * work_fraction + surplus)
            bk["deferred"] += k401 + employer + p["extra_contrib"]["deferred"] * work_fraction
            bk["roth"] += p["extra_contrib"]["roth"] * work_fraction
            bk["hsa"] += p["extra_contrib"]["hsa"] * work_fraction
            taxable_add = p["extra_contrib"]["taxable"] * work_fraction + surplus
            if taxable_add >= 0:
                bk["taxable"] += taxable_add
                basis += taxable_add
            else:
                draw = min(-taxable_add, bk["taxable"])
                ratio = basis / bk["taxable"] if bk["taxable"] else 0
                bk["taxable"] -= draw
                basis -= draw * ratio
            magi_history[year] = p["working_magi"] * work_fraction + vest_gross

        components["scheduled"], scheduled_events = _scheduled_for_year(
            p, year, infl_factor, retired_start)
        events.extend(scheduled_events)

        if retired_months:
            self_months = _benefit_months(year, p["birth_year"], p["birth_month"],
                                          p["claim_self"])
            spouse_own_months = _benefit_months(
                year, p["spouse_birth_year"], p["spouse_birth_month"], p["claim_spouse"])
            worker_start_year = p["birth_year"] + p["claim_self"]
            worker_start_month = p["birth_month"]
            spouse_start_year = p["spouse_birth_year"] + p["claim_spouse"]
            spouse_start_month = p["spouse_birth_month"]
            top_year, top_month = max((worker_start_year, worker_start_month),
                                      (spouse_start_year, spouse_start_month))
            top_months = _active_months(year, top_year, top_month)
            ss_multiplier = float(overrides.get("ss_multiplier", 1.0))
            self_ss = p["self_ss_monthly"] * self_months * cola_factor
            spouse_own = p["spouse_own_monthly"] * spouse_own_months * cola_factor
            spouse_top = p["spouse_topup_monthly"] * top_months * cola_factor
            ss_annual = (self_ss + spouse_own + spouse_top) * ss_multiplier

            components["lifestyle"] = (p["lifestyle_monthly_today"] * retired_months
                                       * infl_factor)
            health, medicare_months, health_parts = _health_for_year(
                p, year, retired_start, health_factor)
            components["healthcare"] = health
            if p["mortgage_payoff_year"] and year <= p["mortgage_payoff_year"]:
                components["mortgage_pi"] = p["pi_annual"] * ret_fraction
            yrs = year - p["start_year"]
            components["property_tax"] = (p["property_tax_annual"]
                                           * (1 + p["tax_growth"]) ** yrs * ret_fraction)
            components["insurance"] = (p["insurance_annual"]
                                        * (1 + p["ins_growth"]) ** yrs * ret_fraction)
            components["home_reserve"] = (p["home_value"] * p["home_reserve_pct"]
                                           * (1 + p["home_reserve_inflation"]) ** yrs
                                           * ret_fraction)
            if overrides.get("ltc"):
                ltc = overrides["ltc"]
                start = p["spouse_birth_year"] + int(ltc.get("start_age", 85))
                if start <= year < start + int(ltc.get("years", 3)):
                    components["long_term_care"] = (float(ltc.get("annual_today", 120000))
                                                     * health_factor)

            n65 = int(medicare_months > 0) + int(medicare_months > 12)
            prior_magi = magi_history.get(year - 2, p["working_magi"])
            components["irmaa"] = tax.irmaa_surcharge(
                prior_magi, year, medicare_months / 12, p["inflation"],
                p["filing_status"])
            spending = sum(components.values())

            # Qualified healthcare comes from HSA first and never creates an RMD.
            hsa_take = min(bk["hsa"], components["healthcare"] + components["long_term_care"])
            required = max(spending - ss_annual - hsa_take, 0.0)
            target = required
            take_tax = take_def = take_roth = forced = taxable_gain = 0.0
            tax_result = {"total": 0, "magi": 0}
            shortfall = 0.0
            # Fixed-point gross-up for income tax. At retirement tax rates the
            # residual contracts quickly; 16 iterations reaches sub-dollar
            # precision while keeping 10,000-path simulations practical.
            for _ in range(16):
                remaining = target
                take_tax = min(remaining, bk["taxable"])
                remaining -= take_tax
                take_def = min(remaining, bk["deferred"])
                remaining -= take_def
                take_roth = min(remaining, bk["roth"])
                remaining -= take_roth
                gain_ratio = max(bk["taxable"] - basis, 0) / bk["taxable"] if bk["taxable"] else 0
                taxable_gain = take_tax * gain_ratio
                rmd_req = tax.rmd(bk["deferred"], age)
                forced = max(rmd_req - take_def, 0.0)
                tax_result = tax.retirement_sources(
                    take_def + forced, taxable_gain, ss_annual, n65, year,
                    p["state"], p["inflation"], p["filing_status"])
                cash = take_tax + take_def + take_roth + forced + hsa_take + ss_annual - tax_result["total"]
                shortfall = spending - cash
                if shortfall <= 1 or remaining > 1:
                    break
                target += shortfall

            before_taxable = bk["taxable"]
            principal_ratio = basis / before_taxable if before_taxable else 0
            bk["taxable"] -= take_tax
            basis = max(basis - take_tax * principal_ratio, 0)
            bk["deferred"] -= take_def + forced
            bk["roth"] -= take_roth
            bk["hsa"] -= hsa_take
            withdrawal = take_tax + take_def + take_roth + forced + hsa_take
            taxes = tax_result["total"]
            tax_detail = tax_result
            withdrawal_sources = {
                "taxable": round(take_tax), "taxable_gain": round(taxable_gain),
                "deferred": round(take_def + forced), "roth": round(take_roth),
                "hsa": round(hsa_take), "forced_rmd": round(forced)}
            cash = withdrawal + ss_annual - taxes
            excess = cash - spending
            if excess > 0:
                bk["taxable"] += excess
                basis += excess
            if shortfall > 1 and depleted_at is None:
                depleted_at = age
            # a vest after retirement is still W-2-style ordinary income, and
            # still counts toward the MAGI that sets IRMAA two years later
            magi_history[year] = tax_result["magi"] + vest_gross
            healthcare_detail = health_parts

        elif components["scheduled"]:
            # Dated purchases also occur during accumulation years. Fund them
            # from liquid accounts first without changing the saved profile.
            spending = components["scheduled"]
            remaining = spending
            before_taxable = bk["taxable"]
            take_tax = min(remaining, before_taxable)
            remaining -= take_tax
            principal_ratio = basis / before_taxable if before_taxable else 0
            bk["taxable"] -= take_tax
            basis = max(basis - take_tax * principal_ratio, 0)
            take_roth = min(remaining, bk["roth"])
            bk["roth"] -= take_roth
            remaining -= take_roth
            take_def = min(remaining, bk["deferred"])
            bk["deferred"] -= take_def
            remaining -= take_def
            withdrawal = take_tax + take_roth + take_def
            withdrawal_sources = {"taxable": round(take_tax), "taxable_gain": 0,
                                  "deferred": round(take_def), "roth": round(take_roth),
                                  "hsa": 0, "forced_rmd": 0}
            if remaining > 1 and depleted_at is None:
                depleted_at = age

        for b in bk:
            bk[b] = max(bk[b], 0.0) * max(1 + r, 0)
        basis = min(max(basis, 0.0), bk["taxable"])
        liquid = bk["taxable"] + bk["roth"] + bk["hsa"]
        min_liquid = min(min_liquid, liquid)
        rounded_components = {k: round(v) for k, v in components.items()}
        rows.append({"year": year, "age": age, "spouse_age": sp_age,
                     "total": round(sum(bk.values())), "liquid": round(liquid),
                     "taxable": round(bk["taxable"]), "taxable_basis": round(basis),
                     "deferred": round(bk["deferred"]), "roth": round(bk["roth"]),
                     "hsa": round(bk["hsa"]), "contributions": round(contributions),
                     "withdrawal": round(withdrawal), "ss": round(ss_annual),
                     "taxes": round(taxes), "spending": sum(rounded_components.values()),
                     "withdrawal_sources": withdrawal_sources,
                     "tax_detail": {k: round(v) if isinstance(v, (int, float)) else v
                                    for k, v in tax_detail.items()},
                     "spending_components": rounded_components,
                     "healthcare_detail": {k: round(v) for k, v in healthcare_detail.items()},
                     "events": events, "magi": round(magi_history.get(year, 0)),
                     "vest_gross": round(vest_gross), "vest_net": round(vest_net)})
        infl_factor *= 1 + inf_rate
        health_factor *= 1 + health_rate
        cola_factor *= 1 + p["cola"]
    return {"rows": rows, "depleted_at": depleted_at,
            "end_balance": rows[-1]["total"] if rows else 0,
            "min_liquid": round(min_liquid if min_liquid != float("inf") else 0)}


def scenarios(p: dict) -> dict:
    out = {}
    for name, r in (("base", p["mean_return"]),
                    ("optimistic", p["mean_return"] + td.SCENARIO_SPREAD),
                    ("pessimistic", p["mean_return"] - td.SCENARIO_SPREAD)):
        out[name] = {"return": r, **project(p, r)}
    return out


def sustainable_spending(p: dict) -> float:
    """Max adjustable lifestyle spending after fixed retirement obligations."""
    lo, hi = 0.0, 60_000.0
    for _ in range(40):
        mid = (lo + hi) / 2
        q = dict(p, lifestyle_monthly_today=mid)
        res = project(q, p["mean_return"])
        if res["depleted_at"] is None and res["end_balance"] >= 0:
            lo = mid
        else:
            hi = mid
    return round(lo, -1)


def stress_cases(p: dict) -> dict:
    years = p["end_year"] - p["start_year"] + 1
    base = [p["mean_return"]] * years
    sequence = list(base)
    start = max(p["retirement_year"] - p["start_year"], 0)
    for off, value in enumerate((-0.25, -0.10, 0.0)):
        if start + off < years:
            sequence[start + off] = value
    high_inf = [0.05 if start <= i < start + 10 else p["inflation"]
                for i in range(years)]
    high_health = [0.08 if start <= i < start + 10 else p["health_inflation"]
                   for i in range(years)]
    cases = {
        "sequence_shock": project(p, sequence),
        "high_inflation": project(p, base, high_inf, high_health),
        "social_security_minus_10pct": project(p, base, overrides={"ss_multiplier": 0.90}),
        "long_term_care": project(p, base, overrides={"ltc": {
            "start_age": 85, "years": 3, "annual_today": 120000}}),
        "combined": project(p, sequence, high_inf, high_health, overrides={
            "ss_multiplier": 0.90,
            "ltc": {"start_age": 85, "years": 3, "annual_today": 120000}}),
    }
    return {name: {"depleted_at": result["depleted_at"],
                   "end_balance": result["end_balance"],
                   "min_liquid": result["min_liquid"]}
            for name, result in cases.items()}


if __name__ == "__main__":
    prof = {
        "household": {"self_birthdate": "1978-04-12", "spouse_birthdate": "1979-09-30",
                      "state": "CA", "retirement_date": "2040-06"},
        "income": {"salary_annual": 150000, "k401_pct": 10, "employer_match_pct": 4},
        "assets": [{"type": "401k", "balance": 600000},
                   {"type": "brokerage", "balance": 120000, "annual_contribution": 6000},
                   {"type": "roth", "balance": 80000}],
        "social_security": {"self": {"62": 2100, "67": 3000, "70": 3720},
                            "spouse_own_monthly_fra": 300, "claim_age_self": 67},
        "mortgage": {"balance": 320000, "rate": 0.0475, "pi_payment": 1900,
                     "next_due": "2026-08"},
        "spending": {"retirement_monthly_today": 7000,
                     "observed_medical_monthly": 452},
        "home": {"value": 875000},
        "healthcare": {"enabled": True, "source_year": 2026,
                       "pre_medicare_monthly_per_person": 1850,
                       "medicare_monthly_per_person": 700, "inflation": .06},
        "capital_expenses": {"home_reserve_pct": .01,
                             "home_reserve_inflation": .035,
                             "schedules": [{"id": "vehicle-replacement",
                                 "name": "Vehicle replacement", "amount_today": 50000,
                                 "first_date": "2040-06", "interval_years": 10,
                                 "inflation": None, "enabled": True}]},
        "assumptions": {"risk": "moderate", "horizon_age": 95},
    }
    p = prepare(prof)
    # 1978 birth + horizon 95 -> 2073; the 1979 spouse pushes the end out a year.
    assert p["retirement_year"] == 2040 and p["end_year"] == 2074
    res = project(p, p["mean_return"])
    assert len(res["rows"]) == p["end_year"] - p["start_year"] + 1
    rows = {r["year"]: r for r in res["rows"]}
    ret = p["retirement_year"]
    # Retiring at 62 means both are pre-Medicare and buying their own cover for
    # three years -- the bridge this model exists to price.
    assert rows[ret]["healthcare_detail"]["pre_medicare"] > 0
    assert rows[ret]["healthcare_detail"]["medicare"] == 0
    # Self reaches 65 in April 2043 and the spouse in September 2044, so those
    # two years are mixed and 2045 is the first with nobody pre-Medicare.
    assert rows[2043]["healthcare_detail"]["medicare"] > 0
    assert rows[2043]["healthcare_detail"]["pre_medicare"] > 0
    assert rows[2044]["healthcare_detail"]["pre_medicare"] > 0
    assert rows[2045]["healthcare_detail"]["pre_medicare"] == 0
    # Dated vehicle events occur exactly every ten years, not as smoothed spending.
    vehicle_years = [r["year"] for r in res["rows"]
                     if any(e["name"] == "Vehicle replacement" for e in r["events"])]
    assert vehicle_years == [ret, ret + 10, ret + 20, ret + 30], vehicle_years
    expected_reserve = 875000 * .01 * 1.035 ** (ret + 1 - p["start_year"])
    assert abs(rows[ret + 1]["spending_components"]["home_reserve"] - expected_reserve) < 2
    # Observed medical is removed from adjustable lifestyle before explicit health.
    assert p["lifestyle_monthly_today"] == 7000 - 452
    # tiny spending -> never depletes; huge spending -> depletes
    lean = dict(p, lifestyle_monthly_today=1000, health_enabled=False,
                home_reserve_pct=0, scheduled_expenses=[])
    assert project(lean, 0.05)["depleted_at"] is None
    assert project(dict(lean, lifestyle_monthly_today=40000), 0.05)["depleted_at"] is not None
    s = sustainable_spending(p)
    assert 1000 < s < 60000, s
    # super catch-up window: limit at 61 exceeds limit at 59
    assert _k401_limit(61) > _k401_limit(59) > _k401_limit(49)
    assert _k401_limit(59) == td.K401_LIMIT + td.CATCHUP_50
    assert _k401_limit(61, 2026) == td.K401_LIMIT + 11250

    # the employer match is on top of the elective limit, never inside it
    maxed = dict(prof, income={"salary_annual": 300000, "k401_pct": 20,
                               "employer_match_pct": 10})
    pm = prepare(maxed)
    r0 = project(pm, 0.0)["rows"][0]
    lim = _k401_limit(r0["age"])
    assert pm["k401_elected"] > lim                       # election exceeds the cap
    expected = lim + pm["match"] + sum(pm["extra_contrib"].values())
    assert r0["contributions"] == round(expected), (r0["contributions"], expected)

    # ---- unvested equity feeding the plan -----------------------------------
    vprof = dict(prof, equity_comp={
        "symbol": "ACME", "price": 100.0, "withholding": 0.30,
        "vests": [{"date": "2027-06-30", "shares": 500, "award_type": "SA",
                   "strike": None, "is_option": False, "condition": None},
                  {"date": "2028-06-30", "shares": 300, "award_type": "NQ",
                   "strike": 60.0, "is_option": True,
                   "condition": "Retirement Provision"}]})
    pv = prepare(vprof)
    assert pv["vesting"]["enabled"] is True and pv["vesting"]["symbol"] == "ACME"
    # 500 shares at 100 = 50,000 gross; an option is only its 40/share spread
    assert pv["vesting"]["by_year"][2027]["gross"] == 50_000
    assert pv["vesting"]["by_year"][2028]["gross"] == 300 * 40
    # withholding comes off before anything is invested
    assert pv["vesting"]["by_year"][2027]["net"] == 35_000

    rows = {r["year"]: r for r in project(pv, 0.0)["rows"]}
    assert rows[2027]["vest_gross"] == 50_000 and rows[2027]["vest_net"] == 35_000
    assert rows[2026]["vest_gross"] == 0        # nothing scheduled that year
    # the net lands in taxable and carries its own basis, so it is not taxed
    # a second time on the way out
    base_rows = {r["year"]: r for r in project(prepare(prof), 0.0)["rows"]}
    assert (rows[2027]["taxable"] - base_rows[2027]["taxable"]) == 35_000
    assert (rows[2027]["taxable_basis"] - base_rows[2027]["taxable_basis"]) == 35_000
    # the gross is ordinary income in the vest year, so it shows up in MAGI
    assert rows[2027]["magi"] - base_rows[2027]["magi"] == 50_000
    # ...and it is reported as an event rather than appearing from nowhere
    assert any("Equity vest" in e["name"] for e in rows[2027]["events"])
    # more money in the plan cannot make the plan worse. This fixture already
    # runs out, so the gain shows as the money lasting longer rather than as a
    # bigger end balance — which is the effect that matters to the owner.
    with_v, without_v = project(pv, 0.05), project(prepare(prof), 0.05)
    assert with_v["end_balance"] >= without_v["end_balance"]
    assert with_v["depleted_at"] > without_v["depleted_at"], (
        with_v["depleted_at"], without_v["depleted_at"])

    # conditional vests can be switched off, and then contribute nothing
    off = dict(vprof, equity_comp=dict(vprof["equity_comp"],
                                       include_conditional=False))
    assert prepare(off)["vesting"]["by_year"].get(2028) is None
    assert prepare(off)["vesting"]["by_year"][2027]["gross"] == 50_000
    # no price -> the schedule is known but contributes no dollars, rather
    # than quietly valuing the shares at zero
    nop = dict(vprof, equity_comp=dict(vprof["equity_comp"], price=None))
    assert prepare(nop)["vesting"]["enabled"] is False
    assert {r["vest_gross"] for r in project(prepare(nop), 0.0)["rows"]} == {0}
    # switched off entirely, or absent, changes nothing at all
    dis = dict(vprof, equity_comp=dict(vprof["equity_comp"], enabled=False))
    assert project(prepare(dis), 0.05)["end_balance"] == project(prepare(prof), 0.05)["end_balance"]
    assert prepare(prof)["vesting"]["by_year"] == {}

    # a contribution typed onto the employer plan is the same money as the
    # salary-driven elective and must not be counted twice
    dup = dict(prof, assets=prof["assets"] + [
        {"name": "Employer 401(k)", "type": "401k", "balance": 0,
         "annual_contribution": 39382}])
    pd = prepare(dup)
    assert pd["extra_contrib"]["deferred"] == 0.0, pd["extra_contrib"]
    assert pd["ignored_contributions"][0]["amount"] == 39382
    # HSA is a distinct healthcare bucket: qualified medical draws are tax-free
    # and it is never included in the RMD calculation.
    hsa_prof = {
        "household": {"self_birthdate": "1950-01-01", "state": "CA",
                      "retirement_date": "2026-01"},
        "assets": [{"type": "hsa", "balance": 100000}],
        "spending": {"retirement_monthly_today": 0},
        "healthcare": {"enabled": False},
        "capital_expenses": {"home_reserve_pct": 0, "schedules": []},
        "social_security": {"self": {"67": 0}},
        "assumptions": {"horizon_age": 76}}
    hsa_row = project(prepare(hsa_prof), 0)["rows"][0]
    assert hsa_row["hsa"] == 100000
    assert hsa_row["withdrawal_sources"]["forced_rmd"] == 0
    print("projection self-test OK  (sustainable check passed)")
