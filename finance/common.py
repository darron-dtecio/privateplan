"""Shared plumbing for the personal-finance app.

PII policy (updated 2026-08-01 per user): everything under finance_data/ is
gitignored personal data. Identity PII — SSN, names, addresses, phone
numbers, emails, DOB, employee/account/loan numbers — is scrubbed by
redact.py from every parsed document BEFORE storage, so it must never
appear in extracted JSON, diagnostics, or logs. Financial values (pay
amounts, balances, benefit estimates, holdings/quantities) ARE allowed in
extraction output and diagnostics and flow into the analysis automatically.
Raw inbox documents are the only unredacted artifacts — parse them only
through extract.py, never print their contents.
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIN_DATA = ROOT / "finance_data"
INBOX = FIN_DATA / "inbox"
EXTRACTED = FIN_DATA / "extracted"
LOGS = FIN_DATA / "logs"
PROFILE_PATH = FIN_DATA / "profile.json"
ANALYSIS_PATH = FIN_DATA / "analysis.json"
DASHBOARD_PATH = FIN_DATA / "dashboard.html"
LINKS_PATH = FIN_DATA / "links.txt"

# pipeline/ modules import each other flat (script-dir on sys.path); mirror that
# so finance code can reuse sources.py parsers and render.py chart builders.
sys.path.insert(0, str(ROOT / "pipeline"))


def ensure_dirs() -> None:
    for d in (FIN_DATA, INBOX, EXTRACTED, LOGS):
        d.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


PROFILE_VERSION = 2

# Neutral defaults used when a profile field is absent and as the seed values
# on the intake form.  They match the fictional household in samples/ so the
# empty form and the sample data tell one consistent story.
DEFAULT_HOUSEHOLD = {
    "self_birthdate": "1978-04-12",
    "spouse_birthdate": "1979-09-30",
    "state": "CA",
    "filing_status": "mfj",
    "retirement_date": "2040-06",
}


def migrate_profile(profile: dict | None) -> dict | None:
    """Return a v2 profile without mutating the document-derived source.

    Version 1 profiles stored healthcare inside the single retirement-spending
    number and had no representation for capital replacements.  The migration
    adds explicit, editable assumptions while retaining every unknown/derived
    field so an intake-form round trip cannot discard extraction work.
    """
    if profile is None:
        return None
    p = deepcopy(profile)
    p["version"] = PROFILE_VERSION
    spending = p.setdefault("spending", {})
    detail = p.get("spending_detail") or {}
    recurring = detail.get("recurring") or {}
    cats = recurring.get("categories") or []
    observed_medical = next(
        (float(c.get("monthly") or 0) for c in cats if c.get("name") == "medical"),
        float(spending.get("observed_medical_monthly") or 0),
    )
    spending.setdefault("observed_medical_monthly", round(observed_medical, 2))

    health = p.setdefault("healthcare", {})
    health.setdefault("source_year", 2026)
    health.setdefault("pre_medicare_monthly_per_person", 1850.0)
    health.setdefault("medicare_monthly_per_person", 700.0)
    health.setdefault("inflation", 0.06)
    health.setdefault("enabled", True)

    cap = p.setdefault("capital_expenses", {})
    cap.setdefault("home_reserve_pct", 0.01)
    cap.setdefault("home_reserve_inflation", 0.035)
    cap.setdefault("home_reserve_start", "retirement")
    schedules = cap.setdefault("schedules", [])
    if not any(s.get("id") == "vehicle-replacement" for s in schedules):
        schedules.append({
            "id": "vehicle-replacement", "name": "Vehicle replacement",
            "amount_today": 50000.0,
            "first_date": (p.get("household", {}).get("retirement_date")
                           or DEFAULT_HOUSEHOLD["retirement_date"]),
            "interval_years": 10, "inflation": None, "end_year": None,
            "enabled": True,
        })
    assumptions = p.setdefault("assumptions", {})
    assumptions.setdefault("monte_carlo_paths", 10000)
    assumptions.setdefault("taxable_basis_unknown_is_gain", True)
    return p


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")


def field(value=None, raw=None, source=None, confidence: float = 0.0) -> dict:
    """One extracted datum with provenance and a review flag for the intake form."""
    return {"value": value, "raw": raw, "source": source, "confidence": confidence,
            "needs_review": confidence < 0.8 or value is None}


def diag(msg: str) -> None:
    """Diagnostics printer. Financial values are OK; identity PII never is —
    only pass content that has been through redact.py (see module docstring)."""
    print(msg, flush=True)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


if __name__ == "__main__":
    legacy = {
        "version": 1,
        "spending": {"retirement_monthly_today": 7000},
        "spending_detail": {"recurring": {"categories": [
            {"name": "medical", "monthly": 452}]}},
        "payroll_detail": {"keep": True}, "holdings": [{"symbol": "TEST"}],
        "mortgage": {"escrow_detail": {"property_tax_annual": 10000}},
    }
    upgraded = migrate_profile(legacy)
    assert upgraded["version"] == 2
    assert upgraded["spending"]["observed_medical_monthly"] == 452
    assert upgraded["payroll_detail"] == legacy["payroll_detail"]
    assert upgraded["holdings"] == legacy["holdings"]
    assert upgraded["mortgage"]["escrow_detail"] == legacy["mortgage"]["escrow_detail"]
    assert (upgraded["capital_expenses"]["schedules"][0]["first_date"]
            == DEFAULT_HOUSEHOLD["retirement_date"])
    assert legacy["version"] == 1  # migration must not mutate its input
    print("common self-test OK")
