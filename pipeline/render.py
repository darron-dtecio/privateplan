"""Render dashboards/<TICKER>.html from the JSON produced by fetch.py/forecast.py
plus the Claude-written narrative.json.

Charts are hand-built inline SVG (no external JS libs): thin marks, hairline
grid, surface gaps, crosshair tooltips wired up by the template's script.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent

S1, S2, S3, S4 = "var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"


# ---------------------------------------------------------------- formatting --
def money(v: float | None, dec: int = 1) -> str:
    if v is None:
        return "—"
    a, sign = abs(v), "-" if v < 0 else ""
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"{sign}${a / div:,.{dec}f}{unit}"
    return f"{sign}${a:,.0f}"


def pct(v: float | None, dec: int = 1, signed: bool = False) -> str:
    if v is None:
        return "—"
    s = "+" if (signed and v >= 0) else ""
    return f"{s}{v * 100:.{dec}f}%"


def eps_fmt(v: float | None) -> str:
    if v is None:
        return "—"
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def fit_fmt(ticks: list[float], fmt_prec, default):
    """Pick the fewest decimals that still label every tick distinctly.

    money(1.5e9, 0) and money(2e9, 0) are both "$2B", so a 0-decimal axis can
    print "$1B $2B $2B $3B" and misstate its own scale. Callers pass the
    precision-taking formatter (e.g. money) and get back a 1-arg formatter.
    """
    if fmt_prec is None:
        return default
    for dec in (0, 1, 2):
        labels = [fmt_prec(t, dec) for t in ticks]
        if len(set(labels)) == len(labels):
            return lambda v, _d=dec: fmt_prec(v, _d)
    return lambda v: fmt_prec(v, 2)


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    start = math.floor(lo / step) * step
    # The axis must span the data, so keep stepping until the running tick
    # reaches hi and then emit that final tick. Stopping at `t <= hi` instead
    # leaves the top tick below the maximum whenever hi is not close to a
    # multiple of step, and callers use ticks[-1] as y_hi — anything above it
    # is scaled outside the plot area and bleeds off the top of the chart.
    ticks = []
    t = start
    eps = step * 1e-9
    while t < hi - eps:
        ticks.append(round(t, 10) + 0.0)  # + 0.0 normalises -0.0, which formats as "$-0"
        t += step
    ticks.append(round(t, 10) + 0.0)
    return ticks


# ------------------------------------------------------------- svg utilities --
def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class Svg:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.parts: list[str] = []

    def add(self, s: str):
        self.parts.append(s)

    def text(self, x, y, s, size=12, fill="var(--muted)", anchor="middle", weight=None, tabular=False):
        style = f"font-variant-numeric:tabular-nums;" if tabular else ""
        w = f' font-weight="{weight}"' if weight else ""
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
                 f'text-anchor="{anchor}"{w} style="{style}">{_esc(s)}</text>')

    def line(self, x1, y1, x2, y2, stroke, width=1, cls="", dash=None, cap="butt"):
        c = f' class="{cls}"' if cls else ""
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                 f'stroke="{stroke}" stroke-width="{width}" stroke-linecap="{cap}"{d}{c}/>')

    def render(self) -> str:
        return (f'<svg viewBox="0 0 {self.w} {self.h}" xmlns="http://www.w3.org/2000/svg" '
                f'aria-label="chart">{"".join(self.parts)}</svg>')


def rounded_bar(x, y_top, w, y_base, fill, idx, r=4.0) -> str:
    """Column from the zero baseline to the data value; rounds the corner
    farthest from baseline (top for positive values, bottom for negative)."""
    if y_top <= y_base:
        h = y_base - y_top
        r = min(r, w / 2, max(h, 0.01))
        return (f'<path class="bar" data-i="{idx}" fill="{fill}" d="M{x:.1f},{y_base:.1f} '
                f'L{x:.1f},{y_top + r:.1f} Q{x:.1f},{y_top:.1f} {x + r:.1f},{y_top:.1f} '
                f'L{x + w - r:.1f},{y_top:.1f} Q{x + w:.1f},{y_top:.1f} {x + w:.1f},{y_top + r:.1f} '
                f'L{x + w:.1f},{y_base:.1f} Z"/>')
    h = y_top - y_base
    r = min(r, w / 2, max(h, 0.01))
    return (f'<path class="bar" data-i="{idx}" fill="{fill}" d="M{x:.1f},{y_base:.1f} '
            f'L{x:.1f},{y_top - r:.1f} Q{x:.1f},{y_top:.1f} {x + r:.1f},{y_top:.1f} '
            f'L{x + w - r:.1f},{y_top:.1f} Q{x + w:.1f},{y_top:.1f} {x + w:.1f},{y_top - r:.1f} '
            f'L{x + w:.1f},{y_base:.1f} Z"/>')


class Frame:
    """Plot frame: scales, grid, axes, crosshair, tooltip-data collection."""

    def __init__(self, vbw=960, vbh=300, left=64, right=20, top=22, bottom=34,
                 y_lo=0.0, y_hi=1.0, n_slots=1, y_fmt=lambda v: f"{v:g}"):
        self.svg = Svg(vbw, vbh)
        self.left, self.right, self.top, self.bottom = left, right, top, bottom
        self.pw = vbw - left - right
        self.ph = vbh - top - bottom
        self.y_lo, self.y_hi = y_lo, y_hi
        self.n = n_slots
        self.step = self.pw / n_slots
        self.x0 = left + self.step / 2
        self.y_fmt = y_fmt
        self.slots: list[dict] = []

    def x(self, i: int) -> float:
        return self.x0 + i * self.step

    def y(self, v: float) -> float:
        return self.top + self.ph * (1 - (v - self.y_lo) / (self.y_hi - self.y_lo))

    def grid(self, ticks: list[float]):
        for t in ticks:
            if t < self.y_lo or t > self.y_hi:
                continue
            yy = self.y(t)
            is_base = abs(t) < 1e-12
            self.svg.line(self.left, yy, self.left + self.pw, yy,
                          "var(--baseline)" if is_base else "var(--grid)", 1)
            self.svg.text(self.left - 8, yy + 4, self.y_fmt(t), 11.5, anchor="end", tabular=True)

    def x_labels(self, labels: list[str], every: int | None = None):
        every = every or max(1, math.ceil(len(labels) / 8))
        for i, lab in enumerate(labels):
            if i % every == 0:
                self.svg.text(self.x(i), self.top + self.ph + 18, lab, 11.5)

    def crosshair(self):
        self.svg.line(0, self.top, 0, self.top + self.ph, "var(--muted)", 1, cls="crosshair")

    def data_json(self) -> str:
        return json.dumps({"vbw": self.svg.w, "x0": round(self.x0, 2),
                           "step": round(self.step, 4), "slots": self.slots})


# ------------------------------------------------------------- chart builders --
def quarterly_chart(hist, fore, y_fmt, series_name, chart_id, title, subtitle, table_fmt,
                    y_fmt_prec=None):
    """History as columns; forecast as base line with bear-bull band.

    Returns None when there is nothing to plot — some filers report no usable
    per-share history, and a chart with zero slots cannot be scaled.
    """
    if not hist or not fore:
        return None
    n = len(hist) + len(fore)
    vals = [h["value"] for h in hist] + [f["high"] for f in fore] + [f["low"] for f in fore]
    lo, hi = min(vals + [0]), max(vals + [0])
    pad = (hi - lo) * 0.06 or abs(hi) * 0.05 or 1
    lo_bound = (lo - pad) if lo < 0 else 0
    ticks = nice_ticks(lo_bound, hi + pad)
    fr = Frame(y_lo=ticks[0], y_hi=ticks[-1], n_slots=n,
               y_fmt=fit_fmt(ticks, y_fmt_prec, y_fmt))
    fr.grid(ticks)

    bw = min(24.0, fr.step * 0.55)
    for i, h in enumerate(hist):
        fr.svg.add(rounded_bar(fr.x(i) - bw / 2, fr.y(h["value"]), bw, fr.y(0), S1, i))
        fr.slots.append({"label": f'{h["label"]} · {h["cal"]}',
                         "items": [[f"{series_name} (reported)", table_fmt(h["value"]), "var(--s1)"]]})

    base_i = len(hist)
    pts, band_hi, band_lo = [], [], []
    for j, f in enumerate(fore):
        i = base_i + j
        pts.append((fr.x(i), fr.y(f["base"])))
        band_hi.append((fr.x(i), fr.y(f["high"])))
        band_lo.append((fr.x(i), fr.y(f["low"])))
        src = {"consensus_quarter": "analyst consensus",
               "consensus_annual_split": "consensus (seasonal split)",
               "extrapolated": "model extrapolation",
               "pre_revenue_placeholder": "model extrapolation (pre-revenue)",
               }.get(f["source"], f["source"].replace("_", " "))
        fr.slots.append({"label": f'{f["label"]} · {f["cal"]}',
                         "items": [[f"base ({src})", table_fmt(f["base"]), "var(--s1)"],
                                   ["bull", table_fmt(f["high"]), ""],
                                   ["bear", table_fmt(f["low"]), ""]]})
    # connect last reported point into the forecast line
    lead = (fr.x(base_i - 1), fr.y(hist[-1]["value"]))
    band = [lead] + band_hi + band_lo[::-1] + [lead]
    fr.svg.add('<path fill="var(--s1)" opacity="0.1" d="M' +
               " L".join(f"{x:.1f},{y:.1f}" for x, y in band) + ' Z"/>')
    line_pts = [lead] + pts
    fr.svg.add('<path fill="none" stroke="var(--s1)" stroke-width="2" '
               'stroke-linejoin="round" stroke-linecap="round" d="M' +
               " L".join(f"{x:.1f},{y:.1f}" for x, y in line_pts) + '"/>')
    # end marker + selective direct label (endpoint only)
    ex, ey = pts[-1]
    fr.svg.add(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4.5" fill="var(--s1)" '
               f'stroke="var(--surface)" stroke-width="2"/>')
    fr.svg.text(ex - 2, ey - 12, table_fmt(fore[-1]["base"]), 12, "var(--ink2)", "end", 600)

    # region demarcations: reported | consensus-anchored | modeled
    first_extrap = next((base_i + j for j, f in enumerate(fore) if f["source"] == "extrapolated"), None)
    for bx, lab_l, lab_r in [(base_i - 0.5, "Reported", "Consensus")] + (
            [(first_extrap - 0.5, None, "Modeled")] if first_extrap else []):
        xx = fr.x0 + bx * fr.step
        fr.svg.line(xx, fr.top, xx, fr.top + fr.ph, "var(--baseline)", 1)
        if lab_l:
            fr.svg.text(xx - 6, fr.top - 8, lab_l, 11, anchor="end")
        fr.svg.text(xx + 6, fr.top - 8, lab_r, 11, anchor="start")

    fr.x_labels([h["label"] for h in hist] + [f["label"] for f in fore], every=4)
    fr.crosshair()

    rows = [[h["label"], table_fmt(h["value"]), "—", "—", "reported"] for h in hist]
    rows += [[f["label"], table_fmt(f["base"]), table_fmt(f["low"]), table_fmt(f["high"]),
              f["source"].replace("_", " ")] for f in fore]
    return {
        "id": chart_id, "title": title, "subtitle": subtitle,
        "legend": [{"kind": "swatch", "color": "var(--s1)", "label": f"{series_name} — reported"},
                   {"kind": "line", "color": "var(--s1)", "label": "forecast base"},
                   {"kind": "band", "color": "var(--s1)", "label": "bear–bull range"}],
        "svg": fr.svg.render(), "data_json": fr.data_json(),
        "table": {"head": ["Quarter", series_name, "Bear", "Bull", "Source"], "rows": rows},
    }


def line_chart(labels, cals, series, y_fmt, val_fmt, chart_id, title, subtitle,
               vbw=960, y_zero=False, x_every=None, end_labels=True, axis_labels=None,
               y_fmt_prec=None):
    """series: list of (name, color, [values])."""
    vals = [v for _, _, vs in series for v in vs if v is not None]
    # y_zero anchors the axis at zero; it must not clamp the floor to zero, or a
    # series that goes negative (operating cash flow and FCF for a cash-burning
    # company, quarterly burn plotted as an outflow) is drawn below the axis and
    # runs off the bottom of the plot.
    lo = min(vals + [0]) if y_zero else min(vals)
    hi = max(vals + [0]) if y_zero else max(vals)
    pad = (hi - lo) * 0.08 or abs(hi) * 0.05 or 1
    floor = lo if (y_zero and lo == 0) else lo - pad
    # Padding below a non-negative series invents an impossible region (a share
    # price of -$50) and wastes vertical space the data could use.
    if lo >= 0:
        floor = max(floor, 0.0)
    ticks = nice_ticks(floor, hi + pad)
    fr = Frame(vbw=vbw, y_lo=ticks[0], y_hi=ticks[-1], n_slots=len(labels),
               y_fmt=fit_fmt(ticks, y_fmt_prec, y_fmt))
    fr.grid(ticks)
    for name, color, vs in series:
        pts = [(fr.x(i), fr.y(v)) for i, v in enumerate(vs) if v is not None]
        fr.svg.add(f'<path fill="none" stroke="{color}" stroke-width="2" '
                   'stroke-linejoin="round" stroke-linecap="round" d="M' +
                   " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>')
        ex, ey = pts[-1]
        fr.svg.add(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4" fill="{color}" '
                   f'stroke="var(--surface)" stroke-width="2"/>')
        if end_labels:
            fr.svg.text(ex - 8, ey - 10, val_fmt(vs[-1]), 11.5, "var(--ink2)", "end", 600)
    for i, lab in enumerate(labels):
        fr.slots.append({"label": f"{lab}" + (f" · {cals[i]}" if cals else ""),
                         "items": [[name, val_fmt(vs[i]) if vs[i] is not None else "—", color]
                                   for name, color, vs in series]})
    fr.x_labels(axis_labels or labels, every=x_every)
    fr.crosshair()
    rows = [[labels[i]] + [val_fmt(vs[i]) if vs[i] is not None else "—" for _, _, vs in series]
            for i in range(len(labels))]
    legend = ([{"kind": "line", "color": c, "label": n} for n, c, _ in series]
              if len(series) > 1 else None)
    return {"id": chart_id, "title": title, "subtitle": subtitle, "legend": legend,
            "svg": fr.svg.render(), "data_json": fr.data_json(),
            "table": {"head": ["Period"] + [n for n, _, _ in series], "rows": rows}}


def stacked_chart(labels, cals, series, chart_id, title, subtitle, vbw=960):
    """series: list of (name, color, [values]); stacked columns with 2px surface gaps."""
    totals = [sum(vs[i] or 0 for _, _, vs in series) for i in range(len(labels))]
    # Take the floor from the ticks rather than pinning it at 0: a segment that
    # nets negative in some quarter would otherwise stack below the axis.
    ticks = nice_ticks(min(totals + [0]), max(totals + [0]) * 1.06 or 1)
    fr = Frame(vbw=vbw, y_lo=ticks[0], y_hi=ticks[-1], n_slots=len(labels),
               y_fmt=fit_fmt(ticks, money, lambda v: money(v, 0)))
    fr.grid(ticks)
    bw = min(24.0, fr.step * 0.55)
    gap = 2.0
    for i in range(len(labels)):
        y_cursor = fr.y(0)
        for k, (name, color, vs) in enumerate(series):
            v = vs[i] or 0
            h = fr.y(0) - fr.y(v)
            top = y_cursor - h
            is_top = k == len(series) - 1
            if is_top:
                fr.svg.add(rounded_bar(fr.x(i) - bw / 2, top, bw, y_cursor, color, i))
            else:
                fr.svg.add(f'<rect class="bar" data-i="{i}" x="{fr.x(i) - bw / 2:.1f}" '
                           f'y="{top:.1f}" width="{bw:.1f}" height="{max(h - gap, 0):.1f}" '
                           f'fill="{color}"/>')
            y_cursor = top
        fr.slots.append({"label": f"{labels[i]}" + (f" · {cals[i]}" if cals else ""),
                         "items": [[name, money((vs[i] or 0)), color]
                                   for name, color, vs in reversed(series)]
                                  + [["total", money(totals[i]), ""]]})
    fr.x_labels(labels)
    fr.crosshair()
    rows = [[labels[i]] + [money(vs[i] or 0) for _, _, vs in series] + [money(totals[i])]
            for i in range(len(labels))]
    return {"id": chart_id, "title": title, "subtitle": subtitle,
            "legend": [{"kind": "swatch", "color": c, "label": n} for n, c, _ in series],
            "svg": fr.svg.render(), "data_json": fr.data_json(),
            "table": {"head": ["Quarter"] + [n for n, _, _ in series] + ["Total"], "rows": rows}}


# ------------------------------------------------------------------- context --
def load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def build_context(ticker: str) -> dict:
    d = ROOT / "data" / ticker
    fin = load(d / "financials.json")
    market = load(d / "market.json") or {}
    est = load(d / "estimates.json") or {}
    fc = load(d / "forecast.json")
    ir = load(d / "ir_workbook.json")
    narrative = load(d / "narrative.json")
    segments = load(d / "segments.json")
    sentiment = load(d / "sentiment.json")
    filings = load(d / "filings.json")
    if not fin or not fc:
        raise SystemExit(f"Missing financials/forecast for {ticker} — run fetch.py first")
    if not segments and ir:
        # Legacy data dirs have an IR workbook but no normalized segments.json.
        import ir_ingest
        segments = ir_ingest.to_segments(ir)

    # Pre-revenue issuers file a full P&L and balance sheet with no revenue line,
    # so anchor history on net income and drive the revenue-relative views off.
    pre_rev = bool(fc.get("pre_revenue"))
    if pre_rev:
        hist_all = [q for q in fin["quarters"] if q.get("net_income") is not None]
    else:
        hist_all = [q for q in fin["quarters"] if q.get("revenue")]
    hist = hist_all[-12:]
    last = hist[-1]
    prior_year = next((q for q in hist_all if q["fiscal_year"] == last["fiscal_year"] - 1
                       and q["fiscal_quarter"] == last["fiscal_quarter"]), None)

    stats = market.get("stats", {})
    prof = market.get("profile", {})
    tgt = (est.get("price_targets") or {})

    def yoy(cur, prev):
        return cur / prev - 1 if cur is not None and prev else None

    price = stats.get("price")
    upside = (tgt.get("mean") / price - 1) if price and tgt.get("mean") else None
    header_stats = [
        {"label": "Price", "value": f"${price:,.2f}" if price else "—"},
        {"label": "Market cap", "value": money(stats.get("market_cap"), 2)},
        {"label": "Trailing P/E", "value": f'{stats["trailing_pe"]:.1f}' if stats.get("trailing_pe") else "—"},
        {"label": "Forward P/E", "value": f'{stats["forward_pe"]:.1f}' if stats.get("forward_pe") else "—"},
        {"label": "Dividend yield", "value": pct(stats["dividend_yield"] / 100, 2) if stats.get("dividend_yield") else "—"},
        {"label": "Beta", "value": f'{stats["beta"]:.2f}' if stats.get("beta") else "—"},
        {"label": "52-week range", "value": f'${stats["52w_low"]:,.0f}–${stats["52w_high"]:,.0f}'
         if stats.get("52w_low") else "—"},
        {"label": "Analyst mean target", "value": (f'${tgt["mean"]:,.0f} ({pct(upside, 1, True)})'
         if tgt.get("mean") and upside is not None else "—")},
    ]

    # ---- KPI tiles -----------------------------------------------------------
    def tile(label, value, growth, vs, fmt_growth=lambda g: pct(g, 1, True), invert=False):
        t = {"label": label, "value": value}
        if growth is not None:
            t["delta"] = fmt_growth(growth)
            t["delta_dir"] = "up" if (growth >= 0) != invert else "down"
            t["vs"] = vs
        return t

    vs_label = f'{last["fiscal_label"].replace(" ", "")} prior year'
    op_m = (last["operating_income"] / last["revenue"]
            if last.get("operating_income") and last.get("revenue") else None)
    op_m_prev = (prior_year["operating_income"] / prior_year["revenue"]
                 if prior_year and prior_year.get("operating_income")
                 and prior_year.get("revenue") else None)
    fcf = (last["operating_cash_flow"] - last["capex"]
           if last.get("operating_cash_flow") is not None and last.get("capex") is not None else None)
    fcf_prev = (prior_year["operating_cash_flow"] - prior_year["capex"]
                if prior_year and prior_year.get("operating_cash_flow") is not None
                and prior_year.get("capex") is not None else None)

    if pre_rev:
        # Revenue, margin and FCF tiles are meaningless with no revenue; the
        # decision-relevant numbers are the burn and how long it is funded.
        fa = fc["assumptions"]
        runway = fa.get("runway_quarters_at_projected_burn")
        tiles = [
            tile(f'Operating loss — {last["fiscal_label"]}', money(last.get("operating_income")),
                 yoy(last.get("operating_income"),
                     prior_year.get("operating_income") if prior_year else None),
                 "prior year", invert=True),
            tile(f'Diluted EPS — {last["fiscal_label"]}', eps_fmt(last.get("eps_diluted")),
                 yoy(last.get("eps_diluted"),
                     prior_year.get("eps_diluted") if prior_year else None),
                 "prior year", invert=True),
            tile("Cash + marketable securities",
                 money(last.get("total_liquidity") or last.get("cash")),
                 yoy(last.get("total_liquidity") or last.get("cash"),
                     (prior_year.get("total_liquidity") or prior_year.get("cash"))
                     if prior_year else None), "prior year"),
            tile("Quarterly burn — last reported", money(-fa["quarterly_burn_recent"])
                 if fa.get("quarterly_burn_recent") else "—", None, None),
        ]
        # The runway is driven by the forward burn, not the trailing average, so
        # show the next projected quarter alongside it or the tile misleads.
        nxt = next((b for b in (fa.get("projected_quarterly_burn") or []) if b), None)
        if nxt:
            tiles.append(tile("Quarterly burn — next projected", money(-nxt), None, None))
        if runway is not None:
            tiles.append(tile("Runway at projected burn",
                              f"{runway} quarters" if isinstance(runway, str)
                              else f"{runway} quarters", None, None))
    else:
        tiles = [
            tile(f'Revenue — {last["fiscal_label"]}', money(last["revenue"]),
                 yoy(last["revenue"], prior_year["revenue"] if prior_year else None), "prior year"),
            tile(f'Diluted EPS — {last["fiscal_label"]}', eps_fmt(last.get("eps_diluted")),
                 yoy(last.get("eps_diluted"), prior_year.get("eps_diluted") if prior_year else None), "prior year"),
            tile("Operating margin", pct(op_m) if op_m else "—",
                 (op_m - op_m_prev) if op_m and op_m_prev else None, "prior year",
                 fmt_growth=lambda g: f"{g * 100:+.1f} pp"),
            tile("Free cash flow", money(fcf), yoy(fcf, fcf_prev), "prior year"),
        ]
    # beat/miss chip on EPS tile from Yahoo earnings history
    hist_rows = est.get("earnings_history") or []
    hr = next((r for r in reversed(hist_rows) if r.get("epsActual") is not None
               and r.get("epsEstimate")), None)
    if hr:
        surprise = hr["epsActual"] / hr["epsEstimate"] - 1
        tiles[1]["beat"] = f'{"Beat" if surprise >= 0 else "Miss"} {pct(abs(surprise), 1)}'
        tiles[1]["beat_dir"] = "up" if surprise >= 0 else "down"

    # ---- charts --------------------------------------------------------------
    if pre_rev:
        h_rev = [{"label": q["fiscal_label"], "cal": q["calendar_label"],
                  "value": q["operating_income"]}
                 for q in hist if q.get("operating_income") is not None]
        f_rev = [{"label": q["fiscal_label"], "cal": q["calendar_label"], "source": q["source"],
                  "base": q["operating_loss"]["base"], "low": q["operating_loss"]["low"],
                  "high": q["operating_loss"]["high"]}
                 for q in fc["quarters"] if q.get("operating_loss")]
        charts = {"revenue": quarterly_chart(
            h_rev, f_rev, lambda v: money(v, 0), "Operating loss", "chart-rev",
            "Quarterly operating loss — reported + forecast",
            "The company has never reported revenue, so the spend path replaces it. "
            "Columns are reported quarters (SEC filings); the line is the modeled "
            "burn ramp with a bear–bull band.",
            lambda v: money(v, 1), y_fmt_prec=money)}
    else:
        h_rev = [{"label": q["fiscal_label"], "cal": q["calendar_label"], "value": q["revenue"]}
                 for q in hist]
        f_rev = [{"label": q["fiscal_label"], "cal": q["calendar_label"], "source": q["source"],
                  "base": q["revenue"]["base"], "low": q["revenue"]["low"], "high": q["revenue"]["high"]}
                 for q in fc["quarters"]]
        charts = {"revenue": quarterly_chart(
            h_rev, f_rev, lambda v: money(v, 0), "Revenue", "chart-rev",
            "Quarterly revenue — 12 reported + 12 forecast quarters",
            "Columns are reported quarters (SEC filings); the line is the forecast base with a bear–bull band.",
            lambda v: money(v, 1), y_fmt_prec=money)}

    h_eps = [{"label": q["fiscal_label"], "cal": q["calendar_label"], "value": q["eps_diluted"]}
             for q in hist if q.get("eps_diluted") is not None]
    f_eps = [{"label": q["fiscal_label"], "cal": q["calendar_label"], "source": q["source"],
              "base": q["eps"]["base"], "low": q["eps"]["low"], "high": q["eps"]["high"]}
             for q in fc["quarters"] if q.get("eps")]
    charts["eps"] = quarterly_chart(
        h_eps, f_eps, lambda v: f"-${abs(v):g}" if v < 0 else f"${v:g}",
        "Diluted EPS", "chart-eps",
        "Quarterly diluted EPS — reported + forecast",
        "Consensus quarters use analyst low/avg/high; modeled quarters inherit the consensus-implied margin path.",
        eps_fmt)

    if pre_rev:
        # Every margin is revenue-relative, so show the funding picture instead:
        # what the balance sheet holds against what each quarter consumes.
        liq_series = []
        liq_vs = [q.get("total_liquidity") or q.get("cash") for q in hist]
        if any(v is not None for v in liq_vs):
            liq_series.append(("Cash + marketable securities", S1, liq_vs))
        burn_vs = [-(abs(q["operating_cash_flow"]) + q["capex"])
                   if q.get("operating_cash_flow") is not None and q.get("capex") is not None
                   else None for q in hist]
        if sum(v is not None for v in burn_vs) >= 4:
            liq_series.append(("Quarterly burn (opex + capex)", S2, burn_vs))
        charts["margins"] = line_chart(
            [q["fiscal_label"] for q in hist], [q["calendar_label"] for q in hist],
            liq_series, lambda v: money(v, 0), lambda v: money(v, 1), "chart-margins",
            "Liquidity vs quarterly burn",
            "Trailing reported quarters. Margins are omitted: with no revenue there is "
            "nothing to take a margin against.", vbw=470, x_every=4, y_zero=True,
            y_fmt_prec=money)
    else:
        m_series = []
        for name, color, key in (("Gross margin", S1, "gross_profit"),
                                 ("Operating margin", S2, "operating_income"),
                                 ("Net margin", S3, "net_income")):
            vs = [q[key] / q["revenue"] if q.get(key) is not None else None for q in hist]
            if any(v is not None for v in vs):
                m_series.append((name, color, vs))
        fcf_m = [q["operating_cash_flow"] / q["revenue"] - q["capex"] / q["revenue"]
                 if q.get("operating_cash_flow") is not None and q.get("capex") is not None
                 else None for q in hist]
        if sum(v is not None for v in fcf_m) >= 4:
            m_series.append(("FCF margin", S4, fcf_m))
        charts["margins"] = line_chart(
            [q["fiscal_label"] for q in hist], [q["calendar_label"] for q in hist],
            m_series, lambda v: pct(v, 0), lambda v: pct(v, 1), "chart-margins",
            "Margin trajectory", "Trailing 12 reported quarters.", vbw=470, x_every=4)

    charts["segments"] = None
    seg_source_label = None
    if segments and segments.get("segments"):
        segs = segments["segments"]
        all_q = sorted({q for s in segs for q in s["quarters"]},
                       key=lambda lab: (int(lab[2:4]), int(lab[-1])))[-8:]
        segs = sorted(segs, key=lambda s: -(s["quarters"].get(all_q[-1]) or 0))
        if len(segs) > 3:  # fixed 3-hue order: fold the tail into "Other"
            other = {q: sum(s["quarters"].get(q) or 0 for s in segs[2:]) for q in all_q}
            segs = segs[:2] + [{"name": "Other", "quarters": other}]
        series = [(s["name"], [S1, S2, S3][i], [s["quarters"].get(q) for q in all_q])
                  for i, s in enumerate(segs)]
        seg_source_label = {"ir_workbook": "company IR workbook",
                           "sec_rfile": "SEC filing segment tables",
                           "manual": "company disclosures"}.get(segments.get("source"),
                                                                segments.get("source", ""))
        charts["segments"] = stacked_chart(
            [q for q in all_q], None, series, "chart-seg", "Segment revenue",
            f"From the {seg_source_label}.", vbw=470)

    # ---- cash generation & balance sheet ------------------------------------
    def ttm(key, offset=0):
        rows = hist_all[len(hist_all) - 4 - offset: len(hist_all) - offset]
        vals = [q.get(key) for q in rows]
        return sum(vals) if len(vals) == 4 and all(v is not None for v in vals) else None

    charts["cash"] = None
    cash_series = []
    for name, color, key in (("Operating cash flow", S1, "operating_cash_flow"),
                             ("Capex", S2, "capex")):
        vs = [q.get(key) for q in hist]
        if sum(v is not None for v in vs) >= 4:
            cash_series.append((name, color, vs))
    if len(cash_series) == 2:
        fcf_vs = [q["operating_cash_flow"] - q["capex"]
                  if q.get("operating_cash_flow") is not None and q.get("capex") is not None
                  else None for q in hist]
        cash_series.append(("Free cash flow", S3, fcf_vs))
        ocf_ttm, capex_ttm = ttm("operating_cash_flow"), ttm("capex")
        ocf_ttm_py, capex_ttm_py = ttm("operating_cash_flow", 4), ttm("capex", 4)
        bits = []
        # Both framings below assume operations generate cash. When they consume
        # it, "capex as a share of operating cash" is a negative percentage of a
        # negative number and a "growth" rate compares two deficits, so state the
        # absolute burn instead of dividing one outflow by another.
        if ocf_ttm is not None and ocf_ttm > 0 and capex_ttm is not None:
            bits.append(f"Capex consumes {pct(capex_ttm / ocf_ttm, 1)} of operating cash (TTM)")
            if capex_ttm_py and ocf_ttm_py and ocf_ttm_py > 0:
                fcf_g = yoy(ocf_ttm - capex_ttm, ocf_ttm_py - capex_ttm_py)
                if fcf_g is not None:
                    bits.append(f"TTM capex {pct(yoy(capex_ttm, capex_ttm_py), 0, True)} YoY vs "
                                f"FCF {pct(fcf_g, 0, True)} YoY")
        elif ocf_ttm is not None and capex_ttm is not None:
            bits.append(f"Operations consumed {money(abs(ocf_ttm))} of cash over the "
                        f"trailing 12 months against {money(capex_ttm)} of capex, "
                        f"a total burn of {money(abs(ocf_ttm) + capex_ttm)}")
        charts["cash"] = line_chart(
            [q["fiscal_label"] for q in hist], [q["calendar_label"] for q in hist],
            cash_series, lambda v: money(v, 0), lambda v: money(v, 1), "chart-cash",
            "Cash generation vs capital spending",
            (". ".join(b for b in bits if b) + "." if bits
             else "Quarterly operating cash flow, capex and free cash flow."),
            y_zero=True, x_every=2, y_fmt_prec=money)

    charts["cashdebt"] = None
    balance = []
    bal_q = next((q for q in reversed(hist_all) if q.get("total_assets") is not None), None)
    if bal_q:
        bal_prior = next((q for q in hist_all
                          if q["fiscal_year"] == bal_q["fiscal_year"] - 1
                          and q["fiscal_quarter"] == bal_q["fiscal_quarter"]), None)
        liquid = (bal_q["cash"] or 0) + (bal_q.get("short_term_investments") or 0) \
            if bal_q.get("cash") is not None else None
        rows = [("Total assets", "total_assets", None, False),
                ("Total liabilities", "total_liabilities", None, True),
                ("Stockholders' equity", "stockholders_equity", None, False),
                ("Cash + short-term investments", None, liquid, False),
                ("Cash + all marketable securities", "total_liquidity", None, False),
                ("Long-term debt", "long_term_debt", None, True),
                ("Net cash (debt)", "net_cash", None, False),
                ("Inventory", "inventory", None, False),
                ("Accounts receivable", "accounts_receivable", None, False),
                ("Goodwill", "goodwill", None, False)]
        for label, key, direct, invert in rows:
            val = direct if key is None else bal_q.get(key)
            if val is None:
                continue
            entry = {"label": label, "value": money(val)}
            prev = None
            if bal_prior:
                if key is None:
                    prev = ((bal_prior["cash"] or 0) + (bal_prior.get("short_term_investments") or 0)
                            if bal_prior.get("cash") is not None else None)
                else:
                    prev = bal_prior.get(key)
            g = yoy(val, prev) if prev else None
            if g is not None:
                entry["delta"] = pct(g, 1, True)
                entry["delta_dir"] = "up" if (g >= 0) != invert else "down"
            balance.append(entry)

        liq_vs = [(q["cash"] or 0) + (q.get("short_term_investments") or 0)
                  if q.get("cash") is not None else None for q in hist]
        debt_vs = [q.get("long_term_debt") for q in hist]
        if sum(v is not None for v in liq_vs) >= 4:
            cd_series = [("Cash + ST investments", S1, liq_vs)]
            if sum(v is not None for v in debt_vs) >= 4:
                cd_series.append(("Long-term debt", S2, debt_vs))
            charts["cashdebt"] = line_chart(
                [q["fiscal_label"] for q in hist], [q["calendar_label"] for q in hist],
                cd_series, lambda v: money(v, 0), lambda v: money(v, 1), "chart-cashdebt",
                "Liquidity vs long-term debt", "Balance-sheet snapshots at quarter end.",
                vbw=470, y_zero=True, x_every=4, y_fmt_prec=money)

    charts["capret"] = None
    bb_vs = [q.get("buybacks") for q in hist]
    dv_vs = [q.get("dividends_paid") for q in hist]
    if sum(v is not None and v > 0 for v in bb_vs + dv_vs) >= 4:
        cr_series = [("Buybacks", S1, bb_vs)]
        if any(v for v in dv_vs):
            cr_series.append(("Dividends", S2, dv_vs))
        charts["capret"] = stacked_chart(
            [q["fiscal_label"] for q in hist], [q["calendar_label"] for q in hist],
            cr_series, "chart-capret", "Capital returned to shareholders",
            "Quarterly share repurchases and dividends paid (cash-flow statement).")

    prices = market.get("prices") or []
    if prices:
        pr = prices[:: max(1, len(prices) // 240)]
        if pr[-1] is not prices[-1]:
            pr.append(prices[-1])  # never drop the latest close
        charts["price"] = line_chart(
            [datetime.fromisoformat(p["date"]).strftime("%b %d, %Y") for p in pr], None,
            [("Close", S1, [p["close"] for p in pr])],
            lambda v: f"${v:,.0f}", lambda v: f"${v:,.2f}", "chart-price",
            "Share price — trailing 2 years", "Daily close, split/dividend-adjusted.",
            x_every=max(1, len(pr) // 8),
            # A sub-$1 stock would otherwise label every tick "$0".
            y_fmt_prec=lambda v, d: f"${v:,.{d}f}",
            axis_labels=[datetime.fromisoformat(p["date"]).strftime("%b '%y") for p in pr])
    else:
        charts["price"] = line_chart(["n/a"], None, [("Close", S1, [0])],
                                     lambda v: "", lambda v: "—", "chart-price",
                                     "Share price", "No price data available.")

    # ---- forecast table + assumptions ---------------------------------------
    forecast_rows = [{
        "fiscal": q["fiscal_label"], "calendar": q["calendar_label"],
        # Pre-revenue: the modelled operating loss is the real column; consensus
        # "revenue" here is a rounding artefact and would only mislead.
        "rev": (money(q["operating_loss"]["base"]) if pre_rev and q.get("operating_loss")
                else money(q["revenue"]["base"])),
        "rev_range": (f'{money(q["operating_loss"]["low"])} – {money(q["operating_loss"]["high"])}'
                      if pre_rev and q.get("operating_loss")
                      else f'{money(q["revenue"]["low"])} – {money(q["revenue"]["high"])}'),
        "eps": eps_fmt(q["eps"]["base"]) if q.get("eps") else "—",
        "eps_range": (f'{eps_fmt(q["eps"]["low"])} – {eps_fmt(q["eps"]["high"])}'
                      if q.get("eps") else "—"),
        "source": q["source"].replace("_", " "),
    } for q in fc["quarters"]]

    a = fc["assumptions"]
    if pre_rev:
        runway = a.get("runway_quarters_at_projected_burn")
        assumptions = [
            {"label": "Model", "value": a.get("model", "pre-revenue")},
            {"label": "Operating-loss run rate (recent)",
             "value": money(-a["operating_loss_run_rate"]) if a.get("operating_loss_run_rate") else "—"},
            {"label": "Opex growth, first forecast year", "value": pct(a.get("opex_growth_per_year"))},
            {"label": "Opex growth by fiscal year",
             "value": ", ".join(f"FY{y[2:]}: {pct(g)}" for y, g in
                                (a.get("opex_growth_by_fy") or {}).items()) or "—"},
            {"label": "Opex growth decay per year", "value": pct(a.get("growth_decay_per_year"))},
            {"label": "Quarterly cash burn (recent avg)",
             "value": money(-a["quarterly_burn_recent"]) if a.get("quarterly_burn_recent") else "—"},
            {"label": "Liquidity, last reported",
             "value": money(a.get("liquidity_last_reported"))},
            {"label": "Runway at projected burn",
             "value": f"{runway} quarters" if runway is not None else "—"},
            {"label": "Quarterly share-count change (dilution)",
             "value": pct(a.get("quarterly_dilution_rate"), 3, True)},
            {"label": "Revenue treatment", "value": a.get("revenue_consensus_note", "—")},
        ]
    else:
        assumptions = [
            {"label": "Revenue seasonality (Q1–Q4 share of FY)",
             "value": " / ".join(pct(v, 1) for v in a["seasonality"].values())},
            {"label": "3-year revenue CAGR (reported)", "value": pct(a["revenue_cagr_3y"])},
            {"label": "Consensus growth, next FY", "value": pct(a.get("consensus_growth_next_fy"))},
            {"label": "Long-run growth (modeled years)", "value": pct(a["long_run_growth"])},
            {"label": "Modeled growth by fiscal year",
             "value": ", ".join(f"FY{y[2:]}: {pct(g)}" for y, g in a["growth_by_fy"].items())},
            {"label": "Net margin, recent 4-quarter avg", "value": pct(a["net_margin_recent_avg"])},
            {"label": "Margin drift per year", "value": f'{a["margin_drift_per_year"] * 100:+.1f} pp'},
            {"label": "Quarterly share-count change", "value": pct(a["quarterly_buyback_rate"], 3, True)},
            {"label": "Revenue growth volatility (8q σ)", "value": pct(a["growth_volatility_8q"])},
        ]
    if a.get("override_applied"):
        assumptions.append({"label": "Analyst override applied",
                            "value": json.dumps(a["override_applied"])})

    # ---- social sentiment ----------------------------------------------------
    senti_ctx = None
    if sentiment and (sentiment.get("summary") or {}).get("post_count"):
        s = sentiment["summary"]
        trend = sentiment.get("trend") or []
        charts["senti_trend"] = None
        if len(trend) >= 5:
            charts["senti_trend"] = line_chart(
                [datetime.fromisoformat(t["date"]).strftime("%b %d") for t in trend], None,
                [("Avg sentiment", S1, [t["avg_compound"] for t in trend])],
                lambda v: f"{v:+.1f}", lambda v: f"{v:+.2f}", "chart-senti",
                "Daily sentiment trend",
                "VADER compound score, −1 (negative) to +1 (positive), averaged per day.",
                vbw=470, x_every=max(1, len(trend) // 6))
        senti_ctx = {
            "summary": s,
            "window_days": sentiment.get("window_days"),
            "by_source": sentiment.get("by_source") or {},
            "top_posts": (sentiment.get("top_posts") or [])[:5],
            "notes": sentiment.get("notes") or [],
            "fetched_at": sentiment.get("fetched_at", ""),
            "dist": [("Positive", round(s.get("pct_positive", 0) * 100), "var(--s3)"),
                     ("Neutral", round(s.get("pct_neutral", 0) * 100), "var(--muted)"),
                     ("Negative", round(s.get("pct_negative", 0) * 100), "var(--s2)")],
        }

    sources = [{"label": "SEC EDGAR company facts (XBRL)",
                "url": f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={fin["cik"]}'},
               {"label": "Yahoo Finance analyst estimates",
                "url": f"https://finance.yahoo.com/quote/{ticker}/analysis"}]
    if ir:
        sources.append({"label": "Company IR financial workbook", "url": ir["source_url"]})
    if segments and segments.get("source_url") and not ir:
        sources.append({"label": f"Segment data ({seg_source_label})",
                        "url": segments["source_url"]})
    for f in (filings or {}).get("filings", [])[:6]:
        if f.get("primary_doc_url"):
            sources.append({"label": f'SEC {f["form"]} filed {f["filing_date"]}',
                            "url": f["primary_doc_url"]})
    if senti_ctx:
        sources.append({"label": "Reddit & StockTwits chatter (see sentiment section)",
                        "url": f"https://www.reddit.com/search/?q=%24{ticker}"})
    for s in (narrative or {}).get("sources", []):
        sources.append(s if isinstance(s, dict) else {"label": s, "url": s})

    return {
        "ticker": ticker,
        "company": fin.get("company") or prof.get("name") or ticker,
        "sector": prof.get("sector") or "", "industry": prof.get("industry"),
        "last_quarter": last,
        "header_stats": header_stats, "tiles": tiles, "charts": charts,
        "balance": balance, "balance_asof": bal_q["fiscal_label"] if bal_q else None,
        "sentiment": senti_ctx,
        "narrative": narrative, "forecast_rows": forecast_rows,
        "forecast_col_label": "Operating loss" if pre_rev else "Revenue",
        "assumptions": assumptions, "sources": sources, "has_ir": bool(ir),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def main() -> int:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "MSFT").upper()
    # A fund has fund.json and no financials.json — render its own dashboard
    # rather than failing on absent fundamentals.
    data_dir = ROOT / "data" / ticker
    if (data_dir / "fund.json").exists() and not (data_dir / "financials.json").exists():
        import fund_render
        return 0 if fund_render.render(ticker) else 1
    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      autoescape=select_autoescape(["html", "j2"]))
    ctx = build_context(ticker)
    html = env.get_template("dashboard.html.j2").render(**ctx)
    out = ROOT / "dashboards" / f"{ticker}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
