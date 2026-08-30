"""Auto-apply extracted document data to finance_data/profile.json.

Policy: document-derived fields flow into the profile automatically. User
edits win — a field is only auto-updated while its current value still
matches what auto-ingest last wrote (tracked in profile["_auto"]). Once the
user overrides a value in the intake form, auto-ingest stops touching it.

Returns a list of human-readable change lines (financial values allowed;
inputs are already redacted upstream).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

# form-ish key -> (summary canonical field, profile section, profile key)
AUTO_MAP = {
    "salary_annual": ("salary_annual", "income", "salary_annual"),
    "pay_frequency": ("pay_frequency", "income", "pay_frequency"),
    "k401_pct": ("k401_pct", "income", "k401_pct"),
    "employer_match_pct": ("employer_match_pct", "income", "employer_match_pct"),
    "employer_match_annual": ("employer_match_annual", "income", "employer_match_annual"),
    "other_pretax_annual": ("other_pretax_annual", "income", "other_pretax_annual"),
    "ss_62": ("benefit_62", "social_security.self", "62"),
    "ss_67": ("benefit_67", "social_security.self", "67"),
    "ss_70": ("benefit_70", "social_security.self", "70"),
    "mortgage_balance": ("principal_balance", "mortgage", "balance"),
    "mortgage_rate": ("interest_rate", "mortgage", "rate"),
    "mortgage_pi_payment": ("pi_payment", "mortgage", "pi_payment"),
    "mortgage_escrow_payment": ("escrow_payment", "mortgage", "escrow_payment"),
    "mortgage_next_due": ("next_due", "mortgage", "next_due"),
    "mortgage_maturity": ("maturity_date", "mortgage", "maturity"),
}

# escrow analysis -> mortgage.escrow_detail
ESCROW_MAP = {
    "property_tax_annual": "escrow_property_tax_annual",
    "insurance_annual": "escrow_insurance_annual",
    "total_annual": "escrow_total_annual",
    "reserve_monthly": "escrow_reserve_payment",
    "shortage": "escrow_shortage",
    "effective_date": "escrow_effective_date",
}

TYPE_RULES = [
    (re.compile(r"roth", re.I), "roth"),
    # rollover IRAs often carry "401k" in the name but are IRAs (RMDs, no
    # employer contributions) — check rollover before the plain 401k rule
    (re.compile(r"r/?o\s*ira|rollover|\bira\b", re.I), "trad_ira"),
    (re.compile(r"401\s*\(?k\)?|403b|457\b|\btsp\b", re.I), "401k"),
    (re.compile(r"\bhsa\b|health\s*sav", re.I), "hsa"),
    (re.compile(r"annuit", re.I), "annuity"),
    (re.compile(r"savings|checking|money\s*market|\bcash\b|\bcd\b", re.I), "cash"),
    (re.compile(r"brokerage|\btod\b|\bjt\b|individual|taxable|vanguard|fidelity|"
                r"schwab|e\*?trade|merrill|invest", re.I), "brokerage"),
]


def _skeleton() -> dict:
    return common.migrate_profile({
        "version": common.PROFILE_VERSION, "saved_at": common.now_iso(),
        "household": {**common.DEFAULT_HOUSEHOLD, "spouse_birthdate": None},
        "income": {"salary_annual": 0, "pay_frequency": "biweekly", "k401_pct": 0,
                   "employer_match_pct": 0, "other_pretax_annual": 0,
                   "spouse_income_annual": 0},
        "social_security": {"self": {"62": None, "67": None, "70": None},
                            "spouse_own_monthly_fra": 300,
                            "claim_age_self": 67, "claim_age_spouse": 67,
                            "cola": 0.025},
        "mortgage": {}, "home": {"value": 0},
        "assets": [], "liabilities": [], "holdings": [],
        "spending": {"current_monthly": 0, "retirement_monthly_today": 0},
        "assumptions": {"risk": "moderate", "inflation": 0.025, "horizon_age": 95,
                        "return_override": None, "compare_claim_ages": [62, 67, 70]},
        "_auto": {},
    })


def _get(profile: dict, section: str, key: str):
    node = profile
    for part in section.split("."):
        node = node.setdefault(part, {})
    return node.get(key)


def _set(profile: dict, section: str, key: str, value) -> None:
    node = profile
    for part in section.split("."):
        node = node.setdefault(part, {})
    node[key] = value


def _auto_set(profile: dict, auto: dict, changes: list, form_key: str,
              section: str, key: str, new) -> None:
    """Write a document-derived value unless the user has overridden it.

    A field stays auto-managed only while its stored value still matches what
    auto-ingest last wrote; once the user edits it in the intake form, we stop
    touching it and say so.
    """
    cur = _get(profile, section, key)
    if cur is not None and form_key in auto and cur != auto[form_key]:
        changes.append(f"kept your value for {form_key} "
                       f"(document says {new}, you set {cur})")
        del auto[form_key]
        return
    if cur is not None and form_key not in auto and cur != new and cur != 0:
        changes.append(f"kept your value for {form_key} "
                       f"(document says {new}, you set {cur})")
        return
    if cur != new:
        _set(profile, section, key, new)
        changes.append(f"{'updated' if cur not in (None, 0) else 'set'} "
                       f"{form_key} = {new}")
    auto[form_key] = new


def infer_type(label: str) -> str:
    for rx, t in TYPE_RULES:
        if rx.search(label):
            return t
    return "other"


def apply(summary: dict, profile_path: Path | None = None) -> list[str]:
    path = profile_path or common.PROFILE_PATH
    profile = common.migrate_profile(common.load_json(path)) or _skeleton()
    auto = profile.setdefault("_auto", {})
    changes: list[str] = []
    fields = summary.get("fields", {})

    # ---- scalar fields --------------------------------------------------------
    for form_key, (canon, section, key) in AUTO_MAP.items():
        f = fields.get(canon)
        if not f or f.get("value") is None:
            continue
        _auto_set(profile, auto, changes, form_key, section, key, f["value"])

    # The spouse statement is intentionally separated during extraction so it
    # can never replace the primary earner's age-based benefit estimates.
    spouse_ss = summary.get("spouse_social_security") or {}
    spouse_schedule = spouse_ss.get("schedule") or {}
    if spouse_schedule:
        normalized = {str(age): float(value) for age, value in spouse_schedule.items()
                      if value is not None}
        social_security = profile.setdefault("social_security", {})
        social_security["spouse"] = normalized
        social_security["spouse_own_monthly_fra"] = normalized.get("67", 0)
        social_security["spouse_source"] = spouse_ss.get("source")
        auto["spouse_own_monthly_fra"] = normalized.get("67", 0)
        changes.append(
            f"updated spouse Social Security schedule from {spouse_ss.get('source')}"
        )

    # ---- assets from workbook account candidates -------------------------------
    # auto-sourced assets are fully machine-managed: purge and rebuild each run
    # (assets the user typed/edited in the form carry no "source" flag and are
    # never touched)
    assets = profile.setdefault("assets", [])
    n_purged = len([a for a in assets if a.get("source") == "auto"])
    assets[:] = [a for a in assets if a.get("source") != "auto"]
    if n_purged:
        changes.append(f"refreshed {n_purged} auto-managed asset(s)")
    aliases = summary.get("account_aliases") or {}
    seen_now = set()
    for cand in summary.get("account_candidates", []):
        label, value = (cand.get("label") or "").strip(), cand.get("value")
        if not label or not value or value < 100 or label.lower() in seen_now:
            continue
        seen_now.add(label.lower())
        # a masked account number ("*1234") tells us nothing about tax
        # treatment; the alias harvested from the statements does
        alias = aliases.get(label)
        name = f"{alias} ({label})" if alias else label
        existing = next((x for x in assets
                         if (x.get("name") or "").strip().lower() == name.lower()), None)
        if existing is not None:
            continue  # user-entered asset with the same name wins
        atype = infer_type(alias or label)
        if atype == "other" and cand.get("sheet") == "positions":
            atype = "brokerage"  # positions-file account: investable by definition
        assets.append({"name": name, "type": atype, "balance": float(value),
                       "annual_contribution": 0, "source": "auto"})
        changes.append(f"asset '{name}' ({atype}) = {value:,.0f}")

    # ---- holdings ----------------------------------------------------------------
    # dedupe by (account, symbol-or-description), keeping the largest value —
    # protects against the same portfolio arriving via both CSV and XLSX exports
    holdings = summary.get("holding_candidates", [])
    if holdings:
        best: dict[tuple, dict] = {}
        for h in holdings:
            key = ((h.get("account") or "").lower(),
                   (h.get("symbol") or h.get("description") or "").lower())
            if not key[1]:
                continue
            if key not in best or (h.get("value") or 0) > (best[key].get("value") or 0):
                best[key] = h
        by_sym: dict[str, dict] = {}
        for h in best.values():
            sym = (h.get("symbol") or h.get("description") or "?").upper()[:24]
            e = by_sym.setdefault(sym, {"symbol": sym, "value": 0.0,
                                        "cost_basis": None, "quantity": None,
                                        "description": h.get("description")})
            e["value"] = round(e["value"] + float(h.get("value") or 0), 2)
            # Share count, summed across accounts. Needed to reprice a position
            # from a live quote; tracked the same way as cost so a symbol whose
            # accounts disagree about reporting shares is not repriced wrongly.
            if h.get("quantity") is not None:
                e["quantity"] = round((e["quantity"] or 0.0) + float(h["quantity"]), 6)
                e["qty_covers"] = round(
                    e.get("qty_covers", 0.0)
                    + float(h.get("qty_value") or h.get("value") or 0), 2)
            # Sum cost only across the lots that actually reported it. A symbol
            # whose accounts disagree about disclosing cost would otherwise show
            # a partial basis against a full market value — a fake gain.
            if h.get("cost_basis") is not None:
                e["cost_basis"] = round((e["cost_basis"] or 0.0)
                                        + float(h["cost_basis"]), 2)
                # cost_value is the market value of the lots that reported a
                # cost; falling back to the whole row would overstate coverage
                # for a position where only some lots disclose one.
                e["cost_covers"] = round(
                    e.get("cost_covers", 0.0)
                    + float(h.get("cost_value") or h.get("value") or 0), 2)
        profile["holdings"] = sorted(by_sym.values(), key=lambda x: -x["value"])
        total = sum(h["value"] for h in by_sym.values())
        changes.append(f"holdings: {len(by_sym)} positions, total {total:,.0f}")
        # only if no investable account rows plausibly contain these holdings,
        # add a synthetic asset (positions files put their accounts in
        # account_candidates from the same rows, so this rarely fires)
        investable = sum(a.get("balance") or 0 for a in assets
                         if a.get("type") in ("brokerage", "401k", "trad_ira", "roth"))
        covered = investable >= total * 0.9
        if not covered and total > 100:
            assets.append({"name": "Holdings (from workbook)", "type": "brokerage",
                           "balance": round(total, 2),
                           "annual_contribution": 0, "source": "auto"})
            changes.append(f"asset 'Holdings (from workbook)' = {total:,.0f} "
                           "(no matching account row found)")

    # ---- payroll detail (lets the analysis audit the 401(k) arithmetic) ---------
    pay = {k: fields[k]["value"] for k in
           ("k401_current", "k401_ytd", "k401_match_current", "base_salary_current",
            "base_salary_ytd", "gross_ytd", "pay_frequency")
           if fields.get(k) and fields[k].get("value") is not None}
    if pay:
        pay["periods_per_year"] = {"weekly": 52, "biweekly": 26,
                                   "semimonthly": 24, "monthly": 12}.get(
                                       pay.get("pay_frequency"), 24)
        pay["history"] = summary.get("payroll_history") or []
        if profile.get("payroll_detail") != pay:
            profile["payroll_detail"] = pay
            changes.append(
                f"payroll detail: {pay.get('pay_frequency')} × "
                f"{pay['periods_per_year']}/yr, deferral "
                f"{pay.get('k401_current', 0):,.2f}/period, match "
                f"{pay.get('k401_match_current', 0):,.2f}/period")

    # ---- escrow detail (property tax vs insurance) -------------------------------
    esc = {k: fields[src]["value"] for k, src in ESCROW_MAP.items()
           if fields.get(src) and fields[src].get("value") is not None}
    if esc.get("property_tax_annual") or esc.get("insurance_annual"):
        m = profile.setdefault("mortgage", {})
        if m.get("escrow_detail") != esc:
            m["escrow_detail"] = esc
            changes.append(
                f"escrow split: property tax {esc.get('property_tax_annual', 0):,.0f}/yr "
                f"+ insurance {esc.get('insurance_annual', 0):,.0f}/yr "
                f"= {esc.get('total_annual', 0):,.0f}/yr "
                f"({esc.get('total_annual', 0) / 12:,.0f}/mo), modelled separately")

    # ---- spending from the checking ledger -------------------------------------
    sp = summary.get("spending")
    if sp and sp.get("n_full_months"):
        profile["spending_detail"] = {
            "source_months": sp["n_months"], "first": sp["first_month"],
            "last": sp["last_month"], "avg_monthly": sp["avg_monthly"],
            "median_monthly": sp["median_monthly"],
            "avg_monthly_recent12": sp["avg_monthly_recent12"],
            "avg_monthly_mortgage": sp["avg_monthly_mortgage"],
            "avg_monthly_ex_mortgage": sp["avg_monthly_ex_mortgage"],
            "categories": sp["categories"], "monthly_total": sp["monthly_total"],
            "sources": sp.get("sources") or ["checking"],
            "reconciliation": sp.get("reconciliation") or [],
            "one_offs": sp.get("one_offs") or [],
            "one_off_monthly": sp.get("one_off_monthly", 0.0),
            "one_off_total": sp.get("one_off_total", 0.0),
            "typical_monthly": sp.get("typical_monthly", sp["avg_monthly"]),
            "recurring": sp.get("recurring") or {},
        }
        # Plan on the recurring run rate: the large one-offs on file (a furnace
        # replacement, an elective procedure, a one-time vet bill) are real but
        # confirmed non-recurring, and averaging them into a thirty-year
        # baseline would overstate it badly.
        rec = sp.get("recurring") or {}
        recent = rec.get("avg_monthly_recent12", sp["avg_monthly_recent12"])
        _auto_set(profile, auto, changes, "current_monthly",
                  "spending", "current_monthly", round(recent))
        # Retirement spending excludes the entire mortgage payment. The
        # projection re-adds P&I until payoff and models the escrow items
        # (property tax, insurance) as their own streams, so adding escrow
        # here as well would double count it.
        m = profile.get("mortgage") or {}
        escrow = float(m.get("escrow_payment") or 0)
        ex_mortgage = rec.get("avg_monthly_ex_mortgage", sp["avg_monthly_ex_mortgage"])
        _auto_set(profile, auto, changes, "retirement_monthly_today",
                  "spending", "retirement_monthly_today", round(ex_mortgage))
        med = next((float(c.get("monthly") or 0)
                    for c in rec.get("categories", []) if c.get("name") == "medical"), 0.0)
        _auto_set(profile, auto, changes, "observed_medical_monthly",
                  "spending", "observed_medical_monthly", round(med, 2))

        # A P&I figure that already equals the full payment was entered as the
        # total; leaving it would apply the escrow portion to principal and
        # pay the loan off years early.
        doc_pi = (fields.get("pi_payment") or {}).get("value")
        pi = float(m.get("pi_payment") or 0)
        total_doc = (fields.get("total_payment") or {}).get("value")
        if doc_pi and pi and escrow and abs(pi - (doc_pi + escrow)) < abs(pi - doc_pi):
            m["pi_payment"] = doc_pi
            auto["mortgage_pi_payment"] = doc_pi
            changes.append(
                f"corrected pi_payment {pi:,.0f} -> {doc_pi:,.0f}: {pi:,.0f} matches the "
                f"TOTAL payment (P&I {doc_pi:,.0f} + escrow {escrow:,.0f}"
                + (f" = {total_doc:,.0f}" if total_doc else "") +
                "), and escrow must not be amortised as principal")
            pi = doc_pi

        # What is actually paid to the servicer vs the contractual payment tells
        # us how much extra principal is going in each month.
        actual = sp.get("avg_monthly_mortgage_recent12") or sp["avg_monthly_mortgage"]
        if pi and escrow and actual > pi + escrow + 25:
            extra = round(actual - pi - escrow, 2)
            if m.get("extra_monthly") != extra:
                m["extra_monthly"] = extra
                changes.append(f"detected extra principal {extra:,.0f}/mo "
                               f"(pays {actual:,.0f}/mo vs contractual "
                               f"{pi + escrow:,.0f})")

    # ---- unvested equity ----------------------------------------------------------
    vest = summary.get("vesting")
    if vest and vest.get("n_future"):
        eq = profile.setdefault("equity_comp", {})
        # The schedule is always refreshed from the export — it is the source of
        # record for dates and share counts. The three judgement inputs beside
        # it (which symbol, whether conditional vests count, what rate to
        # withhold at) are the owner's to set and are never overwritten here.
        eq["vests"] = vest["future"]
        eq["conditions"] = vest.get("conditions") or []
        eq["has_options"] = vest.get("has_options", False)
        eq["source_sheet"] = vest.get("sheet")
        eq["source_files"] = vest.get("source_files") or []
        eq["as_of"] = vest.get("as_of")
        if vest.get("withholding_rate") is not None:
            eq["withholding_measured"] = round(vest["withholding_rate"], 4)
            eq["withholding_from_n"] = vest.get("withholding_from_n") or 0
        eq.setdefault("symbol", None)
        eq.setdefault("include_conditional", True)
        eq.setdefault("enabled", True)
        changes.append(f"vesting schedule: {vest['n_future']} unvested event(s) "
                       f"{vest['future'][0]['date']}..{vest['future'][-1]['date']}"
                       + (f", withholding measured at "
                          f"{vest['withholding_rate'] * 100:.1f}%"
                          if vest.get("withholding_rate") is not None else ""))
        if not eq.get("symbol"):
            changes.append("vesting: no ticker set — set equity_comp.symbol to "
                           "price the shares, or the plan counts them as shares "
                           "only")

    # ---- investment activity ------------------------------------------------------
    inv = summary.get("investments")
    if inv and inv.get("n_months"):
        # copy everything except the bulky per-month maps
        profile["investment_activity"] = {
            k: v for k, v in inv.items()
            if k not in ("hsa_spending_by_month", "full_months", "loan_by_month")
        }
        liab = profile.setdefault("liabilities", [])
        entry = next((l for l in liab
                      if (l.get("name") or "").lower().startswith("401(k) loan")), None)
        if inv.get("loan_active"):
            if entry is None:
                # the balance is not in the activity file — flag it for the user
                liab.append({"name": "401(k) loan (balance unknown)", "balance": None,
                             "rate": None,
                             "payment_monthly": inv["loan_repayment_monthly"],
                             "source": "auto"})
                changes.append(
                    f"detected an active 401(k) loan: repayments "
                    f"{inv['loan_repayment_monthly']:,.0f}/mo — enter the outstanding "
                    "balance in the intake form")
            elif entry.get("source") == "auto":
                entry["payment_monthly"] = inv["loan_repayment_monthly"]
        else:
            if entry is not None and entry.get("source") == "auto":
                liab.remove(entry)
            if inv.get("loan_payoff_detected"):
                changes.append(
                    f"401(k) loan paid off: last repayment {inv['loan_last_repayment_month']} "
                    f"(final {inv['loan_final_payment']:,.0f} vs usual "
                    f"{inv['loan_typical_monthly_repayment']:,.0f}), nothing since "
                    f"({inv['loan_months_since_last_repayment']} months) — "
                    "removed from liabilities")

    # ---- mortgage liability sync ---------------------------------------------------
    m = profile.get("mortgage") or {}
    if m.get("balance"):
        liab = profile.setdefault("liabilities", [])
        entry = next((l for l in liab
                      if (l.get("name") or "").lower().startswith("mortgage")), None)
        pm = (m.get("pi_payment") or 0) + (m.get("escrow_payment") or 0)
        if entry is None:
            liab.insert(0, {"name": "Mortgage", "balance": m["balance"],
                            "rate": m.get("rate"), "payment_monthly": pm or None,
                            "source": "auto"})
        elif entry.get("source") == "auto":
            entry.update({"balance": m["balance"], "rate": m.get("rate"),
                          "payment_monthly": pm or None})

    profile["saved_at"] = common.now_iso()
    common.save_json(path, profile)
    if not changes:
        changes = ["no changes — profile already up to date"]
    return changes


if __name__ == "__main__":
    import json
    import tempfile
    summary = {"fields": {
        "salary_annual": {"value": 165000, "confidence": 0.7},
        "principal_balance": {"value": 285321.55, "confidence": 0.85},
        "interest_rate": {"value": 0.04125, "confidence": 0.9},
        "benefit_67": {"value": 3080, "confidence": 0.8}},
        "spouse_social_security": {
            "schedule": {"62": 877, "67": 1246, "70": 1545},
            "source": "spouse-social-security-statement.pdf"},
        "account_candidates": [{"label": "Fidelity 401(k)", "value": 585000},
                               {"label": "Chase Savings", "value": 45000},
                               {"label": "*1234", "value": 900000, "sheet": "positions"}],
        "account_aliases": {"*1234": "401k R/O IRA"},
        "holding_candidates": [
            {"account": "Personal", "symbol": "AAPL", "value": 28950},
            {"account": "Personal", "symbol": "VTI", "value": 55000},
            # duplicate export of the same position must dedupe, not sum
            {"account": "Personal", "symbol": "VTI", "value": 55000}]}
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "profile.json"
        apply(summary, p)
        prof = json.loads(p.read_text())
        assert prof["income"]["salary_annual"] == 165000
        assert prof["mortgage"]["balance"] == 285321.55
        assert prof["social_security"]["self"]["67"] == 3080
        assert prof["social_security"]["spouse"]["62"] == 877
        assert prof["social_security"]["spouse_own_monthly_fra"] == 1246
        assert any(a["type"] == "401k" for a in prof["assets"])
        # masked account number resolved via alias -> named and typed correctly
        ira = next(a for a in prof["assets"] if "*1234" in a["name"])
        assert ira["type"] == "trad_ira", ira      # rollover IRA, not a 401k
        assert "401k R/O IRA" in ira["name"], ira
        assert infer_type("Annuity") == "annuity"
        assert infer_type("TOD") == "brokerage"
        assert prof["holdings"][0]["symbol"] == "VTI"  # sorted by value
        assert prof["holdings"][0]["value"] == 55000   # deduped, not 110000
        # big investable accounts exist -> no synthetic holdings asset
        assert not any(a["name"].startswith("Holdings (") for a in prof["assets"])
        # re-running refreshes auto assets without duplicating them
        apply(summary, p)
        prof_r = json.loads(p.read_text())
        assert len([a for a in prof_r["assets"]
                    if a["name"] == "Fidelity 401(k)"]) == 1
        # user override survives the next apply()
        prof["income"]["salary_annual"] = 170000
        p.write_text(json.dumps(prof))
        changes = apply(summary, p)
        prof2 = json.loads(p.read_text())
        assert prof2["income"]["salary_annual"] == 170000
        assert any("kept your value" in c for c in changes)
        # and it stays kept on a third run (no _auto key anymore)
        apply(summary, p)
        prof3 = json.loads(p.read_text())
        assert prof3["income"]["salary_annual"] == 170000
    print("autoprofile self-test OK")
