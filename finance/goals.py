"""Structured financial goals and deterministic evaluation.

Goals remain separate from document-derived profile data. Chat may propose
changes, but only an explicitly confirmed proposal writes this file.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import common
import montecarlo
import projection

GOALS_PATH = common.FIN_DATA / "goals.json"
VERSION = 1
TYPES = {"retirement", "success_probability", "legacy", "debt_payoff",
         "cash_reserve", "major_purchase"}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=1, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def defaults(profile: dict) -> dict:
    hh, spend = profile.get("household", {}), profile.get("spending", {})
    return {"version": VERSION, "updated_at": common.now_iso(), "goals": [
        {"id": "retirement-plan", "type": "retirement", "title": "Retirement plan",
         "priority": "high", "enabled": True,
         "target": {"date": hh.get("retirement_date"),
                    "monthly_spending_today": spend.get("retirement_monthly_today")}},
        {"id": "retirement-confidence", "type": "success_probability",
         "title": "Retirement confidence", "priority": "high", "enabled": True,
         "target": {"minimum_probability": 0.85}},
    ]}


def load(profile: dict | None = None, create: bool = False) -> dict:
    value = common.load_json(GOALS_PATH)
    if value:
        return validate_store(value)
    value = defaults(profile or common.load_json(common.PROFILE_PATH) or {})
    if create:
        _atomic_json(GOALS_PATH, value)
    return value


def validate_store(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("goals"), list):
        raise ValueError("goals must contain a goals array")
    seen = set()
    clean = []
    for raw in value["goals"]:
        if not isinstance(raw, dict) or raw.get("type") not in TYPES:
            raise ValueError("unsupported goal type")
        gid = str(raw.get("id") or uuid.uuid4().hex[:12])
        if gid in seen:
            raise ValueError("duplicate goal id")
        seen.add(gid)
        clean.append({"id": gid, "type": raw["type"],
                      "title": str(raw.get("title") or raw["type"].replace("_", " "))[:100],
                      "priority": raw.get("priority") if raw.get("priority") in
                                  {"high", "medium", "low"} else "medium",
                      "enabled": bool(raw.get("enabled", True)),
                      "target": validate_target(raw["type"], raw.get("target") or {})})
    return {"version": VERSION, "updated_at": value.get("updated_at") or common.now_iso(),
            "goals": clean}


def _number(value, low=0.0, high=1e12):
    n = float(value)
    if not low <= n <= high:
        raise ValueError("goal value outside allowed range")
    return n


def _date(value) -> str:
    text = str(value or "")
    datetime.strptime(text[:7], "%Y-%m")
    return text[:7]


def validate_target(kind: str, target: dict) -> dict:
    if kind == "retirement":
        return {"date": _date(target.get("date")),
                "monthly_spending_today": _number(target.get("monthly_spending_today"))}
    if kind == "success_probability":
        return {"minimum_probability": _number(target.get("minimum_probability", .85), 0, 1)}
    if kind == "legacy":
        return {"target_balance_today": _number(target.get("target_balance_today")),
                "target_age": int(_number(target.get("target_age", 95), 50, 120))}
    if kind == "debt_payoff":
        return {"liability_name": str(target.get("liability_name") or "Mortgage")[:100],
                "date": _date(target.get("date"))}
    if kind == "cash_reserve":
        return {"months_of_spending": _number(target.get("months_of_spending", 6), 0, 60)}
    if kind == "major_purchase":
        return {"amount_today": _number(target.get("amount_today")),
                "date": _date(target.get("date"))}
    raise ValueError("unsupported goal type")


def save(value: dict) -> dict:
    clean = validate_store({**value, "updated_at": common.now_iso()})
    _atomic_json(GOALS_PATH, clean)
    return clean


def _status(actual, target, higher=True) -> tuple[str, float | None]:
    if actual is None:
        return "unknown", None
    gap = actual - target if higher else target - actual
    return ("on_track" if gap >= 0 else "off_track"), gap


def evaluate(profile: dict, analysis: dict, store: dict | None = None) -> dict:
    store = store or load(profile)
    results = []
    mc = analysis.get("monte_carlo") or {}
    proj = analysis.get("projection") or {}
    recurring = ((profile.get("spending_detail") or {}).get("recurring") or {})
    monthly = float(recurring.get("avg_monthly_recent12")
                    or profile.get("spending", {}).get("current_monthly") or 0)
    cash = sum(float(a.get("balance") or 0) for a in profile.get("assets", [])
               if a.get("type") == "cash")
    for goal in store["goals"]:
        if not goal.get("enabled"):
            continue
        kind, target = goal["type"], goal["target"]
        actual = wanted = gap = None
        evidence = ""
        if kind == "retirement":
            actual = {"date": profile.get("household", {}).get("retirement_date"),
                      "monthly_spending_today": profile.get("spending", {}).get(
                          "retirement_monthly_today")}
            wanted = target
            ok = actual == wanted
            status, gap = ("on_track" if ok else "off_track"), None
            evidence = "profile.household + profile.spending"
        elif kind == "success_probability":
            actual, wanted = mc.get("success_prob"), target["minimum_probability"]
            status, gap = _status(actual, wanted)
            evidence = "analysis.monte_carlo.success_prob"
        elif kind == "legacy":
            nominal = mc.get("median_end_balance")
            assumptions = proj.get("assumptions") or {}
            years = max(int(assumptions.get("joint_horizon_year") or 0)
                        - int(analysis.get("tax_year") or 0), 0)
            inflation = float(assumptions.get("inflation") or .025)
            actual = nominal / ((1 + inflation) ** years) if nominal is not None else None
            wanted = target["target_balance_today"]
            status, gap = _status(actual, wanted)
            evidence = "analysis.monte_carlo.median_end_balance discounted to today's dollars"
        elif kind == "debt_payoff":
            mortgage = analysis.get("mortgage") or {}
            payoff = next((s.get("payoff_month") for s in mortgage.get("scenarios", [])
                           if s.get("payoff_month")), None)
            actual, wanted = payoff, target["date"]
            if actual:
                status = "on_track" if str(actual)[:7] <= wanted else "off_track"
            else:
                status = "unknown"
            evidence = "analysis.mortgage"
        elif kind == "cash_reserve":
            actual = cash / monthly if monthly else None
            wanted = target["months_of_spending"]
            status, gap = _status(actual, wanted)
            evidence = "profile.assets[cash] + spending_detail.recurring"
        else:  # major purchase: report current liquid coverage; scenario tool gives plan impact
            actual, wanted = cash, target["amount_today"]
            status, gap = _status(actual, wanted)
            evidence = "profile.assets[cash]"
        results.append({**goal, "actual": actual, "wanted": wanted,
                        "status": status, "gap": gap, "evidence": evidence})
    return {"generated": common.now_iso(), "goals": results,
            "on_track": sum(1 for g in results if g["status"] == "on_track"),
            "off_track": sum(1 for g in results if g["status"] == "off_track"),
            "unknown": sum(1 for g in results if g["status"] == "unknown")}


def apply_scenario(profile: dict, changes: dict) -> dict:
    """Return a validated profile copy; never mutate the source profile."""
    p = deepcopy(profile)
    allowed = {"retirement_date", "retirement_monthly_today", "claim_age_self",
               "claim_age_spouse", "k401_pct", "mortgage_extra_monthly",
               "annual_contributions", "inflation", "return_override", "major_purchase"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError("unsupported scenario fields: " + ", ".join(sorted(unknown)))
    if "retirement_date" in changes:
        p.setdefault("household", {})["retirement_date"] = _date(changes["retirement_date"])
    if "retirement_monthly_today" in changes:
        p.setdefault("spending", {})["retirement_monthly_today"] = _number(
            changes["retirement_monthly_today"], 0, 1e7)
    for key in ("claim_age_self", "claim_age_spouse"):
        if key in changes:
            p.setdefault("social_security", {})[key] = int(_number(changes[key], 62, 70))
    if "k401_pct" in changes:
        p.setdefault("income", {})["k401_pct"] = _number(changes["k401_pct"], 0, 100)
    if "mortgage_extra_monthly" in changes:
        p.setdefault("mortgage", {})["extra_monthly"] = _number(
            changes["mortgage_extra_monthly"], 0, 1e7)
    if "annual_contributions" in changes:
        values = changes["annual_contributions"]
        if not isinstance(values, dict) or not set(values).issubset(
                {"taxable", "deferred", "roth", "hsa"}):
            raise ValueError("annual_contributions has unsupported account buckets")
        for asset in p.get("assets", []):
            asset["annual_contribution"] = 0
        type_by_bucket = {"taxable": "brokerage", "deferred": "ira",
                          "roth": "roth", "hsa": "hsa"}
        for bucket, amount in values.items():
            p.setdefault("assets", []).append({
                "name": f"Scenario {bucket} contribution",
                "type": type_by_bucket[bucket], "balance": 0,
                "annual_contribution": _number(amount, 0, 1e7)})
    for key in ("inflation", "return_override"):
        if key in changes:
            p.setdefault("assumptions", {})[key] = _number(changes[key], -.1, .5)
    if "major_purchase" in changes:
        purchase = validate_target("major_purchase", changes["major_purchase"])
        p.setdefault("capital_expenses", {}).setdefault("schedules", []).append({
            "id": "scenario-purchase", "name": "Scenario major purchase",
            "amount_today": purchase["amount_today"], "first_date": purchase["date"],
            "interval_years": None, "inflation": None, "end_year": None, "enabled": True})
    return p


def run_scenario(profile: dict, changes: dict, probabilistic: bool = False) -> dict:
    candidate = apply_scenario(profile, changes)
    prepared = projection.prepare(candidate)
    base = projection.project(prepared, prepared["mean_return"])
    result = {"changes": changes, "depleted_at": base["depleted_at"],
              "end_balance": base["end_balance"], "min_liquid": base["min_liquid"],
              "sustainable_monthly": projection.sustainable_spending(prepared),
              "source": "deterministic projection engine"}
    if probabilistic:
        result["monte_carlo"] = montecarlo.run(prepared, n=1000, seed=42)
    return result


if __name__ == "__main__":
    sample = common.load_json(common.ROOT / "samples" / "profile.json")
    store = defaults(sample)
    assert store["goals"][1]["target"]["minimum_probability"] == .85
    changed = apply_scenario(sample, {"retirement_monthly_today": 9000})
    assert changed["spending"]["retirement_monthly_today"] == 9000
    assert sample["spending"]["retirement_monthly_today"] != 9000
    try:
        apply_scenario(sample, {"delete_files": True})
        raise AssertionError("unsupported field accepted")
    except ValueError:
        pass
    contribution = apply_scenario(sample, {"annual_contributions": {"roth": 7000}})
    assert projection.prepare(contribution)["extra_contrib"]["roth"] == 7000
    base = run_scenario(sample, {})
    purchase = run_scenario(sample, {"major_purchase": {
        "amount_today": 50000, "date": "2035-06"}})
    assert purchase == run_scenario(sample, {"major_purchase": {
        "amount_today": 50000, "date": "2035-06"}})
    assert purchase["sustainable_monthly"] < base["sustainable_monthly"]
    print("goals self-test OK")
