# Setup

## Prerequisites

- **Python 3.11 or newer** (developed against 3.14). `python --version` to check.
- About 200 MB of disk for dependencies, plus whatever your documents need.
- No database, no Docker, no cloud account.

## Install

### Windows (PowerShell)

```powershell
git clone <your-fork-url> privateplan
cd privateplan
./setup.ps1
```

### macOS / Linux

```bash
git clone <your-fork-url> privateplan
cd privateplan
./setup.sh
```

### Or do it by hand

Some people would rather not run a script from a repo that touches their
finances. That is a reasonable instinct, and the script does exactly this:

```bash
python -m venv .venv
# Windows:        .venv\Scripts\Activate.ps1
# macOS / Linux:  source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

The virtual environment lives at `.venv/` inside the repo and is gitignored.
`finance/web.py` looks for it there when it launches pipeline steps, and falls
back to whatever interpreter is running the server, so an activated venv also
works.

## Run

```bash
python server.py
```

Then open <http://127.0.0.1:5000>. The server binds to `127.0.0.1` only — it is
not reachable from your network.

## First run: the sample household

Before feeding it anything real, load the fictional sample:

1. Go to <http://127.0.0.1:5000/finance>
2. Expand **Sample data & reset** → **Load both**
3. **Extract documents** → **Analyze** → **Render dashboard**

You will get a complete plan for a made-up mid-forties couple. Poke at it, then
use **Delete everything in finance_data/** in the same panel to clear it out.
Nothing is destroyed outright — it moves to `finance_data/.trash/<timestamp>/`
until you delete that folder yourself.

Command-line equivalents:

```bash
python finance/samples.py --load full
python finance/extract.py
python finance/analyze.py
python finance/render.py
python finance/samples.py --clear
```

## Your own documents

Upload them on the `/finance` page, or drop them into `finance_data/inbox/`.

| Document | Format | Gives the plan |
|---|---|---|
| Pay stub | PDF | salary, deferral rate, employer match, pre-tax deductions, state hint |
| Brokerage / retirement statement | XLSX, CSV | account balances, holdings, cost basis |
| Brokerage activity export | CSV, XLSX | contributions, dividends, RSU vests, HSA spending, advisory fees |
| Checking / card export | CSV | what you actually spend, separated from what you save |
| Mortgage statement | PDF | balance, rate, P&I, escrow, maturity |
| Escrow analysis | PDF | the property-tax and insurance split |
| Social Security statement | PDF | benefit estimates at 62 / 67 / 70 |
| Equity vesting schedule | CSV, XLSX | unvested shares and dates |

Then **Extract**, review everything in the intake form, and **Analyze**.

**A spouse's Social Security statement** would otherwise overwrite yours. Map it
by filename in `finance_data/source_roles.json`:

```json
{ "spouse-social-security-statement.pdf": "spouse" }
```

## Stock pipeline configuration

The retirement planner works entirely offline. The stock analyzer and live
pricing reach out to SEC EDGAR and Yahoo.

**SEC EDGAR requires a real contact address** in the User-Agent header of every
request, and throttles or blocks requests without one. Set it before using the
Portfolio step:

```powershell
$env:PRIVATEPLAN_CONTACT = "you@example.com"     # PowerShell
```
```bash
export PRIVATEPLAN_CONTACT="you@example.com"     # bash / zsh
```

The address goes only to sec.gov and identifies you as a polite API consumer.
Nothing else about you is transmitted.

**Without it the company half does not work at all.** SEC does not throttle the
placeholder User-Agent, it rejects it: every EDGAR request comes back `403`, so
there are no fundamentals, no forecast and no company dashboard. Funds and ETFs
are unaffected — they come from Yahoo and never touch EDGAR.

`$env:` and `export` set the variable for **that shell only**, which is the
usual reason a pipeline that worked yesterday stops working in a new terminal.
To set it once and keep it:

```powershell
setx PRIVATEPLAN_CONTACT "you@example.com"       # Windows, new terminals only
```
```bash
echo 'export PRIVATEPLAN_CONTACT="you@example.com"' >> ~/.bashrc
```

## Troubleshooting

**Port 5000 is in use.** On macOS this is usually AirPlay Receiver
(System Settings → General → AirDrop & Handoff). Otherwise change the port at
the bottom of `server.py`.

**`ModuleNotFoundError` when a step runs.** The step ran with a different
interpreter than the server. Activate `.venv` before `python server.py`, or
confirm `.venv/` is at the repo root.

**A PDF extracts nothing.** It is probably a scanned image rather than text.
There is no OCR; enter those figures by hand in the intake form.

**A CSV is "unsupported format — skipped".** The header row was not recognised.
The parsers match on header *names* — see
[docs/DATA_MODEL.md](DATA_MODEL.md#document-formats) for what each one needs.

**Extraction overwrote something I fixed.** It should not: a field is only
auto-updated while its value still matches what the last ingest wrote. Once you
edit it, automation stops touching it. If you hit a case where that fails, that
is a bug worth reporting.

**Every ticker comes back as a fund, or with no fundamentals or forecast.**
`PRIVATEPLAN_CONTACT` is unset, so SEC is refusing every request with a `403`.
Set it as above and re-run. The pipeline now stops with that message rather than
carrying on, but data written by an earlier version may still be wrong: re-run
`python pipeline/fetch.py <TICKER>` for any equity that ended up with a
`fund.json`, and it will clear the misfiled files itself.

**`data/` is getting large.** That is the stock pipeline's cache of SEC filings.
Safe to delete; it re-downloads.

**Everything is wrong and I want to start over.**
`python finance/samples.py --clear`, then check `finance_data/.trash/` for the
copy it kept.
