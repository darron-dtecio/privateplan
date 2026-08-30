"""Tax-year constants and the federal/state table registry.

The rates themselves live in taxtables/: federal.json keyed by filing
status, and states/<xx>.json one file per state.  This module holds only
the figures that vary by neither -- FICA, contribution limits, RMD
divisors, risk presets -- plus the loaders federal(), load_state() and
available_states().

Every figure is an ESTIMATE for the stated tax year.  Confirm against
current IRS, state revenue and SSA publications before relying on exact
dollar outcomes.  See docs/ADDING_A_STATE.md to add or correct a state.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

TAX_YEAR = 2026

HERE = Path(__file__).resolve().parent
TABLES = HERE / "taxtables"
STATES_DIR = TABLES / "states"

FILING_STATUSES = ("mfj", "single", "hoh", "mfs")
INF = float("inf")


def _pairs(raw) -> list[tuple[float, object]]:
    """JSON [upper, value] pairs -> tuples, with null upper meaning unbounded."""
    return [((INF if upper is None else float(upper)), value)
            for upper, value in raw]


@lru_cache(maxsize=None)
def _federal_tables() -> dict:
    return json.loads((TABLES / "federal.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def federal(filing_status: str = "mfj") -> dict:
    """Federal figures for one filing status, brackets already de-JSON-ified."""
    status = (filing_status or "mfj").lower()
    table = _federal_tables()["filing_status"]
    if status not in table:
        raise ValueError(f"unknown filing status {filing_status!r}; "
                         f"expected one of {', '.join(FILING_STATUSES)}")
    f = dict(table[status])
    f["brackets"] = _pairs(f["brackets"])
    f["ltcg"] = _pairs(f["ltcg"])
    f["irmaa_tiers"] = _pairs(f["irmaa_tiers"])
    f["irmaa_surcharge_annual"] = _pairs(f["irmaa_surcharge_annual"])
    return f


class StatePolicy:
    """One state income-tax treatment, from taxtables/states/<xx>.json.

    Three kinds: "none" (no income tax), "flat" (single rate), and "brackets"
    (a progressive schedule per filing status).  A state whose file omits the
    requested filing status falls back to "single", the conventional
    approximation, which is better than silently using the joint schedule.
    """

    def __init__(self, raw: dict, filing_status: str = "mfj"):
        self.raw = raw
        self.code = raw.get("code", "OTHER")
        self.name = raw.get("name", self.code)
        self.kind = raw.get("kind", "flat")
        self.estimate = bool(raw.get("estimate", True))
        self.source = raw.get("source", "")
        self.warning = raw.get("_warning", "")
        self.local_tax_note = raw.get("local_tax_note", "")
        self.taxes_social_security = bool(raw.get("taxes_social_security", False))
        self.taxes_retirement_income = raw.get("taxes_retirement_income", "full")
        self.index_brackets = bool(raw.get("index_brackets", False))
        self.surtaxes = raw.get("surtaxes") or []
        self.property_tax_growth = raw.get("property_tax_growth")
        self.insurance_inflation = raw.get("insurance_inflation")

        status = (filing_status or "mfj").lower()
        by_status = raw.get("filing_status") or {}
        block = by_status.get(status) or by_status.get("single") or {}
        self.filing_status = status
        self.brackets = _pairs(block["brackets"]) if block.get("brackets") else []
        self.standard_deduction = float(block.get("standard_deduction", 0.0))
        self.rate = float(raw.get("rate", 0.0))

    @property
    def exempts_retirement_income(self) -> bool:
        return str(self.taxes_retirement_income).lower() == "exempt"

    def tax(self, taxable: float, year: int = TAX_YEAR,
            inflation: float | None = None) -> float:
        """State income tax on taxable, indexed forward where the state indexes."""
        taxable = max(taxable, 0.0)
        if self.kind == "none" or not taxable:
            return 0.0
        inflation = INFLATION_DEFAULT if inflation is None else inflation
        factor = ((1 + inflation) ** max(year - TAX_YEAR, 0)
                  if self.index_brackets else 1.0)
        if self.kind == "brackets" and self.brackets:
            scaled = [((u * factor) if u != INF else u, r) for u, r in self.brackets]
            t = bracket_tax(taxable, scaled)
        else:
            t = taxable * self.rate
        for sur in self.surtaxes:
            threshold = float(sur.get("threshold", 0)) * factor
            if taxable > threshold:
                t += (taxable - threshold) * float(sur.get("rate", 0.0))
        return t

    def marginal_rate(self, taxable: float) -> float:
        if self.kind == "none":
            return 0.0
        if self.kind == "brackets" and self.brackets:
            return marginal_rate(max(taxable, 0.0), self.brackets)
        return self.rate

    def __repr__(self) -> str:
        return f"<StatePolicy {self.code} {self.kind} {self.filing_status}>"


@lru_cache(maxsize=None)
def _state_raw(code: str) -> dict:
    path = STATES_DIR / f"{code.lower()}.json"
    if not path.is_file():
        path = STATES_DIR / "_default.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_state(code: str | None, filing_status: str = "mfj") -> StatePolicy:
    """State policy for a USPS code.  Unknown codes get the documented fallback."""
    return StatePolicy(_state_raw((code or "OTHER").strip() or "OTHER"),
                       filing_status)


@lru_cache(maxsize=None)
def available_states() -> list[tuple[str, str]]:
    """(code, name) for every shipped state table, alphabetical, OTHER last."""
    out = []
    for path in sorted(STATES_DIR.glob("*.json")):
        if path.stem.startswith("_"):
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        out.append((raw["code"], raw["name"]))
    out.sort(key=lambda x: x[1])
    out.append(("OTHER", "Other / not yet modeled"))
    return out


# ---- Federal, married filing jointly ----------------------------------------
# Module-level aliases kept for the many callers that predate filing-status
# support; each is just the MFJ entry of the table above.
_MFJ = federal("mfj")
FED_MFJ = _MFJ["brackets"]
STD_DEDUCTION_MFJ = _MFJ["standard_deduction"]
STD_DEDUCTION_65_EXTRA = _MFJ["std_deduction_65_extra"]
SENIOR_DEDUCTION_PHASEOUT_START_MFJ = _MFJ["senior_deduction_phaseout_start"]
ADDL_MEDICARE_THRESHOLD_MFJ = _MFJ["addl_medicare_threshold"]
SS_TAX_BASE_MFJ = _MFJ["ss_tax_base"]
SS_TAX_UPPER_MFJ = _MFJ["ss_tax_upper"]
LTCG_MFJ = _MFJ["ltcg"]
NIIT_THRESHOLD_MFJ = _MFJ["niit_threshold"]
IRMAA_TIERS_MFJ = _MFJ["irmaa_tiers"]
IRMAA_SURCHARGE_ANNUAL_MFJ = _MFJ["irmaa_surcharge_annual"]

# OBBBA senior deduction: $6,000 per person 65+, tax years 2025-2028,
# phasing out above the per-status threshold in federal.json.
SENIOR_DEDUCTION = 6_000
SENIOR_DEDUCTION_LAST_YEAR = 2028
SENIOR_DEDUCTION_PHASEOUT_RATE = 0.06
NIIT_RATE = 0.038

# ---- FICA (2026, est.) -------------------------------------------------------
SS_WAGE_BASE = 184_500
SS_RATE = 0.062
MEDICARE_RATE = 0.0145
ADDL_MEDICARE_RATE = 0.009
ADDL_MEDICARE_THRESHOLD_MFJ = 250_000

# ---- Retirement plan limits (2026, est.) --------------------------------------
K401_LIMIT = 24_500
CATCHUP_50 = 8_000
# SECURE 2.0 §109 "super catch-up", ages 60-63: greater of $10k or 150% of
# the regular catch-up. Born 1967 -> applies tax years 2027-2030.
SUPER_CATCHUP_60_63 = 11_250
IRA_LIMIT = 7_500
IRA_CATCHUP_50 = 1_100
# §415(c) total additions: employee + employer + forfeitures, per employer.
# The match counts here, NOT against the elective deferral limit above.
TOTAL_ADDITIONS_415C = 72_000
# SECURE 2.0 §603: if prior-year FICA wages from this employer exceed the
# threshold, catch-up contributions must be designated Roth (from 2026).
ROTH_CATCHUP_WAGE_THRESHOLD = 150_000

# ---- RMDs (SECURE 2.0: start age 75 for those born 1960+) ---------------------
RMD_START_AGE = 75
UNIFORM_LIFETIME = {
    75: 24.6, 76: 23.7, 77: 22.9, 78: 22.0, 79: 21.1,
    80: 20.2, 81: 19.4, 82: 18.5, 83: 17.7, 84: 16.8,
    85: 16.0, 86: 15.2, 87: 14.4, 88: 13.7, 89: 12.9,
    90: 12.2, 91: 11.5, 92: 10.8, 93: 10.1, 94: 9.5,
    95: 8.9, 96: 8.4, 97: 7.8, 98: 7.3, 99: 6.8, 100: 6.4,
}

# ---- Social Security taxation (federal; thresholds not indexed) ---------------
SS_TAX_BASE_MFJ = 32_000     # provisional income below this: 0% taxable
SS_TAX_UPPER_MFJ = 44_000    # above this: up to 85% taxable

# ---- Medicare IRMAA, MFJ MAGI tiers (2026, est.; 2-year lookback applies) -----
IRMAA_TIERS_MFJ = [
    (218_000, "standard"),
    (272_000, "tier 1 (+~$888/yr each)"),
    (340_000, "tier 2 (+~$2,220/yr each)"),
    (408_000, "tier 3 (+~$3,552/yr each)"),
    (750_000, "tier 4 (+~$4,884/yr each)"),
    (float("inf"), "tier 5 (+~$5,328/yr each)"),
]
# Annual Part B + Part D income surcharges per Medicare enrollee.  The first
# tuple is the standard tier and therefore adds nothing to healthcare spending.
IRMAA_SURCHARGE_ANNUAL_MFJ = [
    (218_000, 0), (274_000, (81.20 + 14.50) * 12),
    (342_000, (202.90 + 37.50) * 12), (410_000, (324.60 + 60.40) * 12),
    (750_000, (446.30 + 83.30) * 12),
    (float("inf"), (487.00 + 91.00) * 12),
]

# 2026 long-term capital-gain thresholds, MFJ.  The taxable-account model
# stacks gains on top of ordinary taxable income.
LTCG_MFJ = [(98_900, 0.0), (613_700, 0.15), (float("inf"), 0.20)]
NIIT_THRESHOLD_MFJ = 250_000
NIIT_RATE = 0.038

# ---- Model assumptions ---------------------------------------------------------
INFLATION_DEFAULT = 0.025
SS_COLA_DEFAULT = 0.025
# California Prop 13 caps the assessed value increase at 2%/yr, so property tax
# grows more slowly than general inflation. Homeowners insurance has been doing
# the opposite — CA premiums have risen far faster than CPI, so it gets its own
# (deliberately conservative) rate.
PROPERTY_TAX_GROWTH = 0.02
INSURANCE_INFLATION = 0.06
# (nominal mean return, annual std dev)
RISK_PRESETS = {
    "conservative": (0.045, 0.08),
    "moderate": (0.058, 0.11),
    "aggressive": (0.070, 0.15),
}
SCENARIO_SPREAD = 0.02   # optimistic/pessimistic = mean +/- this


def bracket_tax(taxable: float, brackets: list[tuple[float, float]]) -> float:
    """Progressive tax on `taxable` using (upper_bound, rate) bracket list."""
    tax, lower = 0.0, 0.0
    for upper, rate in brackets:
        if taxable <= lower:
            break
        tax += (min(taxable, upper) - lower) * rate
        lower = upper
    return tax


def marginal_rate(taxable: float, brackets: list[tuple[float, float]]) -> float:
    lower = 0.0
    for upper, rate in brackets:
        if taxable <= upper:
            return rate if taxable > lower else brackets[0][1]
        lower = upper
    return brackets[-1][1]


if __name__ == "__main__":
    # sanity self-test
    assert abs(bracket_tax(24_800, FED_MFJ) - 2_480) < 1
    t = bracket_tax(200_000, FED_MFJ)
    expected = 24_800 * .10 + (100_800 - 24_800) * .12 + (200_000 - 100_800) * .22
    assert abs(t - expected) < 1, t
    assert marginal_rate(200_000, FED_MFJ) == 0.22
    assert marginal_rate(500_000, FED_MFJ) == 0.32
    assert UNIFORM_LIFETIME[75] == 24.6
    print("taxdata self-test OK")
