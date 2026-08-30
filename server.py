"""Local web front-end for PrivatePlan.

Lists analyzed tickers, serves the generated dashboards, and runs the
mechanical pipeline steps (fetch / sources / sentiment / forecast / render)
as subprocesses with live log polling. The narrative, web research, and
assumption overrides are Claude-authored — run /analyze <TICKER> in Claude
Code for those.

Run:
    python server.py        # with the repo .venv activated
    -> http://127.0.0.1:5000
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, send_file, url_for)
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent
VENV_PY = sys.executable
PIPELINE = ROOT / "pipeline"

app = Flask(__name__, template_folder=str(ROOT / "templates" / "web"))

from finance.web import fin_bp  # noqa: E402  (needs ROOT on sys.path via cwd)
app.register_blueprint(fin_bp)

JOBS: dict[str, dict] = {}  # job_id -> {"proc", "log", "ticker", "step", "started"}

DATA_FILES = [
    ("financials.json", "SEC fundamentals"),
    ("market.json", "Market data"),
    ("estimates.json", "Analyst estimates"),
    ("forecast.json", "12-quarter forecast"),
    ("sources.json", "Filings & documents"),
    ("sentiment.json", "Social sentiment"),
    ("segments.json", "Segment revenue"),
    ("narrative.json", "Narrative (Claude)"),
    ("fund.json", "Fund profile (ETFs/funds)"),
]
# Datasets that only exist for an operating company. A fund has no revenue,
# earnings or segments, so listing these against one reports four "missing"
# files that are not missing — they do not apply.
COMPANY_ONLY = {"financials.json", "estimates.json", "forecast.json",
                "segments.json"}
# ...and the steps that produce them. A fund's dashboard comes from fetch.py's
# fund path instead.
COMPANY_ONLY_STEPS = {"forecast"}

STEPS = {
    "fetch": lambda t, extra: [VENV_PY, str(PIPELINE / "fetch.py"), t] + extra,
    "sources": lambda t, extra: [VENV_PY, str(PIPELINE / "sources.py"), t] + extra,
    "sentiment": lambda t, extra: [VENV_PY, str(PIPELINE / "sentiment.py"), t],
    "forecast": lambda t, extra: [VENV_PY, str(PIPELINE / "forecast.py"), t],
    "render": lambda t, extra: [VENV_PY, str(PIPELINE / "render.py"), t],
}


def _valid_ticker(t: str) -> str:
    t = (t or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t):
        abort(400, "invalid ticker")
    return t


def _age(mtime: float | None) -> str | None:
    """Human "how stale is this" from an mtime already in hand.

    Takes the timestamp rather than a path so callers can source it from a
    directory scan: os.scandir hands back mtimes for a whole directory in one
    syscall, where a path-per-file version would stat each one separately.
    """
    if not mtime:
        return None
    delta = datetime.now() - datetime.fromtimestamp(mtime)
    if delta.days > 0:
        return f"{delta.days}d ago"
    hours = delta.seconds // 3600
    return f"{hours}h ago" if hours else f"{delta.seconds // 60}m ago"


def _scan(d: Path) -> dict[str, float]:
    """{filename: mtime} for one directory — one syscall, not one per file."""
    try:
        return {e.name: e.stat().st_mtime for e in os.scandir(d) if e.is_file()}
    except OSError:
        return {}


def _busy_tickers() -> set[str]:
    """Tickers with a live job. Polled once per request, not once per ticker."""
    return {j["ticker"] for j in JOBS.values() if j["proc"].poll() is None}


JOB_TTL = 3600  # keep a finished job's status pollable for an hour


def _reap_jobs() -> None:
    """Drop long-finished jobs so JOBS does not grow for the server's lifetime."""
    cutoff = time.time() - JOB_TTL
    for jid in [k for k, j in JOBS.items()
                if j["at"] < cutoff and j["proc"].poll() is not None]:
        del JOBS[jid]


def _ticker_status(t: str, dash: dict[str, float] | None = None,
                   busy: set[str] | None = None) -> dict:
    # dash/busy are passed in when listing every ticker, so the dashboards
    # directory is scanned once and each job polled once for the whole page.
    if dash is None:
        dash = _scan(ROOT / "dashboards")
    if busy is None:
        busy = _busy_tickers()
    mtimes = _scan(ROOT / "data" / t)
    is_fund = "fund.json" in mtimes
    # Every dataset stays in the list — templates look rows up by name — but a
    # company-only file on a fund is marked as not applying, so it can be shown
    # as "n/a" rather than as a missing file the user is expected to go fetch.
    files = [{"name": name, "label": label, "age": _age(mtimes.get(name)),
              "present": name in mtimes,
              "applies": not (is_fund and name in COMPANY_ONLY)}
             for name, label in DATA_FILES]
    core = "fund.json" if is_fund else "financials.json"
    dash_mtime = dash.get(f"{t}.html")
    return {"ticker": t, "files": files, "is_fund": is_fund,
            "needs_analysis": core not in mtimes,
            "dashboard": dash_mtime is not None, "dashboard_age": _age(dash_mtime),
            "running": t in busy}


def _all_tickers(dash: dict[str, float] | None = None) -> list[str]:
    # dirs starting with "_" are pipeline bookkeeping (e.g. _bulk logs)
    if dash is None:
        dash = _scan(ROOT / "dashboards")
    try:
        dirs = {e.name for e in os.scandir(ROOT / "data")
                if e.is_dir() and not e.name.startswith("_")}
    except OSError:
        dirs = set()
    return sorted(dirs | {n[:-5] for n in dash if n.endswith(".html")})


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_JSON_CACHE: dict[Path, tuple[float, dict | None]] = {}


def _load_cached(path: Path) -> dict | None:
    """_load, but re-parsed only when the file actually changes.

    portfolio.json is ~100KB and is read on every dashboard view to find a
    single row; parsing it per request is pure waste when the pipeline rewrites
    it a few times a day. Keyed on mtime, so a rewrite is picked up at once.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _JSON_CACHE.pop(path, None)
        return None
    hit = _JSON_CACHE.get(path)
    if hit is None or hit[0] != mtime:
        hit = (mtime, _load(path))
        _JSON_CACHE[path] = hit
    return hit[1]


def _home_summaries() -> tuple[dict, dict]:
    """High-level numbers for the two apps, tolerant of missing data."""
    fin_dir = ROOT / "finance_data"
    a = _load(fin_dir / "analysis.json") or {}
    snap = a.get("snapshot") or {}
    pj = a.get("projection") or {}
    mc = a.get("monte_carlo") or {}
    cf = snap.get("cashflow") or {}
    recs = a.get("recommendations") or []
    finance = {
        "ready": bool(a),
        "net_worth": snap.get("net_worth"),
        "portfolio": snap.get("portfolio"),
        "debt": snap.get("total_debt"),
        "success": mc.get("success_prob"),
        "surplus": cf.get("surplus_monthly"),
        "spending": cf.get("expenses_monthly"),
        "sustainable": pj.get("sustainable_monthly"),
        "desired": pj.get("desired_monthly"),
        "retire_age": (pj.get("markers") or {}).get("retire_age"),
        "actions": [r for r in recs if r.get("severity") in ("warning", "action")][:3],
        "n_actions": sum(1 for r in recs if r.get("severity") in ("warning", "action")),
        "generated": a.get("generated"),
        "dashboard": (fin_dir / "dashboard.html").exists(),
        "gaps": a.get("data_gaps") or [],
    }

    p = _load_cached(fin_dir / "portfolio.json") or {}
    stocks = p.get("stocks") or []
    dash = _scan(ROOT / "dashboards")

    def wavg(key):
        num = den = 0.0
        for r in stocks:
            v = r.get(key)
            if v is not None:
                num += v * r["value"]
                den += r["value"]
        return num / den if den else None

    top5 = sum(r["value"] for r in stocks[:5])
    total = p.get("total_portfolio") or 0
    invest = {
        "ready": bool(p),
        "total": total or None,
        "stocks_total": p.get("stocks_total"),
        "funds_total": p.get("funds_total"),
        "cash_total": p.get("cash_total"),
        "n_stocks": len(stocks),
        "n_funds": len(p.get("funds") or []),
        "top": stocks[:5],
        "top5_pct": (top5 / total) if total else None,
        "fwd_pe": wavg("forward_pe"),
        "beta": wavg("beta"),
        "cagr": wavg("forecast_2y_cagr"),
        "upside": wavg("upside"),
        "perf": p.get("performance") or {},
        "generated": p.get("generated"),
        "view": (fin_dir / "portfolio.html").exists(),
        "n_dashboards": sum(1 for n in dash if n.endswith(".html")),
        "n_tracked": len(_all_tickers(dash)),
    }
    return finance, invest


@app.get("/")
def home():
    finance, invest = _home_summaries()
    return render_template("home.html.j2", finance=finance, invest=invest)


@app.get("/stocks")
def index():
    dash, busy = _scan(ROOT / "dashboards"), _busy_tickers()
    rows = [_ticker_status(t, dash, busy) for t in _all_tickers(dash)]
    return render_template("index.html.j2", rows=rows)


def _position_panel(ticker: str) -> str:
    """Your holding in this name, as an HTML card — or '' if you don't own it.

    Deliberately injected when the page is served rather than written into
    dashboards/<TICKER>.html, because that directory is tracked in git while
    finance_data/ is ignored. Baking position sizes and cost basis into those
    files would commit personal holdings to the repository.
    """
    p = _load_cached(ROOT / "finance_data" / "portfolio.json") or {}
    row = None
    for bucket in ("stocks", "funds", "failed"):
        row = next((r for r in (p.get(bucket) or []) if r.get("ticker") == ticker), None)
        if row is not None:
            break
    if row is None:
        return ""

    def money(v, dec=2, signed=False):
        if v is None:
            return "—"
        a, s = abs(v), "-" if v < 0 else ("+" if signed else "")
        for unit, div in (("M", 1e6), ("K", 1e3)):
            if a >= div:
                return f"{s}${a / div:,.{dec}f}{unit}"
        return f"{s}${a:,.2f}"

    cost, gain, gpct = row.get("cost_basis"), row.get("gain"), row.get("gain_pct")
    total = p.get("total_portfolio") or 0
    weight = (row["value"] / total) if total else None
    # ("", "good", "bad") — green for a gain, red for a loss, neutral for the
    # facts that are neither (what it's worth, what it cost, how big it is).
    cells = [("Market value", money(row.get("value")), "")]
    if cost is not None:
        tone = "good" if (gain or 0) > 0 else ("bad" if (gain or 0) < 0 else "")
        arrow = "▲ " if tone == "good" else ("▼ " if tone == "bad" else "")
        cov = row.get("cost_coverage")
        lab = "Cost basis"
        if cov is not None and cov < 0.999:
            lab = f"Cost basis ({cov * 100:.0f}% of position)"
        cells += [(lab, money(cost), ""),
                  ("Gain/loss", arrow + money(gain, signed=True), tone),
                  ("Return on cost",
                   f"{arrow}{gpct * 100:+.1f}%" if gpct is not None else "—", tone)]
    if weight is not None:
        cells.append(("Weight in portfolio", f"{weight * 100:.1f}%", ""))
    shares = row.get("quantity")
    if shares is None and row.get("price") and row.get("value"):
        shares = row["value"] / row["price"]
    if shares:
        cells.append(("Shares" if row.get("quantity") else "Approx. shares",
                      f"{shares:,.2f}".rstrip("0").rstrip("."), ""))

    # Styles are inlined rather than left to the dashboard's stylesheet: this
    # card is injected into an already-rendered dashboards/<T>.html that may
    # predate any CSS rule added here. --good/--bad are defined by both the
    # equity and fund templates, so the colors follow the page's light/dark theme.
    tint = {"good": ("var(--good)", "8%"), "bad": ("var(--bad)", "8%")}

    def tile(lab, val, tone):
        style = box = ""
        if tone:
            col, mix = tint[tone]
            style = f' style="color:{col}"'
            box = (f' style="background:color-mix(in srgb, {col} {mix}, var(--surface));'
                   f'border-color:color-mix(in srgb, {col} 35%, var(--border))"')
        return (f'<div class="tile"{box}><div class="label">{lab}</div>'
                f'<div class="value {tone}"{style}>{val}</div></div>')

    body = "".join(tile(*c) for c in cells)
    note = ("" if cost is not None else
            '<div class="sub">No cost basis found for this position in the '
            'imported statements, so gain/loss cannot be shown.</div>')
    return (f'<div class="card"><h2>Your position</h2>{note}'
            f'<div class="tiles" style="margin-top:10px">{body}</div></div>')


@app.get("/dashboard/<ticker>")
def dashboard(ticker):
    t = _valid_ticker(ticker)
    path = ROOT / "dashboards" / f"{t}.html"
    if not path.exists():
        abort(404, f"No dashboard for {t} yet — run the pipeline first.")
    panel = _position_panel(t)
    if not panel:
        return send_file(path)
    html = path.read_text(encoding="utf-8")
    anchor = '<div class="tiles">'
    i = html.find(anchor)
    if i == -1:
        return send_file(path)
    # streamed as three pieces rather than one concatenation: the dashboards
    # run to ~150KB and joining them would copy the lot twice per request
    return Response([html[:i], panel, html[i:]], mimetype="text/html")


@app.get("/ticker/<ticker>")
def ticker_page(ticker):
    t = _valid_ticker(ticker)
    return render_template("ticker.html.j2", s=_ticker_status(t))


@app.post("/run/<ticker>")
def run_step(ticker):
    t = _valid_ticker(ticker)
    step = request.form.get("step", "fetch")
    if step not in STEPS:
        abort(400, "unknown step")
    if step in COMPANY_ONLY_STEPS and (ROOT / "data" / t / "fund.json").exists():
        abort(400, f"{t} is a fund — it has no revenue or earnings to forecast. "
                   f"Use Fetch everything, which takes the fund path (holdings, "
                   f"cost and exposure).")
    if t in _busy_tickers():
        abort(409, f"A job for {t} is already running.")

    extra: list[str] = []
    ir_xlsx = (request.form.get("ir_xlsx") or "").strip()
    if ir_xlsx and step == "fetch":
        if not ir_xlsx.lower().startswith(("http://", "https://")):
            abort(400, "IR workbook must be an http(s) URL to an .xlsx file")
        extra += ["--ir-xlsx", ir_xlsx]

    doc_flag = "--doc" if step == "fetch" else "--add"
    for line in (request.form.get("doc_urls") or "").splitlines():
        line = line.strip()
        if line and step in ("fetch", "sources"):
            extra += [doc_flag, line]
    for f in request.files.getlist("doc_files"):
        if not f or not f.filename:
            continue
        up_dir = ROOT / "data" / t / "uploads"
        up_dir.mkdir(parents=True, exist_ok=True)
        dest = up_dir / secure_filename(f.filename)
        f.save(dest)
        if step in ("fetch", "sources"):
            extra += [doc_flag, str(dest)]

    log_dir = ROOT / "data" / t / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f'{step}_{datetime.now():%Y%m%d_%H%M%S}.log'
    # the child inherits its own descriptor, so the parent's copy is closed as
    # soon as Popen returns — otherwise every run leaks a handle for the life
    # of the server, and Windows keeps the log file held open
    with open(log_path, "w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(STEPS[step](t, extra), cwd=ROOT,
                                stdout=log_fh, stderr=subprocess.STDOUT)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"proc": proc, "log": log_path, "ticker": t, "step": step,
                    "started": datetime.now().isoformat(timespec="seconds"),
                    "at": time.time()}
    _reap_jobs()
    return jsonify({"job_id": job_id})


@app.post("/run-bulk")
def run_bulk():
    """Analyze several tickers in one background job."""
    raw = request.form.getlist("tickers") or []
    if len(raw) == 1 and "," in raw[0]:
        raw = raw[0].split(",")
    tickers = []
    for t in raw:
        t = (t or "").strip().upper()
        if t and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t) and t not in tickers:
            tickers.append(t)
    if not tickers:
        abort(400, "no valid tickers selected")

    busy = _busy_tickers()
    if busy:
        abort(409, f"A job is already running for {', '.join(sorted(busy))}.")

    cmd = [VENV_PY, str(PIPELINE / "bulk.py")] + tickers
    if request.form.get("with_sentiment"):
        cmd.append("--with-sentiment")

    log_dir = ROOT / "data" / "_bulk" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f'bulk_{datetime.now():%Y%m%d_%H%M%S}.log'
    with open(log_path, "w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log_fh,
                                stderr=subprocess.STDOUT)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"proc": proc, "log": log_path,
                    "ticker": ", ".join(tickers), "step": "bulk analyze",
                    "started": datetime.now().isoformat(timespec="seconds"),
                    "at": time.time()}
    _reap_jobs()
    return jsonify({"job_id": job_id, "tickers": tickers})


@app.get("/job/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    rc = job["proc"].poll()
    try:
        tail = job["log"].read_text(encoding="utf-8", errors="replace")[-6000:]
    except OSError:
        tail = ""
    return jsonify({"status": "running" if rc is None
                    else ("done" if rc == 0 else "failed"),
                    "returncode": rc, "step": job["step"], "ticker": job["ticker"],
                    "log_tail": tail})


@app.post("/new")
def new_ticker():
    t = _valid_ticker(request.form.get("ticker", ""))
    (ROOT / "data" / t).mkdir(parents=True, exist_ok=True)
    return redirect(url_for("ticker_page", ticker=t))


if __name__ == "__main__":
    print(f"Stock Analysis UI -> http://127.0.0.1:5000  (python: {VENV_PY})")
    app.run(host="127.0.0.1", port=5000, debug=False)
