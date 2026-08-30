"""Run every finance module's self-test plus the no-PII-in-diagnostics lint.

Usage: python finance/selftest.py
Safe for Claude to run and read — everything here uses synthetic data.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULES = ["common", "taxdata", "mortgage", "socsec", "tax", "projection", "web",
           "montecarlo", "recommend", "redact", "autoprofile", "spending",
           "investments", "funds", "fees", "reprice", "vesting", "analyze",
           "extract", "goals", "samples", "download"]
# analyze.py and extract.py run the real pipeline over finance_data/ when
# invoked bare, so their synthetic checks live behind a flag
SELFTEST_ARGS = {"analyze": ["--selftest"], "extract": ["--selftest"],
                 "samples": ["--selftest"], "download": ["--selftest"]}
# Stock-pipeline modules that carry a self-test of their own. They live outside
# finance/ but this is the gate CONTRIBUTING points people at, so they run here.
PIPELINE_MODULES = {"narrate": ["--selftest"], "edgar": ["--selftest"]}


def pii_lint() -> list[str]:
    """Redaction wiring check: extract.py must scrub parsed content with
    redact.py before storing or extracting (identity PII must never persist)."""
    problems = []
    src = (HERE / "extract.py").read_text(encoding="utf-8")
    for required in ("import redact", "redact.redact_pages(", "redact.redact_tables(",
                     "redact.redact_grids("):
        if required not in src:
            problems.append(f"extract.py: missing '{required}' — parsed content "
                            "would persist unredacted")
    if not re.search(r"redact\.redact_pages\(pages\)[\s\S]{0,200}classify_doc", src):
        problems.append("extract.py: redaction must run BEFORE classify/extract")
    return problems


def main() -> int:
    failed = []
    for mod in MODULES:
        r = subprocess.run([sys.executable, str(HERE / f"{mod}.py")]
                           + SELFTEST_ARGS.get(mod, []),
                           capture_output=True, text=True, cwd=HERE.parent)
        ok = r.returncode == 0
        print(f"[{'OK' if ok else 'FAIL'}] {mod}")
        if not ok:
            print(r.stdout[-2000:])
            print(r.stderr[-2000:])
            failed.append(mod)
    for mod, extra in PIPELINE_MODULES.items():
        r = subprocess.run([sys.executable, str(HERE.parent / "pipeline" / f"{mod}.py")]
                           + extra, capture_output=True, text=True, cwd=HERE.parent)
        ok = r.returncode == 0
        print(f"[{'OK' if ok else 'FAIL'}] pipeline/{mod}")
        if not ok:
            print(r.stdout[-2000:])
            print(r.stderr[-2000:])
            failed.append(f"pipeline/{mod}")

    problems = pii_lint()
    for pb in problems:
        print(f"[FAIL] pii-lint: {pb}")
        failed.append("pii-lint")
    if not problems:
        print("[OK] pii-lint")
    print("ALL OK" if not failed else f"FAILURES: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
