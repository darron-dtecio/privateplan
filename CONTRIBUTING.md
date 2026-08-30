# Contributing

## Before you open a PR

```bash
python finance/selftest.py
```

Every module must pass, and so must the PII lint. That one exists to catch a
specific regression: redaction has to happen *before* documents are classified
and stored, and the lint fails the build if that stops being true.

## Never commit personal data

`finance_data/`, `data/` and `dashboards/` are gitignored and must stay that way.
Before pushing:

```bash
git status
git ls-files | grep -E "^(finance_data|data|dashboards)/"   # must be empty
```

If you need to share a dashboard for a bug report, load the sample household and
screenshot that.

There is a scrub gate for this:

```bash
python scripts/scrub_check.py
```

It scans the tree for personal identifiers, absolute home paths, private network
addresses, leftover fixtures and stray control characters, and exits non-zero on
any hit. Run it before any release, and add a pattern whenever you find a class
of leak it missed.

## Conventions worth knowing

**Every module carries its own self-test.** Look at the bottom of any file in
`finance/` — there is a `if __name__ == "__main__":` block with real assertions on
synthetic data. `finance/selftest.py` runs them all as subprocesses. New code
should come with the same. This is unusual and deliberate: it means any module
can be verified in isolation, on a machine holding no personal data at all.

**`finance/` is a flat module directory, not a package.** Modules do
`sys.path.insert(0, ...)` and import each other flat (`import common`). `common.py`
additionally puts `pipeline/` on the path, so finance code can reuse the chart
machinery in `pipeline/render.py`. Please don't convert this to a package as a
drive-by change — `finance/render.py` deliberately shadows `pipeline/render.py`
and loads it by file path to work around exactly that collision.

**Diagnostics never print dollar values.** `common.diag()` output goes into job
logs that people paste into issues. Field names, row counts and confidence scores
only. `autoprofile` and `analyze` are the two deliberate exceptions, and they
print to your own console rather than to a file that travels.

**Financial values are not PII; identity is.** `finance/redact.py` documents the
line. Don't add a rule that eats balances, and don't add one that lets a name
through.

**Assumptions get stated, not hidden.** Where the model simplifies, it says so on
the dashboard. If you add a simplification, surface it.

## Especially welcome

**State tax tables.** Coverage is uneven and this is pure data entry with high
value — see [docs/ADDING_A_STATE.md](docs/ADDING_A_STATE.md). One state per PR,
with the source you transcribed from.

**Retirement-income exclusions.** Colorado, Georgia, Michigan, New York, Utah and
others have partial or age-based exclusions that are not modeled today, so those
states' retirement tax is overstated. The schema has a slot for it; the logic
needs building.

**Export formats.** If your brokerage's CSV is not recognised, a header mapping
plus a sample of the shape (with your numbers replaced) is a great PR.

**Filing statuses in state tables.** Most shipped states only define `mfj` and
`single`; `hoh` and `mfs` fall back to `single`.

## Please don't

- Add telemetry, analytics, update checks, or any outbound request that carries
  something other than a ticker symbol.
- Add a cloud sync, hosted mode, or account system. The absence of those is the
  product.
- Add a dependency without saying what it buys. The install is deliberately small.

## Reporting a bug

Include what you ran, what happened, and the relevant job log from
`finance_data/logs/` — those are value-free by design, so they are safe to paste.
If it involves a document, describe its shape (column headers, roughly how many
rows) rather than attaching it.
