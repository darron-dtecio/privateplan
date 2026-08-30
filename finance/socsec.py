"""Social Security claiming analysis.

All comparisons are in today's dollars (no COLA), the standard way to look
at claiming breakevens; the projection model applies COLA separately.
"""

from __future__ import annotations


def fra(birth_year: int) -> float:
    """Full retirement age in years. Born 1960+ -> exactly 67."""
    if birth_year >= 1960:
        return 67.0
    if birth_year >= 1955:
        return 66 + (birth_year - 1954) * 2 / 12
    return 66.0


def adjust(pia_monthly: float, claim_age: float, fra_age: float = 67.0) -> float:
    """Monthly benefit at claim_age given the PIA (benefit at FRA).

    Early: 5/9% per month for the first 36 months, 5/12% beyond.
    Delayed: 2/3% per month (8%/yr) up to age 70.
    """
    months = round((claim_age - fra_age) * 12)
    if months == 0:
        return pia_monthly
    if months < 0:
        m = -months
        reduction = min(m, 36) * (5 / 9 / 100) + max(m - 36, 0) * (5 / 12 / 100)
        return pia_monthly * (1 - reduction)
    return pia_monthly * (1 + min(months, 36) * (2 / 3 / 100))


def benefit_at(claim_age: int, statement: dict, fra_age: float = 67.0) -> float:
    """Prefer the SSA statement's own estimate for that age; else derive from FRA."""
    key = str(claim_age)
    if statement.get(key):
        return float(statement[key])
    pia = float(statement.get(str(int(fra_age)), 0) or 0)
    return adjust(pia, claim_age, fra_age)


def cumulative_by_age(claim_age: int, monthly: float, to_age: int = 95) -> list[float]:
    """Cumulative benefits received by each age from 62..to_age (today's $)."""
    out, total = [], 0.0
    for age in range(62, to_age + 1):
        if age >= claim_age:
            total += monthly * 12
        out.append(round(total, 0))
    return out


def breakeven(claim_a: int, claim_b: int, statement: dict, to_age: int = 100) -> int | None:
    """Age at which cumulative benefits from claiming later overtake claiming earlier."""
    early, late = sorted((claim_a, claim_b))
    m_early = benefit_at(early, statement)
    m_late = benefit_at(late, statement)
    cum_e = cum_l = 0.0
    for age in range(early, to_age + 1):
        cum_e += m_early * 12
        if age >= late:
            cum_l += m_late * 12
        if cum_l > cum_e:
            return age
    return None


def spousal_topup(self_pia: float, spouse_own_fra: float) -> float:
    """Extra the spouse gets on top of their own benefit, claimed at spouse FRA."""
    return max(0.0, self_pia / 2 - spouse_own_fra)


def spouse_components(self_pia: float, spouse_own_pia: float,
                      spouse_claim_age: float, worker_claim_age: float,
                      spouse_age_when_worker_files: float,
                      spouse_statement: dict | None = None) -> dict:
    """Own benefit plus the separately reduced excess spousal benefit.

    Deemed filing means the spouse cannot choose to leave one component
    unclaimed once both are available.  A spouse who claims their own record
    first receives the excess only when the worker later files.
    """
    own = (benefit_at(int(spouse_claim_age), spouse_statement)
           if spouse_statement else adjust(spouse_own_pia, spouse_claim_age, 67.0))
    excess_fra = spousal_topup(self_pia, spouse_own_pia)
    excess_age = max(spouse_claim_age, spouse_age_when_worker_files)
    # Spousal benefits do not earn delayed credits; at/after FRA the full
    # excess is paid.  Before FRA, use the statutory spouse reduction.
    months_early = max(round((67.0 - excess_age) * 12), 0)
    reduction = (min(months_early, 36) * (25 / 36 / 100)
                 + max(months_early - 36, 0) * (5 / 12 / 100))
    excess = excess_fra * max(1 - reduction, 0)
    return {"own": own, "spousal_topup": excess,
            "total_after_worker_files": own + excess,
            "spousal_topup_fra": excess_fra}


def survivor_monthly(worker_actual: float, spouse_actual: float) -> float:
    """Household benefit after one death: the larger actual benefit remains."""
    return max(worker_actual, spouse_actual)


def run(ss: dict, horizon_age: int = 95, self_birth_year: int | None = None,
        spouse_birth_year: int | None = None) -> dict:
    """ss = profile['social_security']. Returns dashboard-ready dict."""
    stmt = ss["self"]
    fra_age = 67.0
    compare = ss.get("compare_claim_ages") or [62, 67, 70]
    monthly = {str(a): round(benefit_at(a, stmt, fra_age), 0) for a in range(62, 71)}
    cumulative = {str(a): cumulative_by_age(a, benefit_at(a, stmt, fra_age), horizon_age)
                  for a in compare}
    breakevens = []
    for a, b in [(62, 67), (67, 70), (62, 70)]:
        be = breakeven(a, b, stmt)
        breakevens.append({"early": a, "late": b, "breakeven_age": be})

    self_pia = float(stmt.get("67") or 0)
    spouse_stmt = ss.get("spouse") or {}
    spouse_own = float(spouse_stmt.get("67")
                       or ss.get("spouse_own_monthly_fra") or 0)
    age_gap = ((spouse_birth_year or self_birth_year or 0)
               - (self_birth_year or spouse_birth_year or 0))
    spouse_claim = float(ss.get("claim_age_spouse") or 67)
    comps = spouse_components(self_pia, spouse_own, spouse_claim,
                              float(ss.get("claim_age_self") or 67),
                              float(ss.get("claim_age_self") or 67) - age_gap,
                              spouse_stmt)
    spouse_total = comps["total_after_worker_files"]
    household = {}
    for a in compare:
        c = spouse_components(self_pia, spouse_own, spouse_claim, a, a - age_gap,
                              spouse_stmt)
        household[str(a)] = round(monthly[str(a)] + c["total_after_worker_files"], 0)

    return {
        "fra": fra_age,
        "monthly_by_claim_age": monthly,
        "cumulative_by_age": cumulative,
        "cumulative_ages": list(range(62, horizon_age + 1)),
        "breakevens": breakevens,
        "spouse": {"own_fra": spouse_own,
                   "own_at_claim": round(comps["own"], 0),
                   "spousal_topup": round(comps["spousal_topup"], 0),
                   "spousal_topup_fra": round(comps["spousal_topup_fra"], 0),
                   "total": round(spouse_total, 0),
                   "claim_age": spouse_claim},
        "survivor_monthly": round(survivor_monthly(
            benefit_at(float(ss.get("claim_age_self") or 67), stmt, fra_age),
            spouse_total), 0),
        "household_monthly_by_claim_age": household,
        "notes": [
            "Spousal top-up brings the spouse to 50% of your FRA benefit; it cannot "
            "start before you file, and is reduced if the spouse claims before their own FRA.",
            "Survivor benefit: the surviving spouse steps up to the deceased's actual "
            "benefit — delaying your claim raises the survivor's lifetime floor.",
            "Amounts in today's dollars; SSA estimates assume you keep working at the "
            "current earnings level until the claim age shown.",
        ],
    }


if __name__ == "__main__":
    assert fra(1967) == 67.0
    # claiming at 62 with FRA 67 -> 70% of PIA; at 70 -> 124%
    assert abs(adjust(1000, 62, 67) - 700) < 0.5, adjust(1000, 62, 67)
    assert abs(adjust(1000, 70, 67) - 1240) < 0.5
    stmt = {"62": 2100, "67": 3000, "70": 3720}
    assert benefit_at(62, stmt) == 2100          # statement value wins
    assert abs(benefit_at(65, stmt) - 3000 * (1 - 24 * 5 / 9 / 100)) < 1
    be = breakeven(62, 67, stmt)
    assert be is not None and 76 <= be <= 82, be   # classic ~78-80 crossover
    assert spousal_topup(3000, 300) == 1200
    r = run({"self": stmt, "spouse_own_monthly_fra": 300})
    assert r["household_monthly_by_claim_age"]["67"] == 3000 + 1500
    early = spouse_components(3000, 300, 62, 67, 63)
    assert early["total_after_worker_files"] < 1500
    exact = spouse_components(3000, 1246, 62, 67, 63,
                              {"62": 877, "67": 1246, "70": 1545})
    assert exact["own"] == 877
    assert survivor_monthly(3000, 1200) == 3000
    print("socsec self-test OK")
