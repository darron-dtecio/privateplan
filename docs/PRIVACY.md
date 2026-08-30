# Privacy architecture

Privacy claims are cheap. What matters is whether the design makes the claim hard
to violate by accident. This page describes what actually enforces it, and where
the edges are.

## The short version

- Everything personal lives under `finance_data/`, which is gitignored.
- Identity data is stripped **before** anything is written to disk, not before it
  is displayed.
- Logs and diagnostics contain no dollar values, and a test enforces that.
- The only outbound requests are public market data keyed by ticker.
- There is no account, no server, no telemetry, and no vendor.

## What lives where

```
finance_data/                 ← gitignored; everything about you is here
  inbox/                      ← raw uploaded documents (the ONLY unredacted files)
  extracted/*.json            ← parsed, redacted, per document
  extracted/summary.json      ← merged best values
  extracted/diagnostics.json  ← field names and confidence, no values
  profile.json                ← your plan's inputs
  analysis.json               ← computed results
  dashboard.html              ← the rendered dashboard
  logs/                       ← job logs from pipeline steps
  .trash/<timestamp>/         ← what a reset moved aside, until you delete it
```

Raw inbox documents are the only unredacted artifacts in the system. Nothing
reads them except `finance/extract.py`, and nothing copies them anywhere.

## Redaction: before storage, not before display

`finance/redact.py` runs on every parsed page, table and grid *before* the
extractor sees it and before anything is persisted. Twelve rules strip:

- Social Security numbers, including masked forms like `XXX-XX-1234`
- Email addresses and phone numbers
- Street addresses, and `City, ST 12345` lines
- Labeled identifiers: account, loan, employee, customer, member, case, claim,
  routing and reference numbers (the label survives, the number does not)
- Dates of birth
- Labeled names (`Name:`, `Borrower:`, `Prepared for:`, `Statement for:`)
- Any bare run of 8+ digits

Plus any literal strings you add to `finance_data/redact_names.txt`, one per
line — household members' names, an employer's internal codes, anything else
specific to you.

**Financial values are deliberately preserved.** Pay amounts, balances, benefit
estimates, symbols and share counts all survive intact, because they are the
substance of the plan and, detached from a name, they identify nobody. The
implementation is careful about this: dollar-shaped tokens are stashed behind a
private-use placeholder before the identity rules run and restored afterwards,
so a balance can never be eaten by the account-number rule. Bare integers are
*not* protected — street numbers, ZIPs and account digits must stay strippable.

Both directions are tested. `python finance/redact.py` asserts that a synthetic
identity block leaks nothing *and* that its dollar figures come through unharmed.

## Diagnostics are value-free by construction

Pipeline output names fields, counts rows and reports confidence. It does not
print amounts:

```
[extract] positions.csv  type=workbook  sheets=1 accounts=5 holdings=13
[analyze] snapshot: OK
[analyze] projection: 49 years x 3 scenarios; base depleted_at=never
[extract] missing: salary_annual, pay_frequency, k401_pct, ...
```

`finance/selftest.py` runs a PII lint that fails the suite if redaction stops
happening before classification. That makes logs safe to paste into an issue.

Two deliberate exceptions, both local-only: `autoprofile` prints the values it
writes (so you can see what a document changed), and `analyze` prints plan
figures. Those are console output on your own machine, not files that travel.

## What leaves your machine

| Feature | Network | What is sent |
|---|---|---|
| Retirement planning, extraction, projection, Monte Carlo, tax, fees | none | — |
| Live price refresh | Yahoo | ticker symbols |
| Portfolio / stock analysis | SEC EDGAR, Yahoo | ticker symbols, your `PRIVATEPLAN_CONTACT` address |
| Document download | the URL you supply | the URL you supply |

Ticker symbols do reveal *what* you hold, though not how much, and only for the
names you analyze. If that matters to you, skip the Portfolio and Prices steps —
everything else works offline.

`PRIVATEPLAN_CONTACT` exists because SEC's fair-access policy requires a contact
address in the User-Agent. It goes to sec.gov and nowhere else.

There is no analytics, no crash reporting, no update check, and no phone-home.

## Before you share a dashboard

`dashboard.html` and `portfolio.html` contain your actual numbers. They are
written into `finance_data/`, which is gitignored, precisely so you cannot commit
one by accident. If you want to show someone your setup, load the sample
household and screenshot that instead.

The stock pipeline's `dashboards/*.html` are also gitignored, because they are
generated for the tickers *you* hold and the set itself is revealing.

## Working with an AI assistant

If you use Claude Code or similar on this repo, tell it not to read
`finance_data/`. Everything it needs to debug is available without that: every
module has a self-test on synthetic data (`python finance/selftest.py`), the
diagnostics are value-free, and `samples/` is a complete fictional household
that exercises the whole pipeline.

## Resetting

`python finance/samples.py --clear`, or the panel on `/finance`.

It moves everything into `finance_data/.trash/<timestamp>/` rather than deleting
it, because the difference between "clear the samples" and "destroy an evening of
data entry" is one misread button. Delete `.trash/` by hand when you are sure.

To remove every trace, delete the `finance_data/` directory.
