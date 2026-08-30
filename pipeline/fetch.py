"""Fetch all raw data for a ticker into data/<TICKER>/.

Usage:
    python pipeline/fetch.py MSFT [--ir-xlsx URL] [--doc URL_OR_PATH ...]

Writes: financials.json (EDGAR), market.json + estimates.json (Yahoo),
ir_workbook.json + segments.json (optional IR workbook), filings.json +
docs/*.json + sources.json (SEC filings & user documents), sentiment.json
(Reddit/StockTwits), then runs the forecast model.

ETFs, mutual funds and closed-end funds are detected automatically and take the
fund path instead (fund.json + fund dashboard) — they have no SEC company facts,
revenue or EPS for the company pipeline to work on.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import edgar
import yahoo

ROOT = Path(__file__).resolve().parent.parent


def save(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    print(f"  wrote {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")


def fetch_fund(ticker: str, out_dir: Path, skip_sentiment: bool) -> int:
    """Fund path: cost, holdings and exposure instead of SEC fundamentals.

    An ETF or mutual fund has no revenue, EPS or SEC company facts, so the
    forecast model, analyst estimates and filings steps have nothing to work on
    and are skipped outright rather than left to fail one by one.
    """
    import fund
    import fund_render
    import yahoo

    failures = []
    print(f"[1/3] Fund profile, cost & holdings for {ticker} ...")
    try:
        data = fund.write(ticker)
        er = data.get("expense_ratio")
        cost = f"{er * 100:.3f}%" if er is not None else "unknown"
        print(f'  wrote data/{ticker}/fund.json — '
              f'{data.get("category") or "?"}, expense {cost}, '
              f'{len(data.get("top_holdings") or [])} holdings')
    except Exception as e:
        failures.append(f"Fund data: {e}")
        traceback.print_exc()

    print("[2/3] Yahoo market data ...")
    try:
        save(out_dir / "market.json", yahoo.fetch_market(ticker))
    except Exception as e:
        failures.append(f"Yahoo market: {e}")

    if skip_sentiment:
        print("[3/3] sentiment skipped")
    else:
        print("[3/3] Social sentiment (Reddit/StockTwits) ...")
        try:
            import sentiment
            result = sentiment.run(ticker)
            print(f'  {result["summary"]["post_count"]} posts, '
                  f'label: {result["summary"]["label"]}')
            for err in result.get("errors", []):
                print(f"  ! {err}")
        except Exception as e:
            failures.append(f"Sentiment: {e}")

    if (out_dir / "fund.json").exists():
        print("Rendering fund dashboard ...")
        try:
            fund_render.render(ticker)
        except Exception as e:
            failures.append(f"Fund render: {e}")
            traceback.print_exc()

    if failures:
        print("\nCompleted with issues:")
        for f in failures:
            print(f"  - {f}")
    else:
        print(f"\n{ticker} fetched as a fund (no SEC fundamentals or forecast).")
    return 0 if (out_dir / "fund.json").exists() else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--ir-xlsx", help="URL of a company IR financial workbook (.xlsx)")
    ap.add_argument("--doc", action="append", default=[],
                    help="extra document URL or local path to ingest (repeatable)")
    ap.add_argument("--filings", type=int, default=8,
                    help="how many recent SEC filings to index")
    ap.add_argument("--skip-forecast", action="store_true")
    ap.add_argument("--skip-sources", action="store_true")
    ap.add_argument("--skip-sentiment", action="store_true")
    args = ap.parse_args()

    ticker = args.ticker.upper()
    out_dir = ROOT / "data" / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = []

    ir_xlsx = (args.ir_xlsx or "").strip()
    if ir_xlsx and not ir_xlsx.lower().startswith(("http://", "https://")):
        print(f"! ignoring --ir-xlsx {ir_xlsx!r}: not an http(s) URL to an .xlsx workbook")
        ir_xlsx = ""

    # ETFs and funds have no SEC company facts, so they take a different path
    # entirely. Detect up front when Yahoo knows; otherwise start down the
    # company path and switch as soon as EDGAR says the symbol isn't a filer.
    import fund
    if fund.classify(ticker) == "fund":
        print(f"{ticker} is a fund — using the fund pipeline "
              f"(no SEC fundamentals, estimates or forecast).")
        return fetch_fund(ticker, out_dir, args.skip_sentiment)

    print(f"[1/6] SEC EDGAR financials for {ticker} ...")
    try:
        save(out_dir / "financials.json", edgar.fetch_financials(ticker))
    except edgar.TickerNotFound as e:
        print(f"  ! {e}")
        print(f"{ticker} is not an SEC operating-company filer — "
              f"retrying as a fund.")
        return fetch_fund(ticker, out_dir, args.skip_sentiment)
    except Exception as e:
        failures.append(f"EDGAR: {e}")
        traceback.print_exc()

    print(f"[2/6] Yahoo market data ...")
    try:
        save(out_dir / "market.json", yahoo.fetch_market(ticker))
    except Exception as e:
        failures.append(f"Yahoo market: {e}")

    print(f"[3/6] Yahoo analyst estimates ...")
    try:
        save(out_dir / "estimates.json", yahoo.fetch_estimates(ticker))
    except Exception as e:
        failures.append(f"Yahoo estimates: {e}")

    if ir_xlsx:
        print(f"[4/6] IR workbook ingest ...")
        try:
            import ir_ingest
            ir = ir_ingest.ingest(ir_xlsx)
            save(out_dir / "ir_workbook.json", ir)
            segments = ir_ingest.to_segments(ir)
            if segments:
                save(out_dir / "segments.json", segments)
        except Exception as e:
            failures.append(f"IR workbook: {e}")
            traceback.print_exc()
    else:
        print("[4/6] no IR workbook URL provided, skipping")

    if args.skip_sources:
        print("[5/6] sources skipped")
    else:
        print(f"[5/6] SEC filings & documents ...")
        try:
            import sources
            result = sources.run(ticker, tuple(args.doc), args.filings)
            print(f'  parsed {len(result["documents"])} documents into data/{ticker}/docs/')
            for err in result["errors"]:
                print(f"  ! {err}")
        except Exception as e:
            failures.append(f"Sources: {e}")
            traceback.print_exc()

    if args.skip_sentiment:
        print("[6/6] sentiment skipped")
    else:
        print(f"[6/6] Social sentiment (Reddit/StockTwits) ...")
        try:
            import sentiment
            result = sentiment.run(ticker)
            print(f'  {result["summary"]["post_count"]} posts, '
                  f'label: {result["summary"]["label"]}')
            for err in result.get("errors", []):
                print(f"  ! {err}")
        except Exception as e:
            failures.append(f"Sentiment: {e}")
            traceback.print_exc()

    if not args.skip_forecast and (out_dir / "financials.json").exists():
        print("Running forecast model ...")
        try:
            import forecast
            forecast.run(ticker)
        except Exception as e:
            failures.append(f"Forecast: {e}")
            traceback.print_exc()

    if failures:
        print("\nCompleted with issues:")
        for f in failures:
            print(f"  - {f}")
    else:
        print("\nAll data fetched successfully.")
    return 0 if (out_dir / "financials.json").exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
