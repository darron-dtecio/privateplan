"""Run fetch + render for several tickers in one job.

The web UI spawns this as a single subprocess so one log and one job id cover
the whole batch. A failure on one ticker never stops the rest — the summary at
the end says exactly which ones did not make it.

Usage:
    python pipeline/bulk.py MSFT AAPL NVDA [--with-sentiment] [--filings N]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "pipeline"

DEFAULT_WORKERS = 4


def _env(workers: int) -> dict:
    """Child environment telling the pipeline how many of it are running.

    SEC's ceiling is 10 requests/second for the whole client, not per process,
    so the per-request delays in sources.py scale by this number. Parallelism
    still pays: it overlaps round-trip latency, Yahoo and Reddit calls, HTML
    parsing and interpreter startup, none of which SEC's limit governs.
    """
    return {**os.environ, "PIPELINE_WORKERS": str(max(1, workers))}


def run_fund(symbol: str, workers: int = 1) -> tuple[bool, str]:
    # narrate.py sits between the data and the page for the same reason it does
    # in fetch.py: render reads narrative.json, so it has to exist first.
    for script in ("fund.py", "narrate.py", "fund_render.py"):
        r = subprocess.run([sys.executable, str(PIPELINE / script), symbol],
                           cwd=ROOT, capture_output=True, text=True, timeout=600,
                           env=_env(workers))
        if r.returncode != 0:
            return False, f"{script} failed: " + (r.stdout + r.stderr).strip()[-300:]
    return True, ""


def is_fund(symbol: str) -> bool:
    """Ask the quote feed what the symbol is; trust a local file only as a hint.

    fund.json on disk used to be the whole answer, which made any earlier
    misfiling permanent: a ticker that landed in the fund pipeline because SEC
    was unreachable would be routed there forever, never retrying EDGAR. The
    classifier is the authority; the file only breaks a tie when Yahoo is silent.
    """
    sys.path.insert(0, str(PIPELINE))
    import fund
    kind = fund.classify(symbol)
    if kind != "unknown":
        return kind == "fund"
    return (ROOT / "data" / symbol / "fund.json").exists()


def run_one(ticker: str, with_sentiment: bool, filings: int,
            workers: int = 1) -> tuple[bool, str]:
    # A fund has no SEC fundamentals, so it takes the fund path instead. Known
    # funds are routed straight away; anything else tries the company pipeline
    # first and falls back, so a mixed selection just works.
    if is_fund(ticker):
        ok, err = run_fund(ticker, workers)
        return ok, err if ok else f"fund path: {err}"

    cmd = [sys.executable, str(PIPELINE / "fetch.py"), ticker,
           "--filings", str(filings)]
    if not with_sentiment:
        cmd.append("--skip-sentiment")
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1800,
                       env=_env(workers))
    if r.returncode != 0:
        # A failed fetch says nothing about what the symbol is. This used to
        # retry it as a fund, which quietly relabelled equities whenever EDGAR
        # was blocked or down — and left them with no fundamentals or forecast.
        # fetch.py already routes genuine funds itself.
        return False, "fetch failed: " + (r.stdout + r.stderr).strip()[-300:]
    # fetch.py detects funds itself and renders them; nothing left to do here.
    if (ROOT / "data" / ticker / "fund.json").exists():
        return True, "analysed as a fund"
    r2 = subprocess.run([sys.executable, str(PIPELINE / "render.py"), ticker],
                        cwd=ROOT, capture_output=True, text=True, timeout=600,
                        env=_env(workers))
    if r2.returncode != 0:
        return False, "render failed: " + (r2.stdout + r2.stderr).strip()[-300:]
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--with-sentiment", action="store_true")
    ap.add_argument("--filings", type=int, default=8)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"tickers to process at once (default {DEFAULT_WORKERS}; "
                         "1 restores the old strictly-sequential run)")
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.tickers if t.strip()]
    workers = max(1, min(args.workers, len(tickers)))

    # Say once, before any work, that the company half cannot succeed — rather
    # than letting every equity in the batch fail one at a time on a 403.
    sys.path.insert(0, str(PIPELINE))
    import contact
    if not contact.contact():
        print("[bulk] PRIVATEPLAN_CONTACT is not set. SEC EDGAR rejects the "
              "placeholder User-Agent with a 403, so no company in this batch "
              "will get fundamentals or a forecast. Funds are unaffected.",
              flush=True)
        print('[bulk]   PowerShell:  $env:PRIVATEPLAN_CONTACT = "you@example.com"',
              flush=True)
        print("[bulk]   bash/zsh:    export PRIVATEPLAN_CONTACT='you@example.com'",
              flush=True)
    print(f"[bulk] {len(tickers)} ticker(s) on {workers} worker(s): "
          f"{', '.join(tickers)}", flush=True)
    ok, failed = [], []
    started = time.time()

    def work(t: str) -> tuple[str, bool, str, float]:
        t0 = time.time()
        try:
            good, err = run_one(t, args.with_sentiment, args.filings, workers)
        except Exception as e:            # noqa: BLE001 — one ticker, not the batch
            # A timeout or a crash used to take the whole loop down with it.
            # The docstring's promise is that one bad ticker never stops the
            # rest, so it is caught here and reported as that ticker's failure.
            good, err = False, f"{type(e).__name__}: {e}"
        return t, good, err, time.time() - t0

    # Results are reported as they land rather than in submission order — with
    # tickers overlapping there is no meaningful order to preserve, and a
    # long-running one should not hold back the log for the rest.
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, t) for t in tickers]
        for fut in as_completed(futures):
            t, good, err, secs = fut.result()
            done += 1
            if good:
                ok.append(t)
                note = f" — {err}" if err else ""
                print(f"[bulk] ({done}/{len(tickers)}) {t}: OK ({secs:.0f}s){note}",
                      flush=True)
            else:
                failed.append(t)
                print(f"[bulk] ({done}/{len(tickers)}) {t}: FAILED — {err}",
                      flush=True)

    print(f"[bulk] done in {time.time() - started:.0f}s — "
          f"{len(ok)} succeeded, {len(failed)} failed", flush=True)
    if failed:
        print(f"[bulk] failed: {', '.join(failed)}", flush=True)
    print("[bulk] every dashboard carries a rule-based narrative written from the "
          "fetched numbers. /analyze <TICKER> in Claude Code replaces it with one "
          "that has read the filings and researched the quarter.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
