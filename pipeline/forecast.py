"""12-quarter forward model: analyst consensus where it exists, trend
extrapolation beyond, with base/bull/bear bands and explicit assumptions.

Layering:
  1. Explicit quarterly consensus (Yahoo 0q/+1q)          -> source "consensus_quarter"
  2. Annual consensus (0y/+1y) split by seasonality       -> source "consensus_annual_split"
  3. Beyond consensus: YoY growth glide path + margin path -> source "extrapolated"

An optional data/<TICKER>/assumptions_override.json can adjust the modeled
years after reviewing management guidance, e.g.:
  {"long_run_growth": 0.12, "extra_year_growth": {"2029": 0.14}, "notes": "..."}
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
N_FORECAST = 12


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    # quarter-end style: last day of month
    next_m = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
    from datetime import timedelta
    return next_m - timedelta(days=1)


def _fiscal_labels(last: dict, i: int) -> dict:
    """Labels for the i-th quarter (1-based) after the last historical quarter."""
    fq = last["fiscal_quarter"] + i
    fy = last["fiscal_year"] + (fq - 1) // 4
    fq = (fq - 1) % 4 + 1
    end = _add_months(date.fromisoformat(last["end_date"]), 3 * i)
    cq = (end.month - 1) // 3 + 1
    return {
        "end_date": end.isoformat(),
        "fiscal_year": fy,
        "fiscal_quarter": fq,
        "fiscal_label": f"FY{str(fy)[2:]} Q{fq}",
        "calendar_label": f"CQ{cq} {end.year}",
    }


def _seasonality(quarters: list[dict]) -> dict[int, float]:
    """Average share of fiscal-year revenue contributed by each fiscal quarter."""
    by_fy: dict[int, dict[int, float]] = {}
    for q in quarters:
        if q.get("revenue"):
            by_fy.setdefault(q["fiscal_year"], {})[q["fiscal_quarter"]] = q["revenue"]
    # A restatement can leave a negative revenue quarter in XBRL (e.g. a
    # discontinued-operations reclass). Revenue cannot be negative, and one such
    # quarter wrecks the whole year's shares, so skip years that contain one.
    complete = [fys for fys in by_fy.values()
                if len(fys) == 4 and all(rev > 0 for rev in fys.values())]
    if not complete:
        return {1: 0.25, 2: 0.25, 3: 0.25, 4: 0.25}
    shares = {fq: [] for fq in (1, 2, 3, 4)}
    for fys in complete[-3:]:
        total = sum(fys.values())
        for fq, rev in fys.items():
            shares[fq].append(rev / total)
    return {fq: statistics.mean(v) for fq, v in shares.items()}


def _is_pre_revenue_in_substance(hist: list[dict], threshold: float = 0.05) -> bool:
    """True when reported revenue is trivial next to the operating loss.

    A pre-commercial issuer booking its first milestone or grant payment is
    still economically pre-revenue: the growth engine would compound a
    rounding-error base, and the runway numbers that actually matter would
    disappear. Only applies while the company is loss-making.
    """
    recent = hist[-4:]
    revenue = sum(q.get("revenue") or 0.0 for q in recent)
    op = [q.get("operating_income") for q in recent if q.get("operating_income") is not None]
    if not op:
        return False
    loss = -sum(op)
    return loss > 0 and revenue < threshold * loss


def _consensus_rows(records: list[dict] | None) -> dict[str, dict]:
    if not records:
        return {}
    return {r.get("period"): r for r in records if r.get("period")}


def _run_pre_revenue(ticker: str, data_dir: Path, fin: dict, est: dict,
                     override: dict) -> dict:
    """Forecast a company that has never reported revenue.

    The normal model is a revenue-growth engine: seasonality shares, YoY
    growth, a CAGR glide path and a net-margin drift. None of that has an
    anchor when every reported quarter has revenue of zero, and analyst
    revenue consensus for a pre-commercial issuer is not a real signal
    (most contributors carry zero, so the 'average' is a rounding artefact
    of the few who don't).

    What is real for this kind of company is the spend path and the balance
    sheet, so this projects those instead: operating loss, net loss per
    share (anchored to EPS consensus, which analysts do agree on), quarterly
    cash burn, and the runway that burn implies against reported liquidity.
    Revenue is still emitted so the output schema is unchanged, but it is
    carried at consensus and flagged as immaterial rather than grown.
    """
    hist = [q for q in fin["quarters"] if q.get("net_income") is not None]
    hist.sort(key=lambda q: q["end_date"])
    if not hist:
        raise RuntimeError(f"No income-statement history for {ticker}")
    last = hist[-1]

    rev_est = _consensus_rows(est.get("revenue_estimate"))
    eps_est = _consensus_rows(est.get("earnings_estimate"))

    # --- spend path -----------------------------------------------------------
    # Operating loss is the cleanest read on the burn ramp: it excludes the
    # interest income that a large raise throws off and would otherwise flatter
    # the loss trend.
    op_hist = [q["operating_income"] for q in hist[-8:] if q.get("operating_income")]
    if len(op_hist) >= 5:
        recent = abs(statistics.mean(op_hist[-4:]))
        prior = abs(statistics.mean(op_hist[:-4]))
        opex_growth_y = (recent / prior - 1) if prior else 0.25
    else:
        opex_growth_y = 0.25
    # Trailing opex growth for a company entering construction is genuinely
    # explosive (Oklo ran ~+170% y/y), but carrying that forward for three years
    # compounds into a number no pre-revenue balance sheet could fund. Cap the
    # first forecast year and decay hard toward a normal scaling rate.
    opex_growth_y = override.get("opex_growth_per_year",
                                 max(min(opex_growth_y, 0.60), -0.2))
    opex_decay = override.get("opex_growth_decay", 0.60)
    op_loss_now = abs(statistics.mean([abs(v) for v in op_hist[-2:]])) if op_hist else 0.0

    # --- burn & runway --------------------------------------------------------
    burn_q = []
    for q in hist[-4:]:
        ocf, capex = q.get("operating_cash_flow"), q.get("capex")
        if ocf is not None and capex is not None:
            burn_q.append(abs(ocf) + capex)
    burn_now = statistics.mean(burn_q) if burn_q else None
    liquidity = last.get("total_liquidity") or last.get("cash")

    # --- dilution -------------------------------------------------------------
    # Pre-revenue issuers fund themselves by selling stock, so share count grows;
    # the buyback clamp used for profitable companies would hide that entirely.
    # Only the last five quarters: a longer window on a recently-de-SPAC'd
    # issuer measures the merger share issuance, not the ongoing raise cadence.
    shares_hist = [q["shares_diluted"] for q in hist[-5:] if q.get("shares_diluted")]
    if len(shares_hist) >= 2:
        dilution_q = (shares_hist[-1] / shares_hist[0]) ** (1 / (len(shares_hist) - 1)) - 1
        dilution_q = max(min(dilution_q, 0.06), 0.0)
    else:
        dilution_q = 0.0
    dilution_q = override.get("quarterly_dilution", dilution_q)
    shares_now = shares_hist[-1] if shares_hist else None

    quarters = [_fiscal_labels(last, i) for i in range(1, N_FORECAST + 1)]
    fy0 = last["fiscal_year"] + (1 if last["fiscal_quarter"] == 4 else 0)

    annual: dict[int, dict] = {}
    for period, fy in (("0y", fy0), ("+1y", fy0 + 1)):
        e, r = eps_est.get(period), rev_est.get(period)
        if e and e.get("avg") is not None:
            annual[fy] = {"eps": e["avg"], "eps_low": e.get("low"), "eps_high": e.get("high"),
                          "rev": (r or {}).get("avg"), "analysts": (r or {}).get("numberOfAnalysts")}

    explicit: dict[int, dict] = {}
    for idx, period in ((0, "0q"), (1, "+1q")):
        e, r = eps_est.get(period), rev_est.get(period)
        if e and e.get("avg") is not None:
            explicit[idx] = {"eps": e["avg"], "eps_low": e.get("low"), "eps_high": e.get("high"),
                             "rev": (r or {}).get("avg"), "rev_low": (r or {}).get("low"),
                             "rev_high": (r or {}).get("high")}

    # --- revenue: carried, not modelled ---------------------------------------
    # Consensus for the two named quarters and two named years is passed through;
    # past that the last consensus year is held flat. Deliberately no growth
    # curve: extrapolating a first-revenue ramp would invent the exact number
    # the analysis is least able to support.
    last_cons_rev = None
    for fy in sorted(annual):
        if annual[fy].get("rev"):
            last_cons_rev = annual[fy]["rev"]
    for idx, q in enumerate(quarters):
        fy = q["fiscal_year"]
        if idx in explicit and explicit[idx].get("rev") is not None:
            base = explicit[idx]["rev"]
            lo = explicit[idx].get("rev_low") or 0.0
            hi = explicit[idx].get("rev_high") or base
            q["source"] = "consensus_quarter"
        elif fy in annual and annual[fy].get("rev"):
            base = annual[fy]["rev"] / 4
            lo, hi = 0.0, base * 4
            q["source"] = "consensus_annual_split"
        else:
            base = (last_cons_rev or 0.0) / 4
            lo, hi = 0.0, base * 6 if base else 0.0
            q["source"] = "pre_revenue_placeholder"
        q["revenue"] = {"base": base, "low": lo, "high": hi}

    # --- operating loss path --------------------------------------------------
    g = opex_growth_y
    op_by_fy: dict[int, float] = {}
    for fy in sorted({q["fiscal_year"] for q in quarters}):
        years_out = max(fy - fy0, 0)
        g_fy = override.get("extra_year_opex_growth", {}).get(str(fy))
        if g_fy is None:
            g_fy = opex_growth_y * (opex_decay ** years_out)
        op_by_fy[fy] = g_fy
        g = g_fy

    op_loss = op_loss_now
    cur_fy = last["fiscal_year"]
    for q in quarters:
        rate = op_by_fy[q["fiscal_year"]]
        op_loss = op_loss * (1 + rate) ** 0.25
        q["operating_loss"] = {"base": -op_loss,
                               "low": -op_loss * (1 + 0.25),
                               "high": -op_loss * (1 - 0.20)}

    # --- EPS: consensus first, then loss-per-share off the spend path ----------
    shares = shares_now
    eps_raw = []
    for q in quarters:
        shares = shares * (1 + dilution_q) if shares else None
        # Net loss runs narrower than operating loss by the interest earned on
        # the securities portfolio; hold that offset at its recent level.
        interest = 0.0
        if last.get("operating_income") is not None and last.get("net_income") is not None:
            interest = last["net_income"] - last["operating_income"]
        net_loss = q["operating_loss"]["base"] + interest
        eps_raw.append(net_loss / shares if shares else None)

    # EPS already booked this fiscal year, so a partially-elapsed year can still
    # be tied to its annual consensus using only the quarters that remain.
    actual_eps_by_fy: dict[int, float] = {}
    for q in hist:
        if q.get("eps_diluted") is not None:
            actual_eps_by_fy[q["fiscal_year"]] = (
                actual_eps_by_fy.get(q["fiscal_year"], 0) + q["eps_diluted"])

    carry_scale = 1.0
    for fy in sorted({q["fiscal_year"] for q in quarters}):
        idxs = [i for i, q in enumerate(quarters) if q["fiscal_year"] == fy]
        tgt = annual.get(fy, {}).get("eps")
        raw_sum = sum(eps_raw[i] for i in idxs if eps_raw[i] is not None)
        if tgt is not None and raw_sum:
            remaining = tgt - actual_eps_by_fy.get(fy, 0.0)
            # Only meaningful while the residual still points the same way as
            # the modelled loss; otherwise consensus is already spent.
            if remaining / raw_sum > 0:
                carry_scale = remaining / raw_sum
        scale = carry_scale
        for i in idxs:
            if eps_raw[i] is None:
                quarters[i]["eps"] = None
                continue
            base = eps_raw[i] * scale
            if i in explicit:
                base = explicit[i]["eps"]
                lo = explicit[i].get("eps_low") or base * 1.15
                hi = explicit[i].get("eps_high") or base * 0.85
            else:
                lo, hi = base * 1.25, base * 0.75  # more negative = bear
            quarters[i]["eps"] = {"base": round(base, 2), "low": round(min(lo, hi), 2),
                                  "high": round(max(lo, hi), 2)}

    # --- burn & runway on the projected path ----------------------------------
    # Trailing burn badly understates a company about to start building: Oklo
    # guided FY26 to $80-100M operating cash plus $350-450M of capex against a
    # ~$38M/qtr trailing average. Where management has guided a full-year cash
    # figure, spend that instead of extrapolating history.
    guided = {int(k): v for k, v in (override.get("guided_annual_burn") or {}).items()}
    # A full-year guide covers quarters already reported, so charge the forecast
    # only with what is left of it, spread over that year's remaining quarters.
    spent_by_fy: dict[int, float] = {}
    for q in hist:
        ocf, capex = q.get("operating_cash_flow"), q.get("capex")
        if ocf is not None and capex is not None:
            spent_by_fy[q["fiscal_year"]] = (spent_by_fy.get(q["fiscal_year"], 0)
                                             + abs(ocf) + capex)
    guided_q: dict[int, float] = {}
    for fy, total in guided.items():
        n = sum(1 for q in quarters if q["fiscal_year"] == fy)
        if n:
            guided_q[fy] = max(total - spent_by_fy.get(fy, 0.0), 0.0) / n

    runway_q, burn_path = None, []
    b = burn_now
    for q in quarters:
        fy = q["fiscal_year"]
        if fy in guided_q:
            b = guided_q[fy]
        elif b is not None:
            b = b * (1 + op_by_fy[fy]) ** 0.25
        burn_path.append(b)
        q["burn"] = {"base": -b} if b is not None else None
    if liquidity and any(burn_path):
        remaining, runway_q = liquidity, 0
        for b in burn_path:
            if b is None or remaining < b:
                break
            remaining -= b
            runway_q += 1
        else:
            runway_q = f"{N_FORECAST}+"

    result = {
        "ticker": ticker,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pre_revenue": True,
        "last_reported": {k: last[k] for k in ("fiscal_label", "calendar_label", "end_date")},
        "assumptions": {
            "model": "pre-revenue: spend, dilution and runway rather than revenue growth",
            "seasonality": {"Q1": 0.25, "Q2": 0.25, "Q3": 0.25, "Q4": 0.25},
            "revenue_cagr_3y": None,
            "consensus_growth_next_fy": None,
            "long_run_growth": None,
            "growth_decay_per_year": opex_decay,
            "growth_by_fy": {},
            "opex_growth_per_year": round(opex_growth_y, 4),
            "opex_growth_by_fy": {str(k): round(v, 4) for k, v in op_by_fy.items()},
            "quarterly_buyback_rate": round(-dilution_q, 5),
            "quarterly_dilution_rate": round(dilution_q, 5),
            "net_margin_recent_avg": None,
            "margin_drift_per_year": None,
            "growth_volatility_8q": None,
            "operating_loss_run_rate": round(op_loss_now, 0) if op_loss_now else None,
            "quarterly_burn_recent": round(burn_now, 0) if burn_now else None,
            "guided_annual_burn": guided or None,
            "projected_quarterly_burn": [round(b, 0) if b else None for b in burn_path],
            "liquidity_last_reported": liquidity,
            "runway_quarters_at_projected_burn": runway_q,
            "annual_consensus_anchors": {str(k): v for k, v in annual.items()},
            "revenue_consensus_note": (
                "Analyst revenue consensus is not treated as a forecast input: "
                "most contributors carry zero, so the average is not a real estimate."),
            "override_applied": override or None,
        },
        "quarters": quarters,
    }
    out = data_dir / "forecast.json"
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"  wrote {out.relative_to(ROOT)} (pre-revenue model)")
    return result


def run(ticker: str) -> dict:
    ticker = ticker.upper()
    data_dir = ROOT / "data" / ticker
    fin = json.loads((data_dir / "financials.json").read_text(encoding="utf-8"))
    try:
        est = json.loads((data_dir / "estimates.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        est = {}

    override_path = data_dir / "assumptions_override.json"
    override = json.loads(override_path.read_text(encoding="utf-8")) if override_path.exists() else {}

    hist = [q for q in fin["quarters"] if q.get("revenue")]
    hist.sort(key=lambda q: q["end_date"])
    if not hist or _is_pre_revenue_in_substance(hist):
        return _run_pre_revenue(ticker, data_dir, fin, est, override)
    last = hist[-1]
    hist_by_key = {(q["fiscal_year"], q["fiscal_quarter"]): q for q in hist}

    season = _seasonality(hist)
    rev_est = _consensus_rows(est.get("revenue_estimate"))
    eps_est = _consensus_rows(est.get("earnings_estimate"))

    # --- shares & buyback glide -------------------------------------------------
    shares_hist = [q["shares_diluted"] for q in hist[-9:] if q.get("shares_diluted")]
    if len(shares_hist) >= 2:
        buyback_q = (shares_hist[-1] / shares_hist[0]) ** (1 / (len(shares_hist) - 1)) - 1
        buyback_q = max(min(buyback_q, 0.005), -0.01)  # clamp to sane quarterly range
    else:
        buyback_q = 0.0
    shares_now = shares_hist[-1] if shares_hist else None

    # --- growth statistics -------------------------------------------------------
    yoy = []
    for q in hist:
        prev = hist_by_key.get((q["fiscal_year"] - 1, q["fiscal_quarter"]))
        if prev and prev.get("revenue"):
            yoy.append(q["revenue"] / prev["revenue"] - 1)
    growth_vol = statistics.stdev(yoy[-8:]) if len(yoy) >= 3 else 0.05

    fy_rev: dict[int, float] = {}
    for q in hist:
        fy_rev[q["fiscal_year"]] = fy_rev.get(q["fiscal_year"], 0) + q["revenue"]
    complete_fys = sorted(fy for fy in fy_rev
                          if sum(1 for q in hist if q["fiscal_year"] == fy) == 4)
    if len(complete_fys) >= 3:
        cagr3 = (fy_rev[complete_fys[-1]] / fy_rev[complete_fys[-3]]) ** 0.5 - 1
    else:
        cagr3 = statistics.mean(yoy[-4:]) if yoy else 0.05

    # --- consensus annual anchors ------------------------------------------------
    # 0y = current (next unreported) fiscal year, +1y = the one after.
    fy0 = last["fiscal_year"] + (1 if last["fiscal_quarter"] == 4 else 0)
    annual: dict[int, dict] = {}
    for period, fy in (("0y", fy0), ("+1y", fy0 + 1)):
        r, e = rev_est.get(period), eps_est.get(period)
        if r and r.get("avg"):
            annual[fy] = {
                "rev": r["avg"], "rev_low": r.get("low"), "rev_high": r.get("high"),
                "eps": (e or {}).get("avg"), "eps_low": (e or {}).get("low"),
                "eps_high": (e or {}).get("high"),
                "analysts": r.get("numberOfAnalysts"),
            }

    g_consensus = None
    if fy0 in annual and fy0 + 1 in annual:
        g_consensus = annual[fy0 + 1]["rev"] / annual[fy0]["rev"] - 1
    elif fy0 in annual and fy0 - 1 in fy_rev:
        g_consensus = annual[fy0]["rev"] / fy_rev[fy0 - 1] - 1

    long_run = override.get("long_run_growth")
    if long_run is None:
        candidates = [g for g in (cagr3, g_consensus) if g is not None]
        long_run = statistics.mean(candidates) if candidates else 0.05
    decay = override.get("growth_decay", 0.75)  # fraction of gap to long-run closed per year

    # --- margin path -------------------------------------------------------------
    def net_margin(q):
        return q["net_income"] / q["revenue"] if q.get("net_income") and q.get("revenue") else None

    recent_margins = [m for m in (net_margin(q) for q in hist[-8:]) if m is not None]
    margin_now = statistics.mean(recent_margins[-4:]) if recent_margins else 0.2
    if len(recent_margins) >= 8:
        margin_drift_y = (statistics.mean(recent_margins[4:]) - statistics.mean(recent_margins[:4]))
    else:
        margin_drift_y = 0.0
    margin_drift_y = override.get("margin_drift_per_year", max(min(margin_drift_y, 0.02), -0.02))

    # --- build 12 quarters -------------------------------------------------------
    quarters: list[dict] = []
    for i in range(1, N_FORECAST + 1):
        lab = _fiscal_labels(last, i)
        quarters.append(lab)

    # explicit quarterly consensus for the first quarters
    explicit: dict[int, dict] = {}
    for idx, period in ((0, "0q"), (1, "+1q")):
        r, e = rev_est.get(period), eps_est.get(period)
        if r and r.get("avg"):
            explicit[idx] = {
                "rev": r["avg"], "rev_low": r.get("low"), "rev_high": r.get("high"),
                "eps": (e or {}).get("avg"), "eps_low": (e or {}).get("low"),
                "eps_high": (e or {}).get("high"),
            }
            # sanity: Yahoo's year-ago revenue should match our same-quarter history
            ya = r.get("yearAgoRevenue")
            prev = hist_by_key.get((quarters[idx]["fiscal_year"] - 1,
                                    quarters[idx]["fiscal_quarter"]))
            if ya and prev and abs(ya - prev["revenue"]) / prev["revenue"] > 0.05:
                explicit[idx]["alignment_warning"] = (
                    f"Yahoo yearAgoRevenue {ya:,.0f} vs history {prev['revenue']:,.0f}")

    # per-fiscal-year revenue targets for the forecast horizon
    fy_targets: dict[int, dict] = {}
    horizon_fys = sorted({q["fiscal_year"] for q in quarters})
    prev_rev = {fy: fy_rev.get(fy) for fy in range(fy0 - 2, fy0 + 5)}
    g = g_consensus if g_consensus is not None else long_run
    growth_by_fy = {}
    for fy in horizon_fys:
        if fy in annual:
            fy_targets[fy] = {"rev": annual[fy]["rev"], "source": "consensus"}
            # Prefer an already-computed horizon target over prev_rev: prev_rev
            # for the current in-progress fiscal year holds only actuals
            # reported so far (a partial-year sum), not the full-year figure.
            base_prev = fy_targets.get(fy - 1, {}).get("rev") or prev_rev.get(fy - 1)
            if base_prev:
                growth_by_fy[fy] = annual[fy]["rev"] / base_prev - 1
        else:
            g = g * decay + long_run * (1 - decay)
            g_fy = override.get("extra_year_growth", {}).get(str(fy), g)
            base_prev = fy_targets.get(fy - 1, {}).get("rev") or prev_rev.get(fy - 1)
            fy_targets[fy] = {"rev": base_prev * (1 + g_fy), "source": "extrapolated"}
            growth_by_fy[fy] = g_fy
            g = g_fy

    # quarterly revenue from targets
    for idx, q in enumerate(quarters):
        fy, fq = q["fiscal_year"], q["fiscal_quarter"]
        tgt = fy_targets[fy]
        if idx in explicit:
            q["revenue"] = {"base": explicit[idx]["rev"], "low": explicit[idx]["rev_low"],
                            "high": explicit[idx]["rev_high"]}
            q["source"] = "consensus_quarter"
            if "alignment_warning" in explicit[idx]:
                q["alignment_warning"] = explicit[idx]["alignment_warning"]
        else:
            base = tgt["rev"] * season[fq]
            if tgt["source"] == "consensus":
                rel_lo = (annual[fy]["rev_low"] / annual[fy]["rev"]) if annual[fy].get("rev_low") else 0.97
                rel_hi = (annual[fy]["rev_high"] / annual[fy]["rev"]) if annual[fy].get("rev_high") else 1.03
                q["source"] = "consensus_annual_split"
            else:
                years_out = fy - fy0
                # Cap the band. A small company with a restated or erratic
                # history can carry 8-quarter growth vol above 300%, which makes
                # 1 - spread negative (revenue cannot be) and 1 + spread many
                # multiples of base — the chart axis then scales to the band and
                # flattens the actual series into an invisible line.
                spread = min(growth_vol * (0.8 + 0.5 * years_out), 0.75)
                rel_lo, rel_hi = max(1 - spread, 0.05), 1 + spread
                q["source"] = "extrapolated"
            q["revenue"] = {"base": base, "low": base * rel_lo, "high": base * rel_hi}

    # EPS: margin * revenue / shares, scaled to consensus annual EPS when known
    margin_by_fq = {}
    for fq in (1, 2, 3, 4):
        vals = [net_margin(q) for q in hist if q["fiscal_quarter"] == fq]
        vals = [v for v in vals if v is not None][-2:]
        margin_by_fq[fq] = statistics.mean(vals) if vals else margin_now

    shares = shares_now
    eps_raw = []
    for idx, q in enumerate(quarters):
        shares = shares * (1 + buyback_q) if shares else None
        years_out = max(q["fiscal_year"] - fy0, 0)
        m = margin_by_fq[q["fiscal_quarter"]] + margin_drift_y * (idx + 1) / 4
        eps = (q["revenue"]["base"] * m / shares) if shares else None
        eps_raw.append({"eps": eps, "margin": m, "shares": shares})

    # scale so each consensus FY's summed EPS matches the annual consensus EPS;
    # extrapolated years inherit the last consensus year's scale so the margin
    # assumption stays continuous instead of snapping back to raw history
    carry_scale = 1.0
    for fy in horizon_fys:
        idxs = [i for i, q in enumerate(quarters) if q["fiscal_year"] == fy]
        tgt_eps = annual.get(fy, {}).get("eps")
        raw_sum = sum(eps_raw[i]["eps"] for i in idxs if eps_raw[i]["eps"])
        # A company with a loss quarter in recent history has per-quarter margins
        # of mixed sign, so the year's raw EPS can sum to near zero and this
        # ratio diverges — SanDisk produced +$877, -$1,107 then +$1,324 across
        # consecutive quarters. Fall back to splitting the annual consensus by
        # each quarter's share of modelled revenue whenever the scale is not sane.
        scale = None
        if tgt_eps and raw_sum and len(idxs) == 4:
            candidate = tgt_eps / raw_sum
            if 0 < candidate <= 10:
                scale = carry_scale = candidate
        # Only rebuild quarters that already had a modelled EPS: a filer with no
        # usable share count (Berkshire tags per-share data only by share class)
        # should keep its empty EPS series rather than have one invented here.
        if (scale is None and tgt_eps and len(idxs) == 4
                and all(eps_raw[i]["eps"] is not None for i in idxs)):
            rev_total = sum(quarters[i]["revenue"]["base"] for i in idxs)
            if rev_total:
                for i in idxs:
                    share = quarters[i]["revenue"]["base"] / rev_total
                    eps_raw[i]["eps"] = tgt_eps * share
                scale = carry_scale = 1.0
        if scale is None:
            scale = carry_scale
        for i in idxs:
            if eps_raw[i]["eps"] is None:
                quarters[i]["eps"] = None
                continue
            base = eps_raw[i]["eps"] * scale
            if i in explicit and explicit[i].get("eps"):
                base = explicit[i]["eps"]
                lo = explicit[i].get("eps_low") or base * 0.95
                hi = explicit[i].get("eps_high") or base * 1.05
            else:
                rl = quarters[i]["revenue"]["low"] / quarters[i]["revenue"]["base"]
                rh = quarters[i]["revenue"]["high"] / quarters[i]["revenue"]["base"]
                lo, hi = base * (rl - 0.01), base * (rh + 0.01)
            quarters[i]["eps"] = {"base": round(base, 2), "low": round(lo, 2), "high": round(hi, 2)}

    result = {
        "ticker": ticker,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "last_reported": {k: last[k] for k in ("fiscal_label", "calendar_label", "end_date")},
        "assumptions": {
            "seasonality": {f"Q{k}": round(v, 4) for k, v in season.items()},
            "revenue_cagr_3y": round(cagr3, 4),
            "consensus_growth_next_fy": round(g_consensus, 4) if g_consensus is not None else None,
            "long_run_growth": round(long_run, 4),
            "growth_decay_per_year": decay,
            "growth_by_fy": {str(k): round(v, 4) for k, v in growth_by_fy.items()},
            "quarterly_buyback_rate": round(buyback_q, 5),
            "net_margin_recent_avg": round(margin_now, 4),
            "margin_drift_per_year": round(margin_drift_y, 4),
            "growth_volatility_8q": round(growth_vol, 4),
            "annual_consensus_anchors": {str(k): v for k, v in annual.items()},
            "override_applied": override or None,
        },
        "quarters": quarters,
    }
    out = data_dir / "forecast.json"
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"  wrote {out.relative_to(ROOT)}")
    return result


def main() -> int:
    import sys
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "MSFT").upper()
    data_dir = ROOT / "data" / ticker

    # An ETF or fund has no revenue or EPS to project — it is a basket, priced
    # by what it holds. Saying so is the answer; a traceback out of
    # financials.json only looks like the pipeline is broken.
    if (data_dir / "fund.json").exists():
        print(f"{ticker} is a fund — there is nothing to forecast. A fund has "
              f"no revenue or earnings of its own; its dashboard is built from "
              f"holdings, cost and exposure.")
        print(f"  refresh it with: python pipeline/fetch.py {ticker}")
        return 0

    if not (data_dir / "financials.json").exists():
        print(f"{ticker}: no data/{ticker}/financials.json — nothing to "
              f"forecast from.")
        print(f"  fetch the fundamentals first: python pipeline/fetch.py {ticker}")
        return 1

    r = run(ticker)
    for q in r["quarters"]:
        rev = q["revenue"]["base"] / 1e9
        eps = q["eps"]["base"] if q.get("eps") else None
        print(f"{q['fiscal_label']:9} {q['calendar_label']:9} rev ${rev:6.1f}B  eps {eps}  [{q['source']}]")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
