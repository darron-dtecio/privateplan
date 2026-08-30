"""Federal + state tax estimates for working years and retirement years.

Estimates, not tax prep.  Retirement-account withdrawals are treated as
ordinary income (conservative for taxable-account dollars, right for
401k/IRA); Social Security is taxed federally under the provisional-income
rules and by the state only where that state actually taxes it.

Filing status and state both come from profile["household"], and every rate
table behind them lives in taxtables/ -- see taxdata.py.  Two state-level
behaviours are honoured here: whether the state taxes Social Security, and
whether it exempts qualified retirement income entirely (IL, PA, MS).

Not modeled, and material if they apply to you: partial or age-based
retirement-income exclusions (CO, GA, MI, NY, UT and others), local income
taxes (NYC, Philadelphia, Ohio municipalities), part-year and multi-state
residency, and itemized deductions.  See the Limitations section of README.md.
"""

from __future__ import annotations

import taxdata as td


def index_factor(year: int, inflation: float = td.INFLATION_DEFAULT) -> float:
    return (1 + inflation) ** max(year - td.TAX_YEAR, 0)


def _indexed(brackets, year: int, inflation: float):
    f = index_factor(year, inflation)
    return [((upper * f) if upper != float("inf") else upper, rate)
            for upper, rate in brackets]


def federal_tax(taxable: float, year: int = td.TAX_YEAR,
                inflation: float = td.INFLATION_DEFAULT,
                filing_status: str = "mfj") -> float:
    brackets = td.federal(filing_status)["brackets"]
    return td.bracket_tax(max(taxable, 0.0), _indexed(brackets, year, inflation))


def state_tax(taxable: float, state: str, year: int = td.TAX_YEAR,
              inflation: float = td.INFLATION_DEFAULT,
              filing_status: str = "mfj") -> float:
    return td.load_state(state, filing_status).tax(taxable, year, inflation)


def ss_taxable_amount(ss_annual: float, other_income: float,
                      filing_status: str = "mfj") -> float:
    """Federally taxable portion of SS benefits, per provisional-income rules."""
    if ss_annual <= 0:
        return 0.0
    f = td.federal(filing_status)
    base, upper = f["ss_tax_base"], f["ss_tax_upper"]
    provisional = other_income + 0.5 * ss_annual
    # Married filing separately (having lived with a spouse) has zero bases, so
    # benefits are taxable from the first dollar up to the 85% cap.
    if base <= 0 and upper <= 0:
        return 0.85 * ss_annual
    if provisional <= base:
        return 0.0
    if provisional <= upper:
        return min(0.5 * (provisional - base), 0.5 * ss_annual)
    return min(0.85 * ss_annual,
               0.85 * (provisional - upper)
               + min(0.5 * ss_annual, 0.5 * (upper - base)))


def fica(wages: float, filing_status: str = "mfj") -> float:
    threshold = td.federal(filing_status)["addl_medicare_threshold"]
    ss = min(wages, td.SS_WAGE_BASE) * td.SS_RATE
    medicare = wages * td.MEDICARE_RATE
    if wages > threshold:
        medicare += (wages - threshold) * td.ADDL_MEDICARE_RATE
    return ss + medicare


def _std_deduction(n_65_plus: int, magi: float, year: int,
                   inflation: float = td.INFLATION_DEFAULT,
                   filing_status: str = "mfj") -> float:
    f = td.federal(filing_status)
    factor = index_factor(year, inflation)
    ded = (f["standard_deduction"] + n_65_plus * f["std_deduction_65_extra"]) * factor
    if n_65_plus and year <= td.SENIOR_DEDUCTION_LAST_YEAR:
        senior = n_65_plus * td.SENIOR_DEDUCTION
        over = max(magi - f["senior_deduction_phaseout_start"], 0)
        ded += max(senior - over * td.SENIOR_DEDUCTION_PHASEOUT_RATE, 0)
    return ded


def current_year(gross_wages: float, pretax_deductions: float,
                 spouse_wages: float = 0.0, state: str = "CA",
                 filing_status: str = "mfj") -> dict:
    """Estimated household taxes while working."""
    f = td.federal(filing_status)
    policy = td.load_state(state, filing_status)
    wages = gross_wages + spouse_wages
    agi = wages - pretax_deductions
    fed_taxable = max(agi - f["standard_deduction"], 0)
    state_taxable = max(agi - policy.standard_deduction, 0)
    fed = federal_tax(fed_taxable, filing_status=filing_status)
    st = policy.tax(state_taxable)
    fi = (fica(gross_wages, filing_status)
          + (fica(spouse_wages, filing_status) if spouse_wages else 0))
    total = fed + st + fi
    return {"federal": round(fed), "state": round(st), "fica": round(fi),
            "total": round(total),
            "effective_rate": round(total / wages, 4) if wages else 0,
            "marginal_federal": td.marginal_rate(fed_taxable, f["brackets"]),
            "marginal_state": policy.marginal_rate(state_taxable)}


def retirement_sources(ordinary_withdrawal: float, taxable_gain: float,
                       ss_annual: float, n_65_plus: int, year: int,
                       state: str = "CA",
                       inflation: float = td.INFLATION_DEFAULT,
                       filing_status: str = "mfj") -> dict:
    """Tax a retirement year by source instead of taxing returned basis/Roth."""
    f = td.federal(filing_status)
    policy = td.load_state(state, filing_status)
    other = ordinary_withdrawal + taxable_gain
    taxable_ss = ss_taxable_amount(ss_annual, other, filing_status)
    magi = other + taxable_ss
    deduction = _std_deduction(n_65_plus, magi, year, inflation, filing_status)
    ordinary_taxable = max(ordinary_withdrawal + taxable_ss - deduction, 0)
    gain_taxable = max(taxable_gain - max(deduction - ordinary_withdrawal - taxable_ss, 0), 0)
    fed = federal_tax(ordinary_taxable, year, inflation, filing_status)
    ltcg = 0.0
    brackets = _indexed(f["ltcg"], year, inflation)
    used = ordinary_taxable
    remaining = gain_taxable
    lower = 0.0
    for upper, rate in brackets:
        room = max(upper - max(used, lower), 0)
        take = min(remaining, room)
        ltcg += take * rate
        remaining -= take
        lower = upper
        if remaining <= 0:
            break
    niit_threshold = f["niit_threshold"] * index_factor(year, inflation)
    niit = min(taxable_gain, max(magi - niit_threshold, 0)) * td.NIIT_RATE
    fed += ltcg + niit

    # State side.  Qualified retirement income is exempt outright in a few
    # states; elsewhere it is ordinary income.  Social Security enters the state
    # base only where the state actually taxes benefits.
    state_income = 0.0 if policy.exempts_retirement_income else other
    if policy.taxes_social_security:
        state_income += taxable_ss
    state_ded = policy.standard_deduction * index_factor(year, inflation)
    st = policy.tax(max(state_income - state_ded, 0), year, inflation)

    total = fed + st
    gross_income = ordinary_withdrawal + taxable_gain + ss_annual
    return {"federal": round(fed), "state": round(st), "total": round(total),
            "taxable_ss": round(taxable_ss), "capital_gains_tax": round(ltcg + niit),
            "ss_taxable_pct": round(taxable_ss / ss_annual, 3) if ss_annual else 0,
            "effective_rate": round(total / gross_income, 4) if gross_income else 0,
            "magi": round(magi)}


def retirement_year(gross_withdrawal: float, ss_annual: float,
                    n_65_plus: int, year: int, state: str = "CA",
                    inflation: float = td.INFLATION_DEFAULT,
                    filing_status: str = "mfj") -> dict:
    """Taxes on a retirement year: tax-deferred withdrawals + SS benefits."""
    return retirement_sources(gross_withdrawal, 0, ss_annual, n_65_plus,
                              year, state, inflation, filing_status)


def net_withdrawal(gross_withdrawal: float, ss_annual: float, n_65_plus: int,
                   year: int, state: str = "CA",
                   filing_status: str = "mfj") -> float:
    """After-tax cash available from a gross withdrawal + SS."""
    t = retirement_year(gross_withdrawal, ss_annual, n_65_plus, year, state,
                        filing_status=filing_status)
    return gross_withdrawal + ss_annual - t["total"]


def solve_gross_withdrawal(net_needed: float, ss_annual: float, n_65_plus: int,
                           year: int, state: str = "CA",
                           filing_status: str = "mfj") -> float:
    """Smallest gross withdrawal whose after-tax value (with SS) covers net_needed."""
    if net_withdrawal(0, ss_annual, n_65_plus, year, state, filing_status) >= net_needed:
        return 0.0
    lo, hi = 0.0, max(net_needed * 2, 50_000.0)
    while net_withdrawal(hi, ss_annual, n_65_plus, year, state, filing_status) < net_needed:
        hi *= 2
        if hi > 1e9:
            break
    for _ in range(60):
        mid = (lo + hi) / 2
        if net_withdrawal(mid, ss_annual, n_65_plus, year, state, filing_status) >= net_needed:
            hi = mid
        else:
            lo = mid
    return hi


def irmaa_tier(magi: float, year: int = td.TAX_YEAR,
               inflation: float = td.INFLATION_DEFAULT,
               filing_status: str = "mfj") -> str:
    tiers = td.federal(filing_status)["irmaa_tiers"]
    for upper, label in _indexed(tiers, year, inflation):
        if magi <= upper:
            return label
    return tiers[-1][1]


def irmaa_surcharge(magi_two_years_prior: float, premium_year: int,
                    enrollees: int, inflation: float = td.INFLATION_DEFAULT,
                    filing_status: str = "mfj") -> float:
    if not enrollees:
        return 0.0
    schedule = td.federal(filing_status)["irmaa_surcharge_annual"]
    for upper, annual in _indexed(schedule, premium_year, inflation):
        if magi_two_years_prior <= upper:
            return annual * enrollees
    return 0.0


def rmd(balance: float, age: int) -> float:
    if age < td.RMD_START_AGE:
        return 0.0
    divisor = td.UNIFORM_LIFETIME.get(age, td.UNIFORM_LIFETIME[100])
    return balance / divisor


if __name__ == "__main__":
    # federal MFJ on $200k taxable
    expected = 24_800 * .10 + (100_800 - 24_800) * .12 + (200_000 - 100_800) * .22
    assert abs(federal_tax(200_000) - expected) < 1
    # SS taxation edges
    assert ss_taxable_amount(30_000, 10_000) == 0.0            # provisional 25k < 32k
    assert 0 < ss_taxable_amount(30_000, 25_000) <= 15_000     # middle band
    assert ss_taxable_amount(40_000, 200_000) == 0.85 * 40_000  # capped at 85%
    # solver round-trips
    g = solve_gross_withdrawal(80_000, 45_000, 2, 2033)
    assert abs(net_withdrawal(g, 45_000, 2, 2033) - 80_000) < 5, g
    assert rmd(1_000_000, 74) == 0
    assert abs(rmd(1_000_000, 75) - 1_000_000 / 24.6) < 1
    assert irmaa_tier(100_000) == "standard"
    assert irmaa_surcharge(500_000, 2028, 2) > irmaa_surcharge(100_000, 2028, 2)
    # Only the gain portion of a taxable withdrawal is taxed; unknown basis may
    # conservatively be represented as a 100% gain.
    basis_known = retirement_sources(0, 20_000, 0, 0, 2026)
    basis_unknown = retirement_sources(100_000, 0, 0, 0, 2026)
    assert basis_known["total"] < basis_unknown["total"]
    cy = current_year(150_000, 18_000)
    assert 0.15 < cy["effective_rate"] < 0.35, cy

    # ---- filing status ------------------------------------------------------
    # A single filer on the same income pays more than a joint one, everywhere.
    for st_code in ("CA", "TX", "IL"):
        joint = current_year(150_000, 18_000, state=st_code, filing_status="mfj")
        single = current_year(150_000, 18_000, state=st_code, filing_status="single")
        assert single["total"] > joint["total"], (st_code, single, joint)
    # Head of household sits between joint and single.
    hoh = current_year(150_000, 18_000, filing_status="hoh")["total"]
    assert (current_year(150_000, 18_000)["total"] < hoh
            < current_year(150_000, 18_000, filing_status="single")["total"])
    # Every shipped status resolves and produces a number.
    for status in td.FILING_STATUSES:
        assert current_year(120_000, 10_000, filing_status=status)["total"] > 0
    try:
        current_year(120_000, 0, filing_status="nonsense")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown filing status should raise")

    # ---- state behaviour ----------------------------------------------------
    # A no-income-tax state owes no state tax; California owes some.
    assert current_year(150_000, 18_000, state="TX")["state"] == 0
    assert current_year(150_000, 18_000, state="CA")["state"] > 0
    # An unknown code falls back to the documented flat estimate, not a crash.
    assert current_year(150_000, 18_000, state="ZZ")["state"] > 0
    # Illinois exempts qualified retirement income: same withdrawal, no state
    # tax, while California taxes it.  Federal is identical either way.
    il = retirement_sources(90_000, 0, 40_000, 2, 2033, "IL")
    ca = retirement_sources(90_000, 0, 40_000, 2, 2033, "CA")
    assert il["state"] == 0 and ca["state"] > 0, (il, ca)
    assert il["federal"] == ca["federal"]
    # The Social Security flag is honoured per state.
    assert td.load_state("CO").taxes_social_security
    assert not td.load_state("AZ").taxes_social_security
    print("tax self-test OK")
