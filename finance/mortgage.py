"""Mortgage amortization and payoff scenarios."""

from __future__ import annotations


def _next_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m += 1
    if m > 12:
        y, m = y + 1, 1
    return f"{y:04d}-{m:02d}"


def amortize(balance: float, annual_rate: float, pi_payment: float,
             start: str, extra_monthly: float = 0.0,
             lump: tuple[str, float] | None = None,
             max_months: int = 720) -> list[dict]:
    """Monthly schedule rows {month, interest, principal, balance}.

    `start` is "YYYY-MM". `lump=(month, amount)` applies an extra principal
    payment that month; amount=float('inf') means pay the loan off entirely.
    """
    r = annual_rate / 12
    rows, ym = [], start
    for _ in range(max_months):
        if balance <= 0.005:
            break
        interest = balance * r
        principal = min(pi_payment - interest + extra_monthly, balance)
        if principal <= 0:
            raise ValueError("payment does not cover interest")
        balance -= principal
        if lump and ym == lump[0] and balance > 0:
            lump_amt = min(lump[1], balance)
            principal += lump_amt
            balance -= lump_amt
        rows.append({"month": ym, "interest": round(interest, 2),
                     "principal": round(principal, 2), "balance": round(balance, 2)})
        ym = _next_month(ym)
    return rows


def summarize(rows: list[dict]) -> dict:
    return {"payoff_month": rows[-1]["month"] if rows else None,
            "months_left": len(rows),
            "total_interest_remaining": round(sum(r["interest"] for r in rows), 2)}


def annual_balances(rows: list[dict]) -> list[list]:
    """[[year, end-of-year balance], ...] for charting (plus final month)."""
    out, seen = [], {}
    for r in rows:
        seen[int(r["month"][:4])] = r["balance"]
    for y in sorted(seen):
        out.append([y, seen[y]])
    return out


def run(m: dict, retirement_ym: str) -> dict:
    """Scenario set for the dashboard. `m` = profile['mortgage'].

    `extra_monthly` (detected from what is actually paid to the servicer) is
    part of the baseline, so "current payment" reflects reality rather than
    the contractual minimum.
    """
    base_args = (m["balance"], m["rate"], m["pi_payment"], m.get("next_due") or retirement_ym)
    base_extra = float(m.get("extra_monthly") or 0)
    scenarios = []
    base_rows = amortize(*base_args, extra_monthly=base_extra)
    scenarios.append({"name": ("Current payment (incl. "
                               f"${base_extra:,.0f}/mo extra principal)"
                               if base_extra else "Current payment"),
                      **summarize(base_rows),
                      "annual_balances": annual_balances(base_rows)})
    if base_extra:
        rows = amortize(*base_args)
        s = summarize(rows)
        s["interest_saved"] = round(scenarios[0]["total_interest_remaining"]
                                    - s["total_interest_remaining"], 2)
        scenarios.append({"name": "Contractual payment only (no extra)", **s,
                          "annual_balances": annual_balances(rows)})
    for extra in (base_extra + 200, base_extra + 500):
        rows = amortize(*base_args, extra_monthly=extra)
        s = summarize(rows)
        s["interest_saved"] = round(scenarios[0]["total_interest_remaining"]
                                    - s["total_interest_remaining"], 2)
        scenarios.append({"name": f"${extra:,.0f}/mo extra principal", **s,
                          "annual_balances": annual_balances(rows)})
    # lump-sum payoff at retirement
    rows = amortize(*base_args, extra_monthly=base_extra,
                    lump=(retirement_ym, float("inf")))
    bal_at_ret = next((r0["balance"] + r0["principal"] for r0 in rows
                       if r0["month"] == retirement_ym), None)
    s = summarize(rows)
    s["interest_saved"] = round(scenarios[0]["total_interest_remaining"]
                                - s["total_interest_remaining"], 2)
    scenarios.append({"name": "Lump-sum payoff at retirement", **s,
                      "cash_required": round(bal_at_ret, 2) if bal_at_ret else None,
                      "annual_balances": annual_balances(rows)})
    return {"scenarios": scenarios,
            "monthly_pi": m["pi_payment"], "monthly_escrow": m.get("escrow_payment"),
            "rate": m["rate"]}


if __name__ == "__main__":
    # Textbook case: $100k, 6%, 30y -> payment 599.55, total interest ~115,838
    rows = amortize(100_000, 0.06, 599.55, "2026-01", max_months=400)
    s = summarize(rows)
    assert 358 <= s["months_left"] <= 361, s
    assert abs(s["total_interest_remaining"] - 115_838) < 300, s
    # extra principal shortens the loan
    rows2 = amortize(100_000, 0.06, 599.55, "2026-01", extra_monthly=200, max_months=400)
    assert len(rows2) < len(rows)
    assert summarize(rows2)["total_interest_remaining"] < s["total_interest_remaining"]
    # lump payoff ends the schedule that month
    rows3 = amortize(100_000, 0.06, 599.55, "2026-01", lump=("2030-01", float("inf")))
    assert rows3[-1]["month"] == "2030-01" and rows3[-1]["balance"] == 0
    print("mortgage self-test OK")
