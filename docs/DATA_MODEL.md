# Data model

Everything the planner knows is one file: `finance_data/profile.json`. This page
documents its shape, the unit conventions that will bite you if you guess, how
document ingestion decides what to overwrite, and what each parser needs from a
document.

`samples/profile.json` is a complete, valid, entirely fictional example — the
fastest way to see any of this concretely.

## Units: read this first

The schema mixes three numeric conventions, and mixing them up silently produces
a plausible-looking wrong answer.

| Convention | Fields | Example |
|---|---|---|
| **Fraction** | `mortgage.rate`, `liabilities[].rate`, `social_security.cola`, `assumptions.inflation`, `healthcare.inflation`, `capital_expenses.*_inflation`, `home_reserve_pct` | `0.035` = 3.5% |
| **Percent number** | `income.k401_pct`, `income.employer_match_pct` | `10` = 10% |
| **Dollars** | everything else | `185000` |

Monthly vs annual is always in the field name (`_monthly`, `_annual`,
`payment_monthly`). Dates are `YYYY-MM-DD`; months are `YYYY-MM`.

The intake form handles the conversion for you — its `pctfrac` fields take a
percent and store a fraction, `pctnum` fields store the percent as typed. Hand
editing is where mistakes happen.

## Top level

```jsonc
{
  "version": 2,                 // PROFILE_VERSION; migrations live in common.py
  "saved_at": "2026-08-01T09:00:00",
  "household": {...},
  "income": {...},
  "social_security": {...},
  "mortgage": {...} | null,
  "home": {...},
  "assets": [...],
  "liabilities": [...],
  "holdings": [...],            // document-derived
  "equity_comp": {...},         // document-derived + your judgement
  "spending": {...},
  "spending_detail": {...},     // document-derived
  "payroll_detail": {...},      // document-derived
  "investment_activity": {...}, // document-derived
  "healthcare": {...},
  "capital_expenses": {...},
  "assumptions": {...},
  "_auto": {...}                // provenance ledger, see below
}
```

### `household`

| Field | Type | Notes |
|---|---|---|
| `self_birthdate` | `YYYY-MM-DD` | Required. Drives every age calculation. |
| `spouse_birthdate` | `YYYY-MM-DD` \| null | Null reuses the self birthdate for horizon purposes. |
| `state` | 2-letter code | Selects `finance/taxtables/states/<xx>.json`. Unknown codes get the 5% fallback. |
| `filing_status` | `mfj` \| `single` \| `hoh` \| `mfs` | Selects the federal table. |
| `retirement_date` | `YYYY-MM` | Modeled as fully retired from this month. |

### `income`

`salary_annual` (gross), `pay_frequency` (`weekly|biweekly|semimonthly|monthly`),
`k401_pct` (**percent number**), `employer_match_annual` (dollars — **wins over**
`employer_match_pct` when set), `employer_match_pct` (percent of salary),
`other_pretax_annual`, `spouse_income_annual`.

### `social_security`

`self` and `spouse` are dicts with **string** age keys mapping to monthly dollars:
`{"62": 2480, "67": 3540, "70": 4390}`. Plus `claim_age_self` / `claim_age_spouse`
(int 62–70), `spouse_own_monthly_fra`, `cola` (fraction), and
`compare_claim_ages`.

### `mortgage`

`balance`, `rate` (**fraction**), `pi_payment` (principal and interest **only**),
`escrow_payment`, `next_due` (`YYYY-MM`), `maturity` (`YYYY-MM`),
`extra_monthly`, and `escrow_detail` from an escrow-analysis PDF
(`property_tax_annual`, `insurance_annual`, `reserve_monthly`, `shortage`).

Without an escrow split, the projection falls back to `escrow_payment × 12` as
property tax.

### `assets` and `liabilities`

Assets carry `name`, `type`, `balance`, optional `cost_basis` (taxable only),
`annual_contribution`, and an optional `source: "auto"` marker.

`type` is one of `401k`, `trad_ira`, `roth`, `hsa`, `brokerage`, `annuity`,
`cash`, `other`, bucketed into taxable / tax-deferred / Roth for the projection.

A non-zero `annual_contribution` on a `401k` row is **ignored** when
`income.k401_pct` is set, so payroll deferrals are never counted twice. The
analysis reports when it does this.

Liabilities carry `name`, `balance`, `rate` (**fraction**), `payment_monthly`.
A `Mortgage` row is maintained automatically from the mortgage block.

### `holdings`

Per symbol, after deduplication and summing across accounts:

`symbol`, `description`, `value`, `quantity`, `cost_basis`, plus `qty_covers` and
`cost_covers` — the dollars of market value that the quantity and cost figures
actually account for. Those two exist so partial disclosure never shows a fake
gain: if a statement reports cost for only some rows, the coverage says so
instead of implying the rest cost nothing.

### `equity_comp`

Refreshed from a vesting export on each ingest: `vests` (see below), `conditions`,
`has_options`, `source_files`, `as_of`, `withholding_measured`.

Never overwritten, because they are your judgement: `symbol` (ticker used to price
vests), `price_manual` (a stated per-share price for an unlisted employer — a
409A valuation, say), `include_conditional`, `enabled`, `withholding`.

A live quote for `symbol` wins; `price_manual` is used when there is none; with
neither, shares are scheduled but contribute no value to the plan.

Each vest: `date`, `shares`, `award_date`, `award_type`, `award_id`, `strike`,
`condition`, `is_option`. Only vests dated after today count — a schedule export
is mostly history. Full-value awards (RSU, PSU, RSA…) are worth the share price;
options are worth only the spread over strike, and nothing below it.

### `spending`

`current_monthly`, `retirement_monthly_today` (**excludes** mortgage P&I and
escrow — the projection adds those as their own streams until the loan matures),
and `observed_medical_monthly` (subtracted before explicit retirement healthcare
is added, so medical is never counted twice).

### `assumptions`

`risk` (`conservative|moderate|aggressive`), `inflation` (fraction),
`horizon_age`, `return_override`, `monte_carlo_paths`, `compare_claim_ages`,
`taxable_basis_unknown_is_gain`, and `fee_drag` — which is injected at analysis
time from measured advisory fees and subtracted from the mean return, not entered
by you.

## Provenance: how "your edits win" works

`profile["_auto"]` maps each form field to the value the last ingest wrote there.
On every extraction, for each field:

1. If the current value **differs** from `_auto`, you edited it. The key is
   dropped from `_auto` and the value is never touched again.
2. If it was never auto-managed and holds a meaningful value, it is kept, and the
   run logs `kept your value for X (document says Y, you set Z)`.
3. Otherwise the document value is written and `_auto` is updated.

Assets and liabilities use a row-level equivalent: rows tagged `source: "auto"`
are purged and rebuilt each run; rows you added by hand survive untouched.

The intake form shows *verify — from documents (auto)* on auto-managed fields, so
you can see which numbers are still tracking your paperwork.

## Document formats

Parsers match on header **names**, not column positions, and scan the first 15
rows for a header. Extra columns are ignored.

| Type | Detected by | Needs |
|---|---|---|
| Bank / card ledger | CSV/XLSX headers | a `date` column, a `description`/`payee`/`memo` column, and an `amount`/`debit` column. Card ledgers additionally have `card member`/`card no`/`merchant`. |
| Brokerage activity | CSV/XLSX headers | `date`, `action`, `amount`; optionally `account`, `symbol`, `description`, `type`, `price`, `quantity`. |
| Positions / holdings | CSV/XLSX headers | a market-value column plus `symbol` or `description`. Cost basis must be a **total**, not per share — per-share columns are rejected outright, because a per-share cost against a total value overstates the gain by the share count. |
| Vesting schedule | CSV/XLSX headers | both a vest-date column and a shares column. |
| Pay stub | PDF keywords | labels like "Gross Pay", "Net Pay", "Fed Income Tax", "401(k) Pre Tax", "Period Ending". |
| Mortgage statement | PDF keywords | "Unpaid Principal Balance", "Interest Rate", "Principal & Interest", "Escrow", "Maturity". |
| Escrow analysis | PDF keywords | "Escrow Account Disclosure", "Projected Escrow Account Activity". |
| Social Security statement | PDF keywords | an age 62–70 followed by a monthly amount. |

Amounts accept `$`, commas and `(1,234.00)` for negatives. Dates accept
`MM/DD/YYYY`, `YYYY-MM-DD`, `DD-Mon-YYYY` and several others.

Sign conventions: in a **checking** ledger, spending is negative and income
positive; in a **card** ledger, purchases are positive.

`samples/documents/` has a working example of each CSV format.

## Migrations

`common.PROFILE_VERSION` is the current schema version and
`common.migrate_profile()` upgrades older files forward. It deep-copies rather
than mutating its input — an invariant the self-test asserts. Add a migration
step there when you add a field with a meaningful default.
