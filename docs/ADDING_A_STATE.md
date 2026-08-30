# Adding or correcting a state tax table

State tables are data, not code. Adding one is a single JSON file and needs no
Python. This is the most useful contribution the project can receive, because
coverage is currently uneven and a wrong state tax quietly corrupts every number
downstream of it.

## Where they live

```
finance/taxtables/
  federal.json          brackets keyed by filing status
  states/
    _schema.json        the format, documented field by field
    _default.json       the 5% flat fallback for anything not covered
    ca.json  ny.json    full bracket schedules
    il.json  pa.json    flat rates
    tx.json  fl.json    no income tax
```

The file is named for the lowercase USPS code: `finance/taxtables/states/oh.json`.
Adding it is enough — the intake form's state list is generated from this
directory, so the new state appears automatically.

## The three kinds

### `none` — no state income tax

```json
{
  "code": "TX", "name": "Texas", "tax_year": 2026,
  "source": "No state income tax on wages or retirement income.",
  "kind": "none",
  "estimate": false,
  "taxes_social_security": false,
  "taxes_retirement_income": "full",
  "index_brackets": false
}
```

### `flat` — one rate

```json
{
  "code": "IL", "name": "Illinois", "tax_year": 2026,
  "source": "Illinois flat income tax rate, estimated for 2026. Verify with the state revenue department.",
  "kind": "flat",
  "rate": 0.0495,
  "estimate": true,
  "taxes_social_security": false,
  "taxes_retirement_income": "exempt",
  "local_tax_note": "Illinois does not tax distributions from qualified retirement plans, IRAs or Social Security.",
  "index_brackets": false
}
```

### `brackets` — a progressive schedule

```json
{
  "code": "CA", "name": "California", "tax_year": 2026,
  "source": "FTB 2025 tax rate schedules, indexed. Verify at ftb.ca.gov.",
  "kind": "brackets",
  "estimate": true,
  "filing_status": {
    "mfj":    { "brackets": [[22108, 0.01], [52420, 0.02], [null, 0.123]],
                "standard_deduction": 11540 },
    "single": { "brackets": [[11054, 0.01], [26210, 0.02], [null, 0.123]],
                "standard_deduction": 5770 }
  },
  "taxes_social_security": false,
  "taxes_retirement_income": "full",
  "surtaxes": [{ "name": "Mental Health Services Tax", "threshold": 1000000, "rate": 0.01 }],
  "property_tax_growth": 0.02,
  "index_brackets": true
}
```

Brackets are `[upper_bound, rate]` pairs in ascending order. The last bound is
`null`, meaning unbounded. Rates are fractions, not percents.

A missing filing status falls back to `single`, then to the flat rate. Supplying
`mfj` and `single` covers most users; `hoh` and `mfs` are worth adding if you
have the published figures.

## The fields that change answers

**`index_brackets`** — `true` if the state inflation-adjusts its brackets each
year. Several states deliberately do not, and bracket creep is then real policy.
Getting this wrong compounds over a forty-year projection.

**`taxes_social_security`** — most states exempt benefits entirely. A handful do
not. Set it honestly; the projection adds taxable benefits to the state base only
when this is `true`.

**`taxes_retirement_income`** — `"full"` or `"exempt"`. `"exempt"` means the state
does not tax qualified retirement distributions at all (Illinois, Pennsylvania,
Mississippi). **Partial and age-based exclusions are not yet modeled** — Colorado,
Georgia, Michigan, New York, Utah and others have them. If your state is one of
those, use `"full"` and describe the real rule in `local_tax_note` rather than
approximating it with `"exempt"`; overstating the tax is the safer error, and a
note tells the next contributor what still needs building.

**`property_tax_growth`** — only where a state actually caps assessed-value
growth (California's Proposition 13 is why this field exists). Leave it out
otherwise and the national default applies.

**`local_tax_note`** — surfaced to the user. Use it for meaningful municipal
income taxes (New York City, Philadelphia, Ohio municipalities), which are **not**
modeled, and for exclusions you could not express in the schema.

**`source`** — required. A table nobody can verify is worse than no table.
Cite the schedule and the year, and link the revenue department where you can.

**`estimate`** — `true` unless you transcribed a published schedule for exactly
this tax year.

## Testing your table

```bash
python finance/taxdata.py       # tables load and parse
python finance/tax.py           # tax behaviour, including per-state assertions
python finance/selftest.py      # everything
```

A quick manual check:

```python
import sys; sys.path.insert(0, "finance")
import taxdata as td
p = td.load_state("OH", "mfj")
print(p, p.tax(150_000), p.marginal_rate(150_000))
```

Then set your state in the intake form, run Analyze, and sanity-check the
retirement tax figures against a return you have actually filed. That comparison
catches more mistakes than any unit test.

## Opening the PR

One state per PR, please — it makes the numbers reviewable. Include the source
you transcribed from, say which filing statuses you covered, and flag anything
you approximated.
