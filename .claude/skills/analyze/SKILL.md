---
name: analyze
description: Full stock analysis for one or more US-listed tickers — fetches SEC EDGAR fundamentals (income, balance sheet, cash flow), filings & press releases, social sentiment, Yahoo data, researches earnings context on the web, writes a narrative, and renders a 12-quarter outlook dashboard per ticker. Usage: /analyze TICKER [TICKER ...] [ir-workbook-xlsx-url] [doc-url-or-path ...]
---

# /analyze <TICKER...> [ir-xlsx-url] [doc-url-or-path ...]

Produce `dashboards/<TICKER>.html` for each ticker — a self-contained dashboard
with 12 reported + 12 forecast quarters, full fundamentals (balance sheet, cash
generation vs capex, capital returns), social sentiment, and a Claude-written
narrative.

Funds and ETFs take a different pipeline (no fundamentals, no forecast) but end
the same way: **every instrument gets its findings written to its own
dashboard**, never left in the chat reply alone.

Python interpreter (shared venv, always use this exact path):
`python`

All commands run from the project root (`PrivatePlan` directory).
A local UI also exists (`python server.py` → http://127.0.0.1:5000) for the
mechanical steps; /analyze is the full analysis.

## Step 0 — Parse the arguments

Leading bare ticker-shaped tokens are tickers: 1–5 letters, optionally with a
`.X` / `-X` class suffix (`MSFT`, `BRK.B`, `BRK-B`). The first token that is a
URL or a filesystem path ends the ticker list; everything from there on is the
IR workbook and documents.

- `/analyze MSFT` — one ticker.
- `/analyze MSFT AAPL NVDA` — three tickers.
- `/analyze MSFT https://…/workbook.xlsx ./notes.pdf` — one ticker, with a
  workbook and a document.

**Attached documents apply to a single ticker only.** If the user passes both
several tickers and a workbook/doc, there is no way to tell which company the
document belongs to — ask which ticker it goes with rather than guessing.

If a token looks like a ticker but is not one (a typo, or a name like `APPLE`),
EDGAR lookup fails in step 2; report that ticker as failed and keep going with
the rest rather than aborting the whole run.

State the resolved ticker list back to the user before starting a multi-ticker
run, so a misparsed argument is caught before minutes of fetching.

## Multi-ticker runs

With more than one ticker, do the mechanical fetch for all of them in one batch,
then do the analysis **one ticker at a time, fully finishing each** before
starting the next:

```
python pipeline\bulk.py <TICKER> <TICKER> ... --with-sentiment
```

`bulk.py` runs fetch + render per ticker in a single job, keeps going when one
ticker fails, and prints a `succeeded / failed` summary at the end. It covers
steps 1–2 for every ticker. Then for each ticker in turn, run steps 2b→6 (read
primary documents, research, override assumptions if warranted, write the
narrative, re-render).

Do not batch the research or the narratives. The per-company judgment is the
point of this skill: reading that company's press release, reconciling its
guidance against its model, and writing a narrative grounded in its numbers.
Interleaving several companies is how numbers get attributed to the wrong one.

Skip step 1 (IR workbook discovery) in multi-ticker mode — segment detail comes
from the parsed press release via step 4b instead. Guard the budget: with more
than ~4 tickers, hold step 3 to the 3–4 searches per ticker it already asks for,
and say so in the final report rather than silently doing less research.

Report at the end as a table: ticker, dashboard path, one-line takeaway, plus
any tickers that failed and why.

## Step 1 — Discover the IR workbook (if no URL was given)

Web-search `"<company name> investor relations earnings press release"` and look
for the company's latest quarterly results page. Many companies publish a
financial-statements workbook (.xlsx) there — if you find a direct .xlsx URL,
use it in step 2. If none exists, proceed without it (segment detail can still
come from the parsed press release, step 4b).

## Step 2 — Fetch data

Single ticker (use `bulk.py` instead when there are several — see above):

```
python pipeline\fetch.py <TICKER> [--ir-xlsx <url>] [--doc <url-or-path> ...]
```

Pass any user-supplied documents (HTML, PDF, CSV, XLSX — URLs or local paths)
with repeated `--doc` flags. A `--doc` URL that resolves to an HTML page is
treated as a website and crawled recursively (same host, bounded depth/pages),
so linked pages and PDFs/workbooks deeper in the site land in `docs/*.json`
as `user_doc_crawled` entries. This writes into `data/<TICKER>/`:

- `financials.json` — EDGAR income statement + **balance sheet** (assets,
  liabilities, equity, cash, ST/LT investments, LT debt, inventory, AR,
  goodwill), **cost of revenue**, full cash flow (OCF, capex, D&A, SBC,
  dividends, buybacks) with derived `fcf`, `fcf_margin`, `capex_to_ocf`,
  `net_cash`, and `total_liquidity` (cash + all marketable securities, the
  figure companies quote in their releases)
- `market.json`, `estimates.json` — Yahoo
- `filings.json` + `docs/*.json` + `sources.json` — parsed SEC filings (latest
  10-K, two 10-Qs, earnings 8-K press-release exhibits) and user documents
- `sentiment.json` — Reddit/StockTwits VADER-scored sentiment (may be partial;
  Reddit is often blocked unauthenticated — that's fine, see step 3)
- `ir_workbook.json` + `segments.json` — when an IR workbook was given
- `forecast.json` — the 12-quarter model

Sources/sentiment failures are non-fatal — check the output and continue.
If EDGAR fails the ticker may be foreign (ADR without XBRL) — tell the user
what's available instead.

### ETFs and funds

`fetch.py` detects an ETF / mutual fund / closed-end fund (via Yahoo
`quoteType`, or when EDGAR reports the symbol is not an operating-company
filer) and switches to the fund pipeline by itself: it writes
`data/<SYMBOL>/fund.json` (cost, category, top holdings, sector and
asset-class mix), `market.json`, `sentiment.json`, and renders the fund
dashboard. There are no SEC fundamentals, analyst estimates, filings or
12-quarter forecast for a fund, so **skip steps 4, 4b and the forecast**.

For a fund, write the analysis around what it costs, what is actually inside
it, and how concentrated that is — not margins and FCF.

**A fund gets a written narrative on its dashboard exactly like a company
does.** Do not stop at a reply in chat: the findings belong in
`data/<SYMBOL>/narrative.json`, which `fund_render.py` reads. The fund schema
is a different shape from the company one (see step 5b), and after writing it
re-render with `pipeline/fund_render.py <SYMBOL>` — the render that `fetch.py`
already did ran before the narrative existed.

## Step 2b — Read the primary documents

Skim `data/<TICKER>/sources.json` and open the parsed press release and
CFO-commentary docs in `data/<TICKER>/docs/` (press releases first, then the
latest 10-Q). **Primary documents beat web-search results for guidance
numbers** — pull guided revenue/margin/capex, segment tables, and any buyback
authorization directly from them.

## Step 3 — Research context (web + social)

Search for (batch these efficiently, ~3-4 searches total):
1. Earnings-call takeaways not in the press release (management tone, Q&A,
   guidance color: growth outlook, margin commentary, capex plans).
2. 2–3 pieces of recent public analyst commentary / price-target moves.
3. Anything material to the forward path (product cycles, regulation, M&A).

Social: run the `last30days` skill (or WebSearch) for `$<TICKER>` chatter on
X/Twitter and Reddit — the coded pipeline covers StockTwits but X has no free
API and Reddit often blocks scripted access. Reconcile what you find with
`sentiment.json` and write a 2–4 sentence `social_sentiment` field into the
narrative (step 5).

Read `data/<TICKER>/forecast.json` — especially `assumptions` — and compare
with found guidance.

## Step 4 — Override assumptions only if warranted

If management guidance clearly contradicts the model (e.g. guided growth far
from `growth_by_fy`, announced margin inflection), write
`data/<TICKER>/assumptions_override.json`:

```json
{"long_run_growth": 0.12, "extra_year_growth": {"2029": 0.14},
 "margin_drift_per_year": 0.01, "notes": "why"}
```

then re-run: `& "...python.exe" pipeline\forecast.py <TICKER>`.
Skip this step when the model is already consistent with guidance.

### Pre-revenue companies

When a filer has never reported revenue, `forecast.json` carries
`"pre_revenue": true` and models spend rather than growth: `operating_loss` and
`burn` per quarter, EPS anchored to consensus, plus `quarterly_burn_recent`,
`liquidity_last_reported` and `runway_quarters_at_projected_burn` in
`assumptions`. Revenue is deliberately carried flat, not grown — analyst revenue
consensus for a pre-commercial issuer is a rounding artefact (most contributors
carry zero), so **do not present it as a forecast**.

The override keys differ too: `guided_annual_burn` (per fiscal year, and the one
that matters most — set it from guided operating cash use plus capex),
`opex_growth_per_year`, `opex_growth_decay`, `quarterly_dilution`.

Write the narrative around runway, funding and milestones instead of margins and
FCF, and say plainly which forecast years are guided and which are modeled.

## Step 4b — Segments (when no IR workbook)

If `segments.json` doesn't exist but segment revenue appears in the parsed
press release / 10-Q tables (`docs/*.json`), write
`data/<TICKER>/segments.json` yourself:

```json
{"source": "manual", "source_url": "https://... (where the numbers came from)",
 "unit": "USD", "as_of": "2026-07-31",
 "segments": [
   {"name": "Data Center", "quarters": {"FY26 Q4": 35600000000, "FY27 Q1": 39100000000}}
 ]}
```

Quarter keys must use the pipeline's fiscal labels (`FY{yy} Q{q}`, matching
`financials.json`). Values in dollars. 2–6 quarters of history is enough;
the renderer folds >3 segments into top-2 + Other.

## Step 5 — Write the narrative (operating company)

Every instrument you analyse ends with its findings written into
`data/<SYMBOL>/narrative.json` and rendered onto its dashboard — companies
here, funds and ETFs in step 5b. An analysis that exists only in the chat
reply is not finished: the dashboard is the artifact that survives the session.

Write `data/<TICKER>/narrative.json` (plain text values, no markdown):

```json
{
  "headline": "one-line story of where this company is",
  "executive_summary": "3-5 sentences: quarter results, trajectory, what the 12-quarter path assumes",
  "quarter_recap": "what was reported vs expectations, segment highlights, guidance given",
  "thesis": {"bull": ["3-5 bullets"], "bear": ["3-5 bullets"]},
  "segment_story": [{"name": "Segment", "story": "1-3 sentences"}],
  "forecast_rationale": "where consensus ends and modeling begins; why the assumptions are credible; key sensitivities",
  "social_sentiment": "2-4 sentences reconciling coded Reddit/StockTwits sentiment with X/web chatter found in step 3",
  "risks": ["4-6 bullets"],
  "catalysts": ["4-6 bullets"],
  "sources": [{"label": "…", "url": "…"}]
}
```

Ground every claim in fetched data or found sources. Numbers must match
`financials.json` / `forecast.json`. Use the new fundamentals explicitly:
FCF trajectory and whether cash generation keeps up with capex
(`capex_to_ocf`), balance-sheet strength (`net_cash`), and the pace of
buybacks/dividends.

## Step 5b — Write the narrative for a fund/ETF

Same obligation, different schema. Write `data/<SYMBOL>/narrative.json`
(plain text values, no markdown) — every key optional except the first two,
and each one renders as its own card on the fund dashboard:

```json
{
  "headline": "one line on what this fund is for and whether it earns its keep",
  "executive_summary": "3-5 sentences: what it holds, what it costs, how it has done, who it suits",
  "what_it_is": "what the wrapper actually buys — index or active, the rule it follows, how it differs from the obvious cheaper alternative",
  "cost_verdict": "the expense ratio against the category and against an index equivalent, in dollars per $100k, and what the extra buys",
  "thesis": {"bull": ["3-5 reasons to hold"], "bear": ["3-5 reasons to think again"]},
  "concentration": "what it duplicates — overlap with a broad index fund or with directly held names, and how much of the fund the disclosed holdings actually cover",
  "risks": ["4-6 bullets"],
  "catalysts": ["4-6 bullets: what would change the picture"],
  "social_sentiment": "2-4 sentences reconciling sentiment.json with X/web chatter from step 3",
  "sources": [{"label": "…", "url": "…"}]
}
```

Ground it in `fund.json`: expense ratio vs `category_expense_ratio`, top
holdings and their combined weight, sector and asset-class mix, `ytd_return` /
`return_3y` / `return_5y`, `beta_3y`. Say plainly when the disclosed holdings
cover only part of the fund — that is a limit on the analysis, not a detail.

## Step 6 — Render and report

Company:

```
python pipeline\render.py <TICKER>
```

Fund/ETF:

```
python pipeline\fund_render.py <SYMBOL>
```

Re-render even when `bulk.py` or `fetch.py` already did: that render ran before
`narrative.json` and any `assumptions_override.json` existed, so the dashboard
is missing the headline, thesis, risks and catalysts until this second pass.
The renderer says so — a fund render logs "no narrative.json yet" when it wrote
a dashboard with data but no findings. Treat that line in your own output as a
step you have not finished.

Single ticker: report the dashboard path and a short summary of the analysis.

Multiple tickers: **loop back to step 2b for the next ticker** and only report
once every ticker is done — a table of ticker, dashboard path and a one-line
takeaway, then the failures with reasons, then anything you deliberately
shortened (e.g. reduced research depth on a long run). Do not stop halfway and
report partial results as if complete.
