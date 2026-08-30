"""Monte Carlo wrapper around the deterministic projection."""

from __future__ import annotations

import numpy as np

import projection


ASSET_VOL = {"stocks": 0.18, "bonds": 0.06, "cash": 0.01, "other": 0.12}
CORR = {
    ("stocks", "bonds"): 0.10, ("stocks", "cash"): 0.0,
    ("stocks", "other"): 0.60, ("bonds", "cash"): 0.20,
    ("bonds", "other"): 0.30, ("cash", "other"): 0.0,
}


def risk_from_portfolio(portfolio: dict, fallback_stdev: float = 0.11) -> dict:
    """Allocation-derived volatility with a single-stock concentration penalty."""
    total = float(portfolio.get("total_portfolio") or 0)
    if not total:
        return {"stdev": fallback_stdev, "allocation": {}, "concentration": 0.0,
                "source": "risk preset fallback"}
    fa = portfolio.get("fund_analysis") or {}
    alloc = fa.get("allocation") or {}
    dollars = {
        "stocks": float(portfolio.get("stocks_total") or 0)
                  + float(alloc.get("stocks") or 0)
                  + float(alloc.get("unclassified") or 0),
        "bonds": float(alloc.get("bonds") or 0),
        "cash": float(portfolio.get("cash_total") or 0) + float(alloc.get("cash") or 0),
        "other": (float(portfolio.get("other_total") or 0)
                  + sum(float(alloc.get(k) or 0) for k in
                        ("preferred", "convertible", "other"))),
    }
    # Any unaccounted value is conservatively treated as equity.
    dollars["stocks"] += max(total - sum(dollars.values()), 0)
    weights = {k: v / total for k, v in dollars.items()}
    variance = sum((weights[k] * ASSET_VOL[k]) ** 2 for k in weights)
    keys = list(weights)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            corr = CORR.get((a, b), CORR.get((b, a), 0.0))
            variance += 2 * weights[a] * weights[b] * ASSET_VOL[a] * ASSET_VOL[b] * corr
    look = fa.get("look_through") or []
    top_row = max(look, key=lambda x: float(x.get("pct_portfolio") or 0), default={})
    top = float(top_row.get("pct_portfolio") or 0)
    concentration_vol = top * 0.25
    base_stdev = float(np.sqrt(variance))
    stdev = float(np.sqrt(variance + concentration_vol ** 2))
    return {"stdev": max(stdev, fallback_stdev),
            "allocation": {k: round(v, 4) for k, v in weights.items()},
            "base_stdev": base_stdev, "top_symbol": top_row.get("symbol"),
            "top_weight": round(top, 4), "concentration": round(top, 4),
            "concentration_vol": concentration_vol,
            "concentration_penalty": concentration_vol,
            "source": "portfolio allocation + concentration"}


def run(p: dict, n: int = 10000, seed: int = 42, risk_model: dict | None = None) -> dict:
    """p from projection.prepare(). Returns success prob + percentile bands."""
    years = p["end_year"] - p["start_year"] + 1
    rng = np.random.default_rng(seed)
    risk_model = risk_model or {"stdev": p["stdev"], "concentration": 0.0,
                                "source": "risk preset"}
    sigma = float(risk_model.get("stdev") or p["stdev"])
    # Student-t(5) has finite variance 5/3; normalize it back to requested sigma.
    shocks = rng.standard_t(5, (n, years)) / np.sqrt(5 / 3)
    draws = np.clip(p["mean_return"] + sigma * shocks, -0.80, 1.00)
    infl_draws = np.clip(rng.normal(p["inflation"], 0.015, (n, years)), -0.01, 0.10)
    totals = np.empty((n, years))
    successes = 0
    end_balances = []
    min_liquid = []
    near_failures = 0
    depletion_ages: dict[int, int] = {}
    for i in range(n):
        res = projection.project(p, draws[i], inflation_rates=infl_draws[i])
        totals[i] = [row["total"] for row in res["rows"]]
        if res["depleted_at"] is None:
            successes += 1
        end_balances.append(res["end_balance"])
        min_liquid.append(res["min_liquid"])
        last_spend = next((row["spending"] for row in reversed(res["rows"])
                           if row["spending"]), 0)
        if res["end_balance"] < last_spend * 2:
            near_failures += 1
        if res["depleted_at"] is not None:
            depletion_ages[res["depleted_at"]] = depletion_ages.get(res["depleted_at"], 0) + 1
    ages = [p["start_year"] - p["birth_year"] + j for j in range(years)]
    pct = {f"p{q}": np.percentile(totals, q, axis=0).round(0).tolist()
           for q in (10, 25, 50, 75, 90)}
    return {"n": n, "seed": seed,
            "success_prob": round(successes / n, 3),
            "median_end_balance": round(float(np.median(end_balances))),
            "p10_end_balance": round(float(np.percentile(end_balances, 10))),
            "near_failure_prob": round(near_failures / n, 3),
            "median_min_liquid": round(float(np.median(min_liquid))),
            "p10_min_liquid": round(float(np.percentile(min_liquid, 10))),
            "depletion_ages": {str(k): v for k, v in sorted(depletion_ages.items())},
            "risk_model": risk_model,
            "return_distribution": "Student-t (5 df), clipped -80%/+100%",
            "inflation_stdev": 0.015,
            "bands": {"ages": ages, **pct}}


if __name__ == "__main__":
    prof = {
        "household": {"self_birthdate": "1978-04-12", "state": "CA",
                      "retirement_date": "2040-06"},
        "income": {"salary_annual": 150000, "k401_pct": 10, "employer_match_pct": 4},
        "assets": [{"type": "401k", "balance": 800000},
                   {"type": "brokerage", "balance": 200000}],
        "social_security": {"self": {"67": 3000}, "claim_age_self": 67},
        "spending": {"retirement_monthly_today": 500},
        "assumptions": {"risk": "moderate", "horizon_age": 95},
    }
    p = projection.prepare(prof)
    out = run(p, n=100)
    assert out["success_prob"] > 0.97, out["success_prob"]  # trivial spending
    hungry = projection.prepare({**prof, "spending": {"retirement_monthly_today": 45000}})
    out2 = run(hungry, n=100)
    assert out2["success_prob"] < 0.5, out2["success_prob"]
    assert len(out["bands"]["p50"]) == p["end_year"] - p["start_year"] + 1
    risk = risk_from_portfolio({"total_portfolio": 1_000_000,
        "stocks_total": 600_000, "cash_total": 100_000,
        "fund_analysis": {"allocation": {"bonds": 200_000, "unclassified": 100_000},
                          "look_through": [{"symbol": "TEST", "pct_portfolio": .24}]}})
    assert risk["allocation"]["stocks"] == .7
    assert risk["top_symbol"] == "TEST" and risk["concentration_penalty"] == .06
    assert risk["stdev"] > risk["base_stdev"]
    seeded1 = run(p, n=20, seed=123)
    seeded2 = run(p, n=20, seed=123)
    assert seeded1["bands"] == seeded2["bands"]
    assert "Student-t" in seeded1["return_distribution"]
    print("montecarlo self-test OK")
