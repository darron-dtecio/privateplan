"""Flask blueprint for the personal-finance app (mounted at /finance).

All personal data stays under finance_data/ (gitignored). This module renders
the control page, the review/intake form, and serves the rendered dashboard;
pipeline steps run as subprocesses with the same job-polling contract as the
ticker UI.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
import hashlib
from datetime import datetime
from pathlib import Path

from flask import (Blueprint, abort, jsonify, redirect, render_template,
                   request, send_file, url_for)
from werkzeug.utils import secure_filename

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
import samples
import taxdata as td
from common import FIN_DATA, INBOX, EXTRACTED, LOGS, ROOT

fin_bp = Blueprint("finance", __name__, url_prefix="/finance")

FIN_JOBS: dict[str, dict] = {}

HERE = Path(__file__).resolve().parent
# Keep finance jobs on the repo's own environment even if the web server was
# accidentally started with the system Python.  Several steps (including live
# pricing) depend on packages installed only in that environment.  Falls back
# to the running interpreter, which is correct when a venv is already active.
_VENV_DIR = ROOT / ".venv"
_VENV_PY = (_VENV_DIR / "Scripts" / "python.exe" if sys.platform == "win32"
            else _VENV_DIR / "bin" / "python")
FIN_PY = str(_VENV_PY if _VENV_PY.is_file() else Path(sys.executable))
FIN_STEPS = {
    "download": [str(HERE / "download.py")],
    "extract": [str(HERE / "extract.py")],
    "analyze": [str(HERE / "analyze.py")],
    "render": [str(HERE / "render.py")],
    "refresh": [str(HERE / "refresh.py")],
    # runs the SEC-backed stock analyzer over every holding, then combines them
    "portfolio": [str(HERE / "portfolio.py"), "--fetch", "--render"],
    "portfolio-render": [str(HERE / "portfolio.py"), "--render"],
    # live quotes for every holding, then rebuild the combined view
    "prices": [str(HERE / "prices.py")],
    # fictional sample household -- see samples/README.md
    "load-samples": [str(HERE / "samples.py"), "--load", "full"],
    "load-sample-profile": [str(HERE / "samples.py"), "--load", "profile"],
    "load-sample-documents": [str(HERE / "samples.py"), "--load", "documents"],
}


def _age(path: Path) -> str | None:
    if not path.exists():
        return None
    delta = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if delta.days > 0:
        return f"{delta.days}d ago"
    hours = delta.seconds // 3600
    return f"{hours}h ago" if hours else f"{delta.seconds // 60}m ago"


def _newer(a: Path, b: Path) -> bool:
    """True when a was written after b — i.e. a is not stale against its input."""
    return a.exists() and b.exists() and a.stat().st_mtime >= b.stat().st_mtime


def _profile_gaps() -> list[str]:
    """Fields the profile still needs, live rather than as of the last analyze.

    Imported lazily: analyze pulls in the whole model stack, and the workspace
    page should not pay for that on every request that does not need it.
    """
    profile = common.migrate_profile(common.load_json(common.PROFILE_PATH)) or {}
    if not profile:
        return []
    try:
        import analyze
        return analyze.check_completeness(profile)
    except Exception:
        return []


def _portfolio_step() -> dict:
    """The per-holding stock analysis — a real stage, but an optional one.

    It is off the critical path: the plan is complete without it, and it takes
    minutes because it runs the SEC analyzer over every company you hold. So it
    carries its own state like any other stage but never becomes "the next thing
    to do", which would tell a first-time user to sit through it before seeing a
    plan they already have.
    """
    page = FIN_DATA / "portfolio.html"
    data = FIN_DATA / "portfolio.json"
    detail = "runs the SEC-backed analyzer over every holding — several minutes"
    if data.exists():
        p = common.load_json(data) or {}
        held = len(p.get("stocks") or []) + len(p.get("funds") or [])
        failed = len(p.get("failed") or [])
        detail = (f"{held} holding(s) analysed"
                  + (f", {failed} could not be" if failed else ""))
        if not _newer(page, common.PROFILE_PATH):
            detail += " — out of date, your holdings changed since"
    return {"id": "portfolio", "label": "Portfolio analysis", "step": "portfolio",
            "href": "/finance/portfolio" if page.exists() else None,
            "optional": True,
            "done": _newer(page, common.PROFILE_PATH),
            "detail": detail, "when": _age(page)}


def _pipeline_steps(inbox_files: list[dict], summary: dict) -> list[dict]:
    """The pipeline as a path with a state per stage, not a table of timestamps.

    It is a linear five-step flow, plus the optional portfolio analysis at the
    end. Presented as a status table beside seven equally weighted buttons,
    nothing tells a first-time user which one to press — so each step carries
    its own state and the page promotes exactly one.
    """
    routed = [f for f in inbox_files if f.get("pipeline")]
    unrouted = len(inbox_files) - len(routed)
    gaps = _profile_gaps()
    steps = [
        {"id": "documents", "label": "Documents", "step": None,
         "done": bool(inbox_files),
         "detail": (f"{len(inbox_files)} file(s) in the inbox" if inbox_files
                    else "nothing to read yet — add documents below"),
         "when": _age(INBOX) if inbox_files else None},
        {"id": "extract", "label": "Extract", "step": "extract",
         "done": (EXTRACTED / "summary.json").exists(),
         "detail": (f"{len(routed)} routed"
                    + (f", {unrouted} unrecognised" if unrouted else "")
                    if (EXTRACTED / "summary.json").exists()
                    else "reads each document into structured fields"),
         "when": _age(EXTRACTED / "summary.json")},
        {"id": "profile", "label": "Review profile", "step": None,
         "href": "/finance/intake",
         "done": common.PROFILE_PATH.exists() and not gaps,
         "detail": (f"{len(gaps)} field(s) still needed" if gaps
                    else "everything the documents cannot know is filled in"
                    if common.PROFILE_PATH.exists()
                    else "add what documents cannot know"),
         "when": _age(common.PROFILE_PATH)},
        {"id": "analyze", "label": "Analyze", "step": "analyze",
         "done": _newer(common.ANALYSIS_PATH, common.PROFILE_PATH),
         "detail": ("out of date — the profile changed since"
                    if common.ANALYSIS_PATH.exists()
                    and not _newer(common.ANALYSIS_PATH, common.PROFILE_PATH)
                    else "projection, Monte Carlo, tax, Social Security, fees"),
         "when": _age(common.ANALYSIS_PATH)},
        {"id": "render", "label": "Render dashboard", "step": "render",
         "href": "/finance/dashboard" if common.DASHBOARD_PATH.exists() else None,
         "done": _newer(common.DASHBOARD_PATH, common.ANALYSIS_PATH),
         "detail": ("out of date — the analysis changed since"
                    if common.DASHBOARD_PATH.exists()
                    and not _newer(common.DASHBOARD_PATH, common.ANALYSIS_PATH)
                    else "builds the page you read the plan on"),
         "when": _age(common.DASHBOARD_PATH)},
        _portfolio_step(),
    ]
    # Exactly one step is "current": the first thing on the required path that
    # is not finished. An optional stage never claims it.
    current = next((s for s in steps if not s["done"] and not s.get("optional")), None)
    for s in steps:
        s["state"] = ("done" if s["done"]
                      else "current" if s is current
                      else "optional" if s.get("optional") else "pending")
    return steps


# State options come from the shipped tax tables, so adding a state file is
# all it takes to offer it here -- see docs/ADDING_A_STATE.md.
_STATE_OPTIONS = ",".join(f"{code}|{name} ({code})"
                          for code, name in td.available_states())
_FILING_OPTIONS = ",".join(
    f"{code}|{td.federal(code)['label']}" for code in td.FILING_STATUSES)

# ---------------------------------------------------------------- intake spec --
# kind: text | date | month | number | pctfrac (input % -> stored fraction)
#       | pctnum (stored as the percent number) | select:opt1,opt2
SECTIONS = [
    ("Household", [
        ("self_birthdate", "Your birthdate", "date",
         common.DEFAULT_HOUSEHOLD["self_birthdate"]),
        ("spouse_birthdate", "Spouse birthdate", "date", ""),
        ("state", "State of residence", f"select:{_STATE_OPTIONS}",
         common.DEFAULT_HOUSEHOLD["state"]),
        ("filing_status", "Federal filing status", f"select:{_FILING_OPTIONS}",
         common.DEFAULT_HOUSEHOLD["filing_status"]),
        ("retirement_date", "Planned retirement (YYYY-MM)", "month",
         common.DEFAULT_HOUSEHOLD["retirement_date"]),
    ]),
    ("Income (from paystub — verify)", [
        ("salary_annual", "Your gross salary, annual ($)", "number", ""),
        ("pay_frequency", "Pay frequency", "select:biweekly,semimonthly,monthly,weekly", "biweekly"),
        ("k401_pct", "401(k) contribution (% of salary)", "pctnum", ""),
        ("employer_match_annual", "Employer 401(k) match, $ per year "
         "(preferred — use this if your plan matches a % of YOUR contribution)", "number", ""),
        ("employer_match_pct", "…or employer match as % of SALARY "
         "(ignored when the dollar amount above is set)", "pctnum", ""),
        ("other_pretax_annual", "Other pre-tax deductions, annual ($ — health premiums, HSA…)", "number", "0"),
        ("spouse_income_annual", "Spouse gross income, annual ($)", "number", "0"),
    ]),
    ("Social Security (from SSA statement — verify)", [
        ("ss_62", "Your monthly benefit claiming at 62 ($)", "number", ""),
        ("ss_67", "Your monthly benefit at FRA 67 ($)", "number", ""),
        ("ss_70", "Your monthly benefit claiming at 70 ($)", "number", ""),
        ("spouse_own_monthly_fra", "Spouse's own monthly benefit at their FRA ($)", "number", "300"),
        ("claim_age_self", "Claim age to model for you", "select:62,63,64,65,66,67,68,69,70", "67"),
        ("claim_age_spouse", "Claim age to model for spouse", "select:62,63,64,65,66,67,68,69,70", "67"),
    ]),
    ("Mortgage (from statement — verify)", [
        ("mortgage_balance", "Principal balance ($)", "number", ""),
        ("mortgage_rate", "Interest rate (%)", "pctfrac", ""),
        ("mortgage_pi_payment", "Principal & interest payment, monthly ($)", "number", ""),
        ("mortgage_escrow_payment", "Escrow payment, monthly ($)", "number", ""),
        ("mortgage_next_due", "Next payment (YYYY-MM)", "month", ""),
        ("mortgage_maturity", "Maturity (YYYY-MM)", "month", ""),
        ("home_value", "Home market value ($, estimate)", "number", ""),
    ]),
    ("Spending", [
        ("current_monthly", "Current total spending, monthly ($)", "number", ""),
        ("retirement_monthly_today", "Desired retirement spending, monthly, today's $ "
         "(EXCLUDE mortgage P&I; include property tax/insurance/healthcare)", "number", ""),
    ]),
    ("Retirement healthcare (2026-dollar estimates)", [
        ("health_pre_medicare", "Pre-Medicare cost, monthly per person ($)", "number", "1850"),
        ("health_medicare", "Medicare cost, monthly per person ($)", "number", "700"),
        ("health_inflation", "Healthcare inflation (%)", "pctfrac", "6"),
        ("observed_medical_monthly", "Observed medical spending already in baseline ($/mo)", "number", "0"),
    ]),
    ("Major home replacements", [
        ("home_reserve_pct", "Annual reserve as % of home value", "pctfrac", "1"),
        ("home_reserve_inflation", "Home-capital inflation (%)", "pctfrac", "3.5"),
    ]),
    ("Assumptions", [
        ("risk", "Risk profile", "select:conservative,moderate,aggressive", "moderate"),
        ("inflation", "Inflation (%)", "pctfrac", "2.5"),
        ("cola", "Social Security COLA (%)", "pctfrac", "2.5"),
        ("horizon_age", "Plan to age", "number", "95"),
    ]),
]

ASSET_TYPES = ["401k", "trad_ira", "roth", "hsa", "brokerage", "annuity",
               "cash", "other"]

# summary.json canonical field -> intake form field
SUMMARY_MAP = {
    "salary_annual": "salary_annual", "pay_frequency": "pay_frequency",
    "k401_pct": "k401_pct", "state_hint": "state",
    "principal_balance": "mortgage_balance", "interest_rate": "mortgage_rate",
    "pi_payment": "mortgage_pi_payment", "escrow_payment": "mortgage_escrow_payment",
    "maturity_date": "mortgage_maturity",
    "benefit_62": "ss_62", "benefit_67": "ss_67", "benefit_70": "ss_70",
}


def _flatten_profile(p: dict) -> dict:
    """profile.json -> flat {form_field: value} for prefill."""
    p = common.migrate_profile(p)
    out = {}
    hh, inc = p.get("household", {}), p.get("income", {})
    ss, m = p.get("social_security", {}), p.get("mortgage", {}) or {}
    sp, a = p.get("spending", {}), p.get("assumptions", {})
    hc, cap = p.get("healthcare", {}), p.get("capital_expenses", {})
    out.update({k: hh.get(k) for k in ("self_birthdate", "spouse_birthdate",
                                       "state", "filing_status", "retirement_date")})
    out.update({k: inc.get(k) for k in ("salary_annual", "pay_frequency", "k401_pct",
                                        "employer_match_pct", "employer_match_annual",
                                        "other_pretax_annual", "spouse_income_annual")})
    for age in (62, 67, 70):
        out[f"ss_{age}"] = (ss.get("self") or {}).get(str(age))
    out["spouse_own_monthly_fra"] = ss.get("spouse_own_monthly_fra")
    out["claim_age_self"] = ss.get("claim_age_self")
    out["claim_age_spouse"] = ss.get("claim_age_spouse")
    out["mortgage_balance"] = m.get("balance")
    out["mortgage_rate"] = round(m["rate"] * 100, 4) if m.get("rate") else None
    out["mortgage_pi_payment"] = m.get("pi_payment")
    out["mortgage_escrow_payment"] = m.get("escrow_payment")
    out["mortgage_next_due"] = m.get("next_due")
    out["mortgage_maturity"] = m.get("maturity")
    out["home_value"] = (p.get("home") or {}).get("value")
    out["current_monthly"] = sp.get("current_monthly")
    out["retirement_monthly_today"] = sp.get("retirement_monthly_today")
    out["observed_medical_monthly"] = sp.get("observed_medical_monthly")
    out["health_pre_medicare"] = hc.get("pre_medicare_monthly_per_person")
    out["health_medicare"] = hc.get("medicare_monthly_per_person")
    out["health_inflation"] = round(hc["inflation"] * 100, 2) if hc.get("inflation") else None
    out["home_reserve_pct"] = round(cap["home_reserve_pct"] * 100, 3) if cap.get("home_reserve_pct") else None
    out["home_reserve_inflation"] = round(cap["home_reserve_inflation"] * 100, 2) if cap.get("home_reserve_inflation") else None
    out["risk"] = a.get("risk")
    out["inflation"] = round(a["inflation"] * 100, 2) if a.get("inflation") else None
    out["cola"] = round(ss["cola"] * 100, 2) if ss.get("cola") else None
    out["horizon_age"] = a.get("horizon_age")
    return {k: v for k, v in out.items() if v is not None}


def _prefill() -> tuple[dict, dict, list, list, list]:
    """(values, review_flags, assets, liabilities, schedules)."""
    values: dict = {}
    review: dict = {}
    assets: list = []
    liabilities: list = []
    schedules: list = []

    summary = common.load_json(EXTRACTED / "summary.json") or {}
    for canon, form_field in SUMMARY_MAP.items():
        f = summary.get("fields", {}).get(canon)
        if f and f.get("value") is not None:
            v = f["value"]
            if canon == "interest_rate" and isinstance(v, (int, float)) and v < 1:
                v = round(v * 100, 4)  # stored as fraction -> show %
            values[form_field] = v
            review[form_field] = {"flag": f.get("needs_review", True),
                                  "src": (f.get("source") or {}).get("file", "extracted")}
    for cand in (summary.get("account_candidates") or [])[:12]:
        assets.append({"name": cand.get("label"), "type": "other",
                       "balance": cand.get("value"), "annual_contribution": 0})

    profile = common.migrate_profile(common.load_json(common.PROFILE_PATH))
    if profile:
        auto = profile.get("_auto", {})
        for k, v in _flatten_profile(profile).items():
            values[k] = v
            if k in auto:
                # auto-ingested from documents; keep an informational badge
                review[k] = {"flag": review.get(k, {}).get("flag", False),
                             "src": "documents (auto)"}
            else:
                review.pop(k, None)  # user-entered
        assets = profile.get("assets") or assets
        liabilities = profile.get("liabilities") or []
        schedules = (profile.get("capital_expenses") or {}).get("schedules") or []
    return values, review, assets, liabilities, schedules


def _num(form, key, default=None):
    raw = (form.get(key) or "").replace(",", "").replace("$", "").strip()
    if raw == "":
        return default
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return default


def _build_profile(form) -> dict:
    def pctfrac(key):
        v = _num(form, key)
        return round(v / 100, 6) if v is not None else None

    def _at(lst, i, default=""):
        return lst[i] if i < len(lst) else default

    assets = []
    srcs = form.getlist("asset_source")
    for i, name in enumerate(form.getlist("asset_name")):
        if not name.strip():
            continue
        row = {
            "name": name.strip(),
            "type": _at(form.getlist("asset_type"), i, "other"),
            "balance": _num({"v": _at(form.getlist("asset_balance"), i)}, "v", 0),
            "annual_contribution": _num({"v": _at(form.getlist("asset_contrib"), i)}, "v", 0),
            "cost_basis": _num({"v": _at(form.getlist("asset_basis"), i)}, "v"),
        }
        # keep the auto-managed marker across the form round-trip, otherwise
        # document-sourced rows become user-owned and can never be refreshed
        # (which silently duplicates them on the next extract)
        if _at(srcs, i) == "auto":
            row["source"] = "auto"
        assets.append(row)
    liabilities = []
    lsrcs = form.getlist("liab_source")
    for i, name in enumerate(form.getlist("liab_name")):
        if not name.strip():
            continue
        rate = _num({"v": _at(form.getlist("liab_rate"), i)}, "v")
        row = {
            "name": name.strip(),
            "balance": _num({"v": _at(form.getlist("liab_balance"), i)}, "v", 0),
            "rate": round(rate / 100, 6) if rate is not None else None,
            "payment_monthly": _num({"v": _at(form.getlist("liab_payment"), i)}, "v"),
        }
        if _at(lsrcs, i) == "auto":
            row["source"] = "auto"
        liabilities.append(row)

    schedules = []
    for i, name in enumerate(form.getlist("schedule_name")):
        if not name.strip():
            continue
        interval = _num({"v": _at(form.getlist("schedule_interval"), i)}, "v")
        inflation = _num({"v": _at(form.getlist("schedule_inflation"), i)}, "v")
        end_year = _num({"v": _at(form.getlist("schedule_end_year"), i)}, "v")
        schedules.append({
            "id": _at(form.getlist("schedule_id"), i) or f"scheduled-{i + 1}",
            "name": name.strip(),
            "amount_today": _num({"v": _at(form.getlist("schedule_amount"), i)}, "v", 0),
            "first_date": _at(form.getlist("schedule_first_date"), i) or None,
            "interval_years": int(interval) if interval else None,
            "inflation": round(inflation / 100, 6) if inflation is not None else None,
            "end_year": int(end_year) if end_year else None,
            "enabled": True,
        })

    mortgage = None
    if _num(form, "mortgage_balance"):
        mortgage = {"balance": _num(form, "mortgage_balance"),
                    "rate": pctfrac("mortgage_rate"),
                    "pi_payment": _num(form, "mortgage_pi_payment"),
                    "escrow_payment": _num(form, "mortgage_escrow_payment"),
                    "next_due": form.get("mortgage_next_due") or None,
                    "maturity": form.get("mortgage_maturity") or None}
        if not any(l["name"].lower().startswith("mortgage") for l in liabilities):
            liabilities.insert(0, {"name": "Mortgage", "balance": mortgage["balance"],
                                   "rate": mortgage["rate"],
                                   "payment_monthly": (mortgage["pi_payment"] or 0)
                                   + (mortgage["escrow_payment"] or 0)})

    return {
        "version": common.PROFILE_VERSION, "saved_at": common.now_iso(),
        "household": {"self_birthdate": (form.get("self_birthdate")
                                      or common.DEFAULT_HOUSEHOLD["self_birthdate"]),
                      "spouse_birthdate": form.get("spouse_birthdate") or None,
                      "state": form.get("state") or common.DEFAULT_HOUSEHOLD["state"],
                      "filing_status": (form.get("filing_status")
                                        or common.DEFAULT_HOUSEHOLD["filing_status"]),
                      "retirement_date": (form.get("retirement_date")
                                          or common.DEFAULT_HOUSEHOLD["retirement_date"])},
        "income": {"salary_annual": _num(form, "salary_annual", 0),
                   "pay_frequency": form.get("pay_frequency") or "biweekly",
                   "k401_pct": _num(form, "k401_pct", 0),
                   "employer_match_pct": _num(form, "employer_match_pct", 0),
                   "employer_match_annual": _num(form, "employer_match_annual"),
                   "other_pretax_annual": _num(form, "other_pretax_annual", 0),
                   "spouse_income_annual": _num(form, "spouse_income_annual", 0)},
        "social_security": {
            "self": {str(a): _num(form, f"ss_{a}") for a in (62, 67, 70)},
            "spouse_own_monthly_fra": _num(form, "spouse_own_monthly_fra", 0),
            "claim_age_self": int(form.get("claim_age_self") or 67),
            "claim_age_spouse": int(form.get("claim_age_spouse") or 67),
            "cola": pctfrac("cola") or 0.025},
        "mortgage": mortgage,
        "home": {"value": _num(form, "home_value", 0)},
        "assets": assets, "liabilities": liabilities,
        "spending": {"current_monthly": _num(form, "current_monthly", 0),
                     "retirement_monthly_today": _num(form, "retirement_monthly_today", 0),
                     "observed_medical_monthly": _num(form, "observed_medical_monthly", 0)},
        "healthcare": {"enabled": True, "source_year": 2026,
                       "pre_medicare_monthly_per_person": _num(form, "health_pre_medicare", 1850),
                       "medicare_monthly_per_person": _num(form, "health_medicare", 700),
                       "inflation": pctfrac("health_inflation") or 0.06},
        "capital_expenses": {"home_reserve_pct": pctfrac("home_reserve_pct") or 0,
                             "home_reserve_inflation": pctfrac("home_reserve_inflation") or 0.035,
                             "home_reserve_start": "retirement",
                             "schedules": schedules},
        "assumptions": {"risk": form.get("risk") or "moderate",
                        "inflation": pctfrac("inflation") or 0.025,
                        "horizon_age": int(_num(form, "horizon_age", 95)),
                        "return_override": None,
                        "compare_claim_ages": [62, 67, 70],
                        "monte_carlo_paths": 10000,
                        "taxable_basis_unknown_is_gain": True},
    }


def _preserve_ingested(profile: dict, old: dict) -> dict:
    """Carry document-derived structures through an editable-form save."""
    for key in ("_auto", "holdings", "spending_detail", "payroll_detail",
                "investment_activity", "equity_comp"):
        if key in old:
            profile[key] = old[key]
    if old.get("mortgage", {}).get("escrow_detail") and profile.get("mortgage"):
        profile["mortgage"]["escrow_detail"] = old["mortgage"]["escrow_detail"]
    old_ss = old.get("social_security", {})
    if old_ss.get("spouse"):
        profile["social_security"]["spouse"] = old_ss["spouse"]
        profile["social_security"]["spouse_source"] = old_ss.get("spouse_source")
    return profile


# --------------------------------------------------------------------- routes --
@fin_bp.get("")
def home():
    common.ensure_dirs()
    summary = common.load_json(EXTRACTED / "summary.json") or {}
    analysis = common.load_json(common.ANALYSIS_PATH) or {}
    extracted_by_name = {d.get("name"): d for d in summary.get("source_documents", [])}
    analyzed_by_hash = {d.get("sha256"): d for d in analysis.get("source_documents", [])}

    def shown_time(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).strftime("%b %d, %Y %I:%M %p")
        except ValueError:
            return value

    inbox_files = []
    for p in sorted(INBOX.iterdir()):
        if not p.is_file():
            continue
        meta = extracted_by_name.get(p.name) or {}
        analyzed = analyzed_by_hash.get(meta.get("sha256")) or {}
        matched = meta.get("same_structure_as") or []
        pipeline = meta.get("pipeline")
        # One row, one plain state. The five columns it replaces — format,
        # pipeline, extracted, included, integration check — asked the reader to
        # reconcile them into exactly this sentence.
        if pipeline and analyzed.get("included_at"):
            state, state_cls = "in the plan", "good"
        elif pipeline:
            state, state_cls = "read, not yet analysed", "warn"
        elif meta.get("extracted_at"):
            state, state_cls = "not used", "warn"
        else:
            state, state_cls = "not read yet", ""
        kind = (pipeline or "").replace("_", " ")
        detail = ", ".join(filter(None, [
            kind or (meta.get("format") or "unrecognised format"),
            (f"{len(matched)} structural match(es)" if matched else None),
            (f"dedupe: {meta.get('dedupe_strategy')}" if meta.get("dedupe_strategy") else None),
            f"{p.stat().st_size // 1024} KB",
        ]))
        inbox_files.append({"name": p.name, "kb": p.stat().st_size // 1024,
                            "format": meta.get("format"),
                            "pipeline": pipeline,
                            "extracted_at": shown_time(meta.get("extracted_at")),
                            "included_at": shown_time(analyzed.get("included_at")),
                            "matched": len(matched),
                            "state": state, "state_cls": state_cls, "detail": detail,
                            "dedupe": meta.get("dedupe_strategy")})
    extracted = sorted(p.name for p in EXTRACTED.glob("*.json"))
    steps = _pipeline_steps(inbox_files, summary)
    next_step = next((s for s in steps if s["state"] == "current"), None)
    running = any(j["proc"].poll() is None for j in FIN_JOBS.values())
    return render_template("finance.html.j2", inbox=inbox_files, steps=steps,
                           next_step=next_step,
                           portfolio_age=_age(FIN_DATA / "portfolio.html"),
                           extracted=extracted, links_exists=common.LINKS_PATH.exists(),
                           running=running,
                           has_dashboard=common.DASHBOARD_PATH.exists(),
                           has_portfolio=(FIN_DATA / "portfolio.html").exists(),
                           has_profile=common.PROFILE_PATH.exists(),
                           uploaded=request.args.get("uploaded", type=int),
                           duplicates=request.args.get("duplicates", type=int))


@fin_bp.post("/upload")
def upload():
    common.ensure_dirs()
    n = duplicates = 0
    known_hashes = {hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in INBOX.iterdir() if p.is_file()}
    for f in request.files.getlist("docs"):
        if f and f.filename:
            content = f.read()
            digest = hashlib.sha256(content).hexdigest()
            if digest in known_hashes:
                duplicates += 1
                continue
            name = secure_filename(f.filename)
            destination = INBOX / name
            counter = 2
            while destination.exists():
                destination = INBOX / f"{Path(name).stem}_{counter}{Path(name).suffix}"
                counter += 1
            destination.write_bytes(content)
            known_hashes.add(digest)
            n += 1
    links = (request.form.get("links") or "").strip()
    if links:
        common.LINKS_PATH.write_text(links + "\n", encoding="utf-8")
    # Parse immediately so format/schema compatibility and dedupe routing are
    # visible on the source list without requiring a separate manual step.
    if n and not any(j["proc"].poll() is None for j in FIN_JOBS.values()):
        subprocess.run([FIN_PY, str(HERE / "extract.py")], cwd=ROOT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=120, check=False)
    return redirect(url_for("finance.home", uploaded=n, duplicates=duplicates))


@fin_bp.post("/run/<step>")
def run_step(step):
    if step not in FIN_STEPS:
        abort(400, "unknown step")
    return jsonify(_start_job(step))


def _start_job(step: str) -> dict:
    if any(j["proc"].poll() is None for j in FIN_JOBS.values()):
        abort(409, "A finance job is already running.")
    common.ensure_dirs()
    log_path = LOGS / f"{step}_{datetime.now():%Y%m%d_%H%M%S}.log"
    log_fh = open(log_path, "w", encoding="utf-8")
    cmd = [FIN_PY] + FIN_STEPS[step]
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log_fh, stderr=subprocess.STDOUT)
    job_id = uuid.uuid4().hex[:12]
    FIN_JOBS[job_id] = {"proc": proc, "log": log_path, "step": step,
                        "started": datetime.now().isoformat(timespec="seconds")}
    return {"job_id": job_id}


@fin_bp.post("/reset")
def reset():
    """Move everything under finance_data/ to .trash/, optionally reloading samples.

    Deliberately not a FIN_STEPS entry: those fire from a plain click with no
    confirmation, which is right for "recompute" and wrong for "delete
    everything I have entered".  This wants a typed confirmation, a same-origin
    check, and no job running underneath it.
    """
    if (request.form.get("confirm") or "").strip().upper() != "DELETE":
        abort(400, "type DELETE to confirm")
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/").split("://", 1)[-1] != request.host:
        abort(403, "cross-origin request rejected")
    if any(j["proc"].poll() is None for j in FIN_JOBS.values()):
        abort(409, "A finance job is already running.")

    result = samples.clear(keep_documents=bool(request.form.get("keep_documents")))
    if request.form.get("then_load"):
        _start_job("load-samples")
    return redirect(url_for("finance.home", reset=len(result["removed"]),
                            trash=Path(result["trash"]).name))


@fin_bp.get("/job/<job_id>")
def job_status(job_id):
    job = FIN_JOBS.get(job_id)
    if not job:
        abort(404)
    rc = job["proc"].poll()
    try:
        tail = job["log"].read_text(encoding="utf-8", errors="replace")[-6000:]
    except OSError:
        tail = ""
    return jsonify({"status": "running" if rc is None
                    else ("done" if rc == 0 else "failed"),
                    "returncode": rc, "step": job["step"], "log_tail": tail})


@fin_bp.get("/intake")
def intake():
    common.ensure_dirs()
    values, review, assets, liabilities, schedules = _prefill()
    return render_template("finance_intake.html.j2", sections=SECTIONS,
                           values=values, review=review, assets=assets,
                           liabilities=liabilities, schedules=schedules,
                           asset_types=ASSET_TYPES)


@fin_bp.post("/intake")
def intake_save():
    common.ensure_dirs()
    profile = _build_profile(request.form)
    # carry over auto-ingest bookkeeping and workbook holdings
    old = common.migrate_profile(common.load_json(common.PROFILE_PATH)) or {}
    _preserve_ingested(profile, old)
    common.save_json(common.PROFILE_PATH, profile)
    return redirect(url_for("finance.home"))


@fin_bp.get("/portfolio")
def portfolio():
    path = common.FIN_DATA / "portfolio.html"
    if not path.exists():
        abort(404, "No portfolio view yet — run the Portfolio step.")
    return send_file(path)


@fin_bp.get("/dashboard")
def dashboard():
    if not common.DASHBOARD_PATH.exists():
        abort(404, "No dashboard yet — save the intake form, then run Analyze and Render.")
    return send_file(common.DASHBOARD_PATH)


if __name__ == "__main__":
    old = {"_auto": {"salary_annual": 1}, "holdings": [{"symbol": "TEST"}],
           "spending_detail": {"source_months": 12},
           "payroll_detail": {"periods_per_year": 26},
           "investment_activity": {"n_months": 6},
           "mortgage": {"escrow_detail": {"property_tax_annual": 10000}}}
    new = {"mortgage": {"balance": 1}}
    _preserve_ingested(new, old)
    for key in ("_auto", "holdings", "spending_detail", "payroll_detail",
                "investment_activity"):
        assert new[key] == old[key]
    assert new["mortgage"]["escrow_detail"] == old["mortgage"]["escrow_detail"]
    print("web self-test OK")
