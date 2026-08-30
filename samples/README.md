# The sample household

Everything here is fictional. It exists so you can see a finished plan — and
watch document ingestion actually work — before deciding whether to feed this
software your own statements.

```
python finance/samples.py --load full
python finance/extract.py
python finance/analyze.py
python finance/render.py
```

Or use the **Sample data & reset** panel on `/finance`.

## Who they are

Morgan and Riley, 48 and 47, married filing jointly, in California. Morgan earns
$185,000 at a mid-size company and defers 10% into a 401(k) with a $7,400 match;
Riley earns $95,000. They want to retire in June 2040, when Morgan turns 62.

They have about $1.45M across a 401(k), a spouse 401(k), a rollover IRA, a Roth
IRA, an HSA, an advised brokerage account and cash. Their house is worth $780,000
with $268,000 left on a 3.5% mortgage maturing in 2038 — twelve years out, which
matters because the projection treats that payment as a fixed cost that stops on
a real date. Morgan has 1,235 unvested RSUs vesting through 2029.

They are, broadly, on track. The plan does not deplete on the base projection.
That is deliberate: a sample that screams at you teaches less than one where the
problems are real but have to be found.

## What there is to find

Three things are planted, because they are the findings this software exists to
surface and a household would not spot them from statements alone.

**Fee drag.** The $248,000 brokerage account is advised at 1.05% a year, billed
quarterly, against a passive alternative at 0.03%. `finance/fees.py` measures the
rate from the four actual charges rather than the published schedule, converts it
to dollars, works out what the advisor must beat just to leave them level, and
then answers the question nobody asks: how many years of track record would be
needed to *demonstrate* that they are earning it. Run the **Portfolio** step to
see it — that step is what prices the fees against real returns.

**Fund overlap.** They hold FXAIX, VTSAX, VTI, SPY, VOO and VUG across six
accounts. Every one of those is substantially the same thirty US megacaps.
`finance/funds.py` unwraps them and reports what the portfolio really owns, with
the coverage percentage alongside, since published top-holdings data only
accounts for part of a fund.

**Spending that is not spending.** The checking export includes $1,900 a month of
transfers to brokerage and Fidelity accounts. Counted as consumption — which most
naive imports do — it would inflate their retirement need by nearly a quarter and
make the plan look far worse than it is. `finance/spending.py` separates it out.

There is also an HSA paying medical bills directly by debit card, which never
touches the checking account and would otherwise be invisible in the spending
picture entirely.

## What is in here

| File | What it is |
|---|---|
| `seed_profile.json` | The hand-entered half — exactly what a person types into the intake form. Accounts that appear in `documents/positions.csv` are deliberately absent. |
| `profile.json` | `seed_profile.json` after the real pipeline has run over `documents/`. This is what `--load profile` installs. |
| `advisory_fees.json` | The advised account, its schedule, and four quarterly charges. |
| `documents/checking_ledger.csv` | 18 months: pay, mortgage, bills, transfers, two genuine one-offs. |
| `documents/card_ledger.csv` | 18 months of card purchases across most spending categories. |
| `documents/brokerage_activity.csv` | Contributions, match, dividends, RSU deposits, HSA spending, advisory fees, one rollover. |
| `documents/positions.csv` | 13 positions across five accounts, with cost basis on the taxable ones. |
| `documents/vesting_schedule.csv` | Past and future vests, including one conditional award. |
| `generate_documents.py` | Regenerates the CSVs. Deterministic — same seed, same bytes. |
| `build_profile.py` | Rebuilds `profile.json` by running the real pipeline over the documents. |

## Why the profile is generated, not written

Hand-authoring the document-derived sections — `holdings`, `spending_detail`,
`payroll_detail`, `investment_activity`, `equity_comp.vests` — guarantees they
drift out of step with what the parsers actually emit, and a sample that
disagrees with the code is worse than no sample. So:

```
seed_profile.json  +  documents/*.csv  →  extract.py  →  autoprofile  →  profile.json
```

After changing the seed or any document, run `python samples/build_profile.py`.
It refuses to run if `finance_data/` holds anything that is not sample data.

## Two things it cannot demonstrate

**PDF parsing.** The sample set is CSV only. Hand-built PDFs that satisfy the
keyword classifier prove nothing about a real pay stub or Social Security
statement, and would cost more to maintain than they are worth. The PDF
extractors are covered by inline fixtures in `python finance/extract.py --selftest`.
Because there is no pay stub here, `payroll_detail` is stated in the seed instead
— that is what lets the 401(k) audit cross-check the deferral rate.

**A real share price for the RSUs.** The employer is invented, so there is no
ticker to quote. `equity_comp.price_manual` states $118.50 instead — the same
thing you would do for a private-company award with a 409A valuation and no
public market.

## Getting rid of it

```bash
python finance/samples.py --clear
```

Nothing is destroyed: it moves to `finance_data/.trash/<timestamp>/` until you
delete that folder yourself.
