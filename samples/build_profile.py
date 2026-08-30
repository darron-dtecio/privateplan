"""Rebuild samples/profile.json by running the real pipeline over the samples.

    python samples/build_profile.py

Hand-authoring the document-derived sections -- holdings, spending_detail,
payroll_detail, investment_activity, equity_comp.vests -- guarantees they drift
out of step with what the parsers actually emit, and a sample that disagrees
with the code is worse than no sample.  So the flow is:

    seed_profile.json  (what a person types)
      + documents/*.csv  (what the parsers read)
      -> extract.py -> autoprofile -> profile.json  (what ships)

This WILL overwrite finance_data/, so it refuses to run unless that directory
holds nothing but sample data.  Run it after changing seed_profile.json or any
file under documents/.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "finance"))
import common  # noqa: E402
import samples as samples_mod  # noqa: E402

PY = sys.executable
SEED = HERE / "seed_profile.json"
OUT = HERE / "profile.json"

NOTE = ("FICTIONAL sample household. Every figure here is invented. Built by "
        "running the real extract pipeline over samples/documents/, so the "
        "document-derived sections are exactly what the parsers emit -- "
        "regenerate with samples/build_profile.py. Safe to commit, safe to "
        "read, safe to hand to an AI assistant. See samples/README.md.")

# Keys ordered the way a reader wants to meet them, not the way dicts happen to.
ORDER = ("version", "_note", "saved_at", "household", "income", "social_security",
         "mortgage", "home", "assets", "liabilities", "holdings", "equity_comp",
         "spending", "spending_detail", "payroll_detail", "investment_activity",
         "healthcare", "capital_expenses", "assumptions")


def _looks_like_real_data() -> bool:
    """True if finance_data/ holds anything this script did not put there."""
    profile = common.load_json(common.PROFILE_PATH)
    if profile and "FICTIONAL" not in str(profile.get("_note", "")):
        return True
    sample_docs = {p.name for p in (HERE / "documents").iterdir() if p.is_file()}
    inbox = {p.name for p in common.INBOX.iterdir() if p.is_file()}
    return bool(inbox - sample_docs)


def run(*args: str) -> None:
    result = subprocess.run([PY, *args], cwd=ROOT)
    if result.returncode:
        raise SystemExit(f"step failed: {' '.join(args)}")


def main() -> int:
    common.ensure_dirs()
    if _looks_like_real_data():
        print("refusing to run: finance_data/ contains data that is not the "
              "sample set. Back it up and clear it first "
              "(python finance/samples.py --clear).", file=sys.stderr)
        return 1

    samples_mod.clear()
    common.ensure_dirs()

    seed = json.loads(SEED.read_text(encoding="utf-8"))
    common.PROFILE_PATH.write_text(json.dumps(seed, indent=1), encoding="utf-8")
    samples_mod.load("documents")
    run("finance/extract.py", "--force")

    built = json.loads(common.PROFILE_PATH.read_text(encoding="utf-8"))
    built["_note"] = NOTE
    # _auto records which fields the last ingest owns. It is meaningful only
    # against the documents that produced it, so shipping it would have the
    # sample claim provenance a fresh install has not earned.
    built.pop("_auto", None)
    # The seed's stated price survives ingest; keep the explanation with it.
    eq = built.setdefault("equity_comp", {})
    for key in ("price_manual", "_price_note"):
        if key in seed.get("equity_comp", {}):
            eq[key] = seed["equity_comp"][key]
    eq.pop("price", None)   # an analyze-time output, not an input

    ordered = {k: built[k] for k in ORDER if k in built}
    ordered.update({k: v for k, v in built.items() if k not in ordered})
    OUT.write_text(json.dumps(ordered, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8")

    assets = ordered.get("assets", [])
    auto = sum(1 for a in assets if a.get("source") == "auto")
    print(f"\nwrote {OUT.relative_to(ROOT)}")
    print(f"  {len(assets)} assets ({auto} from documents, {len(assets) - auto} typed)")
    print(f"  {len(ordered.get('holdings', []))} holdings")
    print(f"  {len(ordered.get('equity_comp', {}).get('vests', []))} unvested events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
