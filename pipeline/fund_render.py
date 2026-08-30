"""Render dashboards/<SYMBOL>.html for a fund from data/<SYMBOL>/fund.json.

Deliberately contains no position sizes: these land in dashboards/, which is
committed to the repo, so they describe the fund only. How much of it you own
belongs to the portfolio view under finance_data/.

Usage:  python pipeline/fund_render.py FXAIX [VTI ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from render import Frame, money, nice_ticks, pct, rounded_bar  # noqa: E402

S1, S2, S3 = "var(--s1)", "var(--s2)", "var(--s3)"

SECTOR_LABELS = {
    "technology": "Technology", "financial_services": "Financial Svcs",
    "consumer_cyclical": "Cons. Cyclical", "healthcare": "Healthcare",
    "communication_services": "Communication", "industrials": "Industrials",
    "consumer_defensive": "Cons. Defensive", "energy": "Energy",
    "utilities": "Utilities", "realestate": "Real Estate",
    "basic_materials": "Materials",
}


def bar_chart(labels, values, chart_id, title, subtitle, y_fmt, val_fmt):
    ticks = nice_ticks(0, max(values) * 1.1)
    fr = Frame(vbw=960, y_lo=0, y_hi=ticks[-1], n_slots=len(labels),
               y_fmt=y_fmt, bottom=48)
    fr.grid(ticks)
    bw = min(40.0, fr.step * 0.6)
    for i, (lab, v) in enumerate(zip(labels, values)):
        fr.svg.add(rounded_bar(fr.x(i) - bw / 2, fr.y(v), bw, fr.y(0), S1, i))
        fr.slots.append({"label": lab, "items": [["weight", val_fmt(v), S1]]})
    fr.x_labels(labels, every=1)
    fr.crosshair()
    return {"id": chart_id, "title": title, "subtitle": subtitle,
            "svg": fr.svg.render(), "data_json": fr.data_json(),
            "table": {"head": ["Name", "Weight"],
                      "rows": [[l, val_fmt(v)] for l, v in zip(labels, values)]}}


def load_narrative(symbol: str) -> dict | None:
    """The findings /analyze wrote for this fund, if it has been run.

    A fund's dashboard used to be data only, which meant the judgment part of
    an analysis — what the thing actually is, what it costs you, what would
    make you sell it — lived in a chat reply and was gone by the next session.
    The keys are a subset of the company narrative: no quarter recap, no
    segments, no forecast rationale, because a basket has none of those.
    """
    p = ROOT / "data" / symbol.upper() / "narrative.json"
    if not p.exists():
        return None
    try:
        n = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[fund-render] {symbol}: narrative.json unreadable ({e}) — skipped")
        return None
    return n or None


def build_context(f: dict, narrative: dict | None = None) -> dict:
    er = f.get("expense_ratio")
    cat = f.get("category_expense_ratio")
    charts = {}

    holds = f.get("top_holdings") or []
    if holds:
        charts["holdings"] = bar_chart(
            [h["symbol"] for h in holds], [h["weight"] for h in holds],
            "chart-holdings", "Largest disclosed holdings",
            f'Top {len(holds)} positions, {pct(sum(h["weight"] for h in holds), 1)} '
            "of the fund. The remainder is not published by the provider.",
            lambda v: pct(v, 0), lambda v: pct(v, 2))

    sect = f.get("sector_weightings") or {}
    if sect:
        items = sorted(sect.items(), key=lambda kv: -kv[1])
        charts["sectors"] = bar_chart(
            [SECTOR_LABELS.get(k, k.replace("_", " ").title()) for k, _ in items],
            [v for _, v in items], "chart-sectors", "Sector weighting",
            "Where the fund's equity sleeve is invested.",
            lambda v: pct(v, 0), lambda v: pct(v, 1))

    ac = f.get("asset_classes") or {}
    mix = [{"name": n, "w": round(ac.get(k, 0) * 100, 1),
            "pct": pct(ac.get(k, 0), 1), "color": c}
           for k, n, c in (("stockPosition", "Stocks", S1),
                           ("bondPosition", "Bonds", S3),
                           ("cashPosition", "Cash", "var(--s4)"),
                           ("preferredPosition", "Preferred", S2),
                           ("convertiblePosition", "Convertible", "var(--muted)"),
                           ("otherPosition", "Other", "var(--grid)"))
           if (ac.get(k) or 0) > 0.0005]

    stats = [
        {"label": "Category", "value": f.get("category") or "—"},
        {"label": "Provider", "value": f.get("family") or "—"},
        {"label": "Fund assets", "value": money(f.get("total_assets"), 1)},
        {"label": "Price", "value": f'${f["price"]:,.2f}' if f.get("price") else "—"},
        {"label": "Distribution yield",
         "value": pct(f.get("yield"), 2) if f.get("yield") is not None else "—"},
        {"label": "3-year beta",
         "value": f'{f["beta_3y"]:.2f}' if f.get("beta_3y") is not None else "—"},
    ]

    perf = [("Year to date", f.get("ytd_return")),
            ("3-year average", f.get("return_3y")),
            ("5-year average", f.get("return_5y"))]
    perf_rows = [{"label": n, "value": pct(v, 2), "dir": "up" if v >= 0 else "down"}
                 for n, v in perf if v is not None]

    n = narrative or {}
    thesis = n.get("thesis") or {}
    return {
        "f": f, "charts": charts, "stats": stats, "mix": mix, "perf_rows": perf_rows,
        "narrative": n or None,
        "n_sources": n.get("sources") or [],
        "n_bull": thesis.get("bull") or [],
        "n_bear": thesis.get("bear") or [],
        "er": pct(er, 3) if er is not None else None,
        "er_zero": er == 0,
        "cat_er": pct(cat, 3) if cat else None,
        "vs_cat": (pct(er - cat, 3, True) if er is not None and cat else None),
        "cheaper": (er is not None and cat is not None and er < cat),
        "cost_per_100k": (f"${er * 100_000:,.0f}" if er is not None else None),
        "expense_source": f.get("expense_source"),
        "fetched_at": f.get("fetched_at", ""),
    }


def render(symbol: str) -> Path | None:
    src = ROOT / "data" / symbol.upper() / "fund.json"
    if not src.exists():
        print(f"[fund-render] {symbol}: no fund.json — run pipeline/fund.py first")
        return None
    f = json.loads(src.read_text(encoding="utf-8"))
    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      autoescape=select_autoescape(["html", "j2"]))
    narrative = load_narrative(symbol)
    html = env.get_template("fund.html.j2").render(**build_context(f, narrative))
    out = ROOT / "dashboards" / f"{symbol.upper()}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[fund-render] wrote {out.name} ({out.stat().st_size // 1024} KB)"
          + ("" if narrative else " — no narrative.json yet, so the dashboard "
                                 "carries data without findings"))
    return out


def main() -> int:
    syms = [s.strip().upper() for s in sys.argv[1:] if s.strip()]
    if not syms:
        print("usage: python pipeline/fund_render.py SYMBOL [SYMBOL ...]")
        return 2
    n = sum(1 for s in syms if render(s))
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
