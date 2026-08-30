"""Social sentiment for a ticker: Reddit + StockTwits, scored with VADER.

Free public endpoints only (no API keys). Both sources degrade gracefully —
partial data with an "errors" record beats failing the pipeline. X/Twitter has
no free API; it is covered agent-side during /analyze via web research.

Usage:
    python pipeline/sentiment.py NVDA [--days 30]

Writes data/<T>/sentiment.json.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import requests

import contact

ROOT = Path(__file__).resolve().parent.parent

USER_AGENT = contact.user_agent("equity research")
HEADERS = {"User-Agent": USER_AGENT}

SUBREDDITS = "stocks+wallstreetbets+investing+StockMarket"
REDDIT_SEARCH = f"https://www.reddit.com/r/{SUBREDDITS}/search/.json"
STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"

POS_THRESHOLD, NEG_THRESHOLD = 0.05, -0.05


def fetch_reddit(ticker: str, company: str | None, days: int, errors: list) -> list[dict]:
    """Search finance subreddits via the public .json endpoint (no key)."""
    query = f'"${ticker}" OR "{ticker}"'
    if company:
        # First word of the registrant name cuts false positives on short tickers.
        first = company.split()[0].rstrip(",.")
        if len(first) > 3 and first.upper() != ticker:
            query += f' OR "{first}"'
    posts, after = [], None
    cutoff = (datetime.now() - timedelta(days=days)).timestamp()
    for page in range(3):
        params = {"q": query, "restrict_sr": "on", "sort": "new",
                  "t": "month", "limit": 100, "raw_json": 1}
        if after:
            params["after"] = after
        try:
            resp = requests.get(REDDIT_SEARCH, params=params, headers=HEADERS, timeout=30)
            if resp.status_code == 429:
                errors.append("Reddit: rate limited (429) — keeping partial results")
                break
            resp.raise_for_status()
            data = resp.json().get("data", {})
        except Exception as e:
            errors.append(f"Reddit page {page + 1}: {e}")
            break
        children = data.get("children", [])
        for c in children:
            d = c.get("data", {})
            if d.get("created_utc", 0) < cutoff:
                continue
            posts.append({
                "source": "reddit",
                "title": d.get("title", ""),
                "text": (d.get("selftext") or "")[:2000],
                "url": "https://www.reddit.com" + d.get("permalink", ""),
                "score": d.get("score", 0),
                "num_comments": d.get("num_comments", 0),
                "subreddit": d.get("subreddit", ""),
                "created": datetime.fromtimestamp(d.get("created_utc", 0))
                           .isoformat(timespec="seconds"),
            })
        after = data.get("after")
        if not after or not children:
            break
        time.sleep(2)
    return posts


def fetch_stocktwits(ticker: str, errors: list) -> list[dict]:
    """Latest ~30 messages from the free StockTwits symbol stream."""
    try:
        resp = requests.get(STOCKTWITS_URL.format(symbol=ticker), headers=HEADERS, timeout=30)
        resp.raise_for_status()
        messages = resp.json().get("messages", [])
    except Exception as e:
        errors.append(f"StockTwits: {e}")
        return []
    posts = []
    for m in messages:
        native = (((m.get("entities") or {}).get("sentiment") or {}) or {}).get("basic")
        posts.append({
            "source": "stocktwits",
            "title": (m.get("body") or "")[:280],
            "text": "",
            "url": f'https://stocktwits.com/symbol/{ticker}',
            "score": ((m.get("likes") or {}).get("total") or 0),
            "native_sentiment": native,  # Bullish / Bearish / None
            "created": (m.get("created_at") or "").replace("Z", ""),
        })
    return posts


def score(posts: list[dict]) -> None:
    """Add a VADER compound score and pos/neu/neg label to each post in place."""
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    analyzer = SentimentIntensityAnalyzer()
    for p in posts:
        text = f'{p["title"]} {p.get("text", "")}'.strip()
        p["compound"] = round(analyzer.polarity_scores(text)["compound"], 4)
        # StockTwits authors label their own posts — trust that over the model.
        if p.get("native_sentiment") == "Bullish":
            p["compound"] = max(p["compound"], 0.5)
        elif p.get("native_sentiment") == "Bearish":
            p["compound"] = min(p["compound"], -0.5)
        p["label"] = ("positive" if p["compound"] >= POS_THRESHOLD
                      else "negative" if p["compound"] <= NEG_THRESHOLD else "neutral")


def label_for(avg: float) -> str:
    """Turn a mean compound score into the wording the dashboards use.

    A function rather than an inline expression because narrate.py labels each
    source with it too — two copies of these thresholds would drift, and the
    second one would be wrong quietly.
    """
    return ("strongly positive" if avg >= 0.35 else "mildly positive" if avg >= 0.08
            else "strongly negative" if avg <= -0.35
            else "mildly negative" if avg <= -0.08 else "mixed / neutral")


def summarize(posts: list[dict], days: int, errors: list) -> dict:
    n = len(posts)
    avg = sum(p["compound"] for p in posts) / n if n else 0.0
    counts = {"positive": 0, "neutral": 0, "negative": 0}
    for p in posts:
        counts[p["label"]] += 1
    label = label_for(avg)

    by_source: dict[str, dict] = {}
    for src in ("reddit", "stocktwits"):
        sp = [p for p in posts if p["source"] == src]
        if sp:
            entry = {"post_count": len(sp),
                     "avg_compound": round(sum(p["compound"] for p in sp) / len(sp), 3)}
            if src == "stocktwits":
                entry["bullish"] = sum(1 for p in sp if p.get("native_sentiment") == "Bullish")
                entry["bearish"] = sum(1 for p in sp if p.get("native_sentiment") == "Bearish")
            by_source[src] = entry

    daily: dict[str, list[float]] = defaultdict(list)
    for p in posts:
        if p.get("created"):
            daily[p["created"][:10]].append(p["compound"])
    trend = [{"date": d, "posts": len(vals),
              "avg_compound": round(sum(vals) / len(vals), 3)}
             for d, vals in sorted(daily.items())]

    top = sorted(posts, key=lambda p: -(p["score"] + p.get("num_comments", 0)))[:10]
    top_posts = [{"source": p["source"], "title": p["title"][:200], "url": p["url"],
                  "score": p["score"] + p.get("num_comments", 0),
                  "created": p["created"], "compound": p["compound"], "label": p["label"]}
                 for p in top]

    return {
        "window_days": days,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {"post_count": n, "avg_compound": round(avg, 3),
                    "pct_positive": round(counts["positive"] / n, 3) if n else 0,
                    "pct_neutral": round(counts["neutral"] / n, 3) if n else 0,
                    "pct_negative": round(counts["negative"] / n, 3) if n else 0,
                    "label": label if n else "no data"},
        "by_source": by_source,
        "trend": trend,
        "top_posts": top_posts,
        "errors": errors,
        "notes": ["X/Twitter has no free API — covered by agent web research "
                  "(last30days / WebSearch) at analysis time.",
                  "Sentiment scored with VADER; StockTwits native Bullish/Bearish "
                  "labels override the model score."]
                 + (["Reddit blocked unauthenticated access from this network — "
                     "Reddit chatter is covered agent-side (last30days) during /analyze."]
                    if any(e.startswith("Reddit") for e in errors) else []),
    }


def run(ticker: str, days: int = 30) -> dict:
    ticker = ticker.upper()
    out_dir = ROOT / "data" / ticker
    out_dir.mkdir(parents=True, exist_ok=True)

    company = None
    fin_path = out_dir / "financials.json"
    if fin_path.exists():
        try:
            company = json.loads(fin_path.read_text(encoding="utf-8")).get("company")
        except Exception:
            pass

    errors: list[str] = []
    posts = fetch_reddit(ticker, company, days, errors)
    posts += fetch_stocktwits(ticker, errors)
    score(posts)
    result = {"ticker": ticker, **summarize(posts, days, errors)}
    (out_dir / "sentiment.json").write_text(
        json.dumps(result, indent=1, default=str), encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    result = run(args.ticker, args.days)
    s = result["summary"]
    print(f'{s["post_count"]} posts — {s["label"]} (avg {s["avg_compound"]:+.2f}); '
          f'sources: {", ".join(f"{k}={v['post_count']}" for k, v in result["by_source"].items()) or "none"}')
    for e in result["errors"]:
        print(f"  ! {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
