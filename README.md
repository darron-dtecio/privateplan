# PrivatePlan

A retirement and portfolio planner that reads your actual financial documents and
never sends them anywhere.

It runs on your machine, as a local web app. There is no account to create, no
server to upload statements to, and no company holding a copy of your balances.
Everything it learns about you lives in one gitignored directory on your own disk.

```
python server.py     →     http://127.0.0.1:5000
```

---

## Your data never leaves your machine

This is the whole point, so it is worth being precise about how it is enforced
rather than promised:

- **One home for personal data.** Everything is under `finance_data/`, which is
  gitignored. Delete that directory and the app knows nothing about you again.
- **Redaction happens before storage, not before display.** Every parsed page,
  table and grid passes through `finance/redact.py` *before* anything is written
  to disk. SSNs, names, addresses, phone numbers, emails, dates of birth and
  employee/account/loan numbers never reach the extracted JSON at all. Financial
  values are deliberately kept — they are what the plan is made of, and a balance
  with no name attached identifies nobody.
- **Diagnostics carry no dollar values.** The pipeline logs field names, row
  counts and confidence scores. `finance/selftest.py` includes a lint that fails
  if that changes, so logs stay safe to paste into a bug report.
- **The only outbound traffic is public market data.** Share prices (Yahoo) and
  SEC EDGAR filings, keyed by ticker. Nothing about you is in those requests.
  Skip the price and portfolio steps and the app is fully offline.

See [docs/PRIVACY.md](docs/PRIVACY.md) for the details, including what to check
before sharing a dashboard.

---

## Two halves, one workspace

**A retirement planner.** Feed it pay stubs, brokerage and retirement statements,
a mortgage statement, a Social Security statement, a checking-account export and
an equity vesting schedule. It builds one profile from them and answers the
questions your individual accounts cannot:

- What you actually spend, once transfers to savings and brokerage are separated
  out from consumption — including money spent straight from an HSA, which never
  touches your bank account.
- What your plan looks like year by year to age 95, across three tax buckets,
  with RMDs, Medicare, IRMAA surcharges and a mortgage that ends on a real date.
- In what fraction of ten thousand simulated markets it survives.
- What Social Security claiming at 62 vs 67 vs 70 is worth to you specifically.
- What your advisory fee costs in dollars, what it must beat to leave you level,
  and how long a track record would be needed to prove it is earning that.
- What your funds actually hold once you look through them — and how much of your
  "diversified" equity is the same thirty companies.
- What unvested equity adds, which is one of the few genuinely knowable inputs in
  a financial plan and is usually left out.

**A stock analyzer.** Set `PRIVATEPLAN_CONTACT` to an email address first — SEC
rejects requests without one and the company pipeline cannot run (see
[docs/SETUP.md](docs/SETUP.md)). A SEC/EDGAR-backed pipeline that pulls fundamentals,
filings and estimates for any US-listed ticker and renders a 12-quarter outlook
dashboard. It is here because your portfolio *is* part of your plan: the
portfolio step runs it across everything you hold and weights the result by what
you actually own.

Each dashboard comes with its findings already written. `pipeline/narrate.py`
composes the headline, the quarter recap, a bull and bear case, the forecast
rationale, risks and catalysts from the fetched numbers alone — no model, no API
key, no network beyond the data already pulled. Every sentence is arithmetic on
a value in `financials.json`, `forecast.json`, `estimates.json` or `fund.json`,
and the page says so.

What it cannot do is read the earnings call, weigh management's tone, or judge
whether guidance contradicts the model — that needs a human or an LLM, and the
`/analyze` skill in Claude Code is the version that does it. Running it replaces
the computed narrative with a researched one. Neither is investment advice.

---

## Quickstart

```bash
git clone <your-fork-url> privateplan
cd privateplan

# Windows
./setup.ps1
# macOS / Linux
./setup.sh

python server.py
```

Open <http://127.0.0.1:5000/finance>, expand **Sample data & reset**, and click
**Load both**. Then **Extract documents** → **Analyze** → **Render dashboard**.

That runs the whole pipeline over a fictional household — the Chens, mid-forties,
retiring at 62 — so you can see what a finished plan looks like before deciding
whether to feed it your own documents. When you are ready, the same panel has a
**Delete everything** button that clears the samples and starts you fresh.

See [docs/SETUP.md](docs/SETUP.md) for prerequisites, the stock-pipeline
configuration, and troubleshooting.

---

## How it works

```
  your documents            finance_data/inbox/
        │
        ▼
  extract.py  ──►  redact.py  ──►  extracted/*.json      (identity stripped here)
        │
        ▼
  autoprofile.py  ──►  profile.json      (your edits always win)
        │
        ▼
  analyze.py   projection · Monte Carlo · tax · Social Security · fees · funds
        │
        ▼
  render.py    ──►  dashboard.html
```

Supported inputs: PDF (pay stub, mortgage statement, escrow analysis, Social
Security statement), and CSV/XLSX exports (bank and card ledgers, brokerage
activity, positions, vesting schedules). Header names are matched, not column
positions, so most brokerages' exports work without configuration.

---

## Limitations

Read these before you trust a number. They are real, and stating them is more
useful than pretending otherwise.

- **Not financial, tax or legal advice.** This is educational software that makes
  arithmetic and assumptions visible. It is not a fiduciary and does not know
  your situation.
- **Tax figures are estimates for 2026** and need updating each year. Confirm
  against current IRS, state and SSA publications before relying on exact dollars.
- **State tax coverage is uneven.** California and New York have full bracket
  schedules; about a dozen states have their flat rate; nine have no income tax.
  Everything else falls back to a documented **5% flat estimate** that will be
  wrong for you. Adding a state is a JSON file — see
  [docs/ADDING_A_STATE.md](docs/ADDING_A_STATE.md).
- **Retirement-income exclusions are only partly modeled.** Illinois,
  Pennsylvania and Mississippi are handled as full exemptions. The age-based and
  partial exclusions in Colorado, Georgia, Michigan, New York, Utah and others
  are **not** modeled, so those states' tax is overstated.
- **Local income taxes are not modeled** — New York City, Philadelphia and Ohio
  municipalities among them.
- **One state of residence.** Part-year and multi-state residency are out of scope.
- **US only.** Federal brackets, FICA, 401(k)/IRA limits, RMDs, Social Security
  and Medicare are all US-specific.
- **The projection simplifies deliberately,** and says so on the dashboard: the
  retirement year is modeled as fully retired, and all withdrawals are taxed as
  ordinary income, which is conservative for taxable-account dollars.
- **Market returns are assumptions, not forecasts.** Nobody knows the future; the
  Monte Carlo shows a distribution precisely because a single number would lie.

---

## Contributing

Bug reports, state tax tables and parser support for more export formats are all
welcome. Run `python finance/selftest.py` before opening a PR — every module
carries its own self-test and new code should too. See
[CONTRIBUTING.md](CONTRIBUTING.md).

Never commit anything from `finance_data/`.

## License

MIT — see [LICENSE](LICENSE).
