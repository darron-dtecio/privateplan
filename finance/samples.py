"""Load the fictional sample household, or wipe finance_data/ and start fresh.

Two jobs, deliberately in one module because they are the same decision seen
from opposite sides: what is in finance_data/ right now, and what should be.

    python finance/samples.py --load profile      # a finished plan, instantly
    python finance/samples.py --load documents    # raw files, to watch ingest
    python finance/samples.py --load full         # both
    python finance/samples.py --clear             # remove everything
    python finance/samples.py --clear --keep-documents

Nothing here deletes irreversibly.  A clear moves files into a timestamped
finance_data/.trash/<stamp>/ instead of unlinking them, because the difference
between "I wanted the sample data gone" and "I just destroyed an evening of
intake work" is one misread button, and the recovery costs three lines of code.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
from common import FIN_DATA, ROOT, diag

SAMPLES = ROOT / "samples"
SAMPLE_DOCS = SAMPLES / "documents"

# Files the app writes under finance_data/.  Everything here is either derived
# (regenerate it) or entered by you (the trash copy is your undo).
DATA_FILES = [
    "profile.json", "profile.json.bak", "analysis.json",
    "dashboard.html", "portfolio.html", "portfolio.json",
    "prices.json", "price_history.json", "goals.json",
    "advisory_fees.json", "cost_basis_overrides.json", "source_roles.json",
    "redact_names.txt", "links.txt", "funds_extra.json",
    # left over from installs that predate the removal of the chat copilot
    "ai_settings.json",
]
DATA_DIRS = ["inbox", "extracted", "logs", "chat"]

# Copied into finance_data/ by --load profile.
PROFILE_FILES = {"profile.json": "profile.json",
                 "advisory_fees.json": "advisory_fees.json"}


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def load(mode: str = "profile", force: bool = False) -> dict:
    """Install sample data.  mode: profile | documents | full."""
    if mode not in {"profile", "documents", "full"}:
        raise ValueError(f"unknown mode {mode!r}; expected profile, documents or full")
    if not SAMPLES.is_dir():
        raise FileNotFoundError(f"samples directory not found at {SAMPLES}")
    common.ensure_dirs()
    written: list[str] = []

    if mode in {"profile", "full"}:
        target = common.PROFILE_PATH
        if target.exists() and not force:
            raise FileExistsError(
                f"{target.name} already exists. Loading samples would overwrite the "
                f"plan you have. Clear your data first, or pass --force if you are "
                f"sure.")
        for src_name, dst_name in PROFILE_FILES.items():
            src = SAMPLES / src_name
            if src.is_file():
                shutil.copy2(src, FIN_DATA / dst_name)
                written.append(dst_name)

    if mode in {"documents", "full"}:
        if not SAMPLE_DOCS.is_dir():
            raise FileNotFoundError(f"no sample documents at {SAMPLE_DOCS}")
        for src in sorted(SAMPLE_DOCS.iterdir()):
            if src.is_file():
                shutil.copy2(src, common.INBOX / src.name)
                written.append(f"inbox/{src.name}")

    diag(f"[samples] loaded {mode}: {len(written)} files")
    for name in written:
        diag(f"[samples]   + {name}")
    return {"mode": mode, "written": written}


def clear(keep_documents: bool = False) -> dict:
    """Move every finance_data/ artifact into a timestamped .trash/ folder."""
    # Guard against a relocated or mis-resolved data directory: only ever
    # operate on the path common.py computed, and only if it exists.
    if FIN_DATA.name != "finance_data" or not FIN_DATA.is_dir():
        raise RuntimeError(f"refusing to clear an unexpected data directory: {FIN_DATA}")

    trash = FIN_DATA / ".trash" / _stamp()
    moved: list[str] = []

    def _rescue(src: Path, rel: str) -> None:
        dst = trash / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved.append(rel)

    for name in DATA_FILES:
        path = FIN_DATA / name
        if path.is_file():
            _rescue(path, name)

    for dirname in DATA_DIRS:
        if keep_documents and dirname == "inbox":
            continue
        path = FIN_DATA / dirname
        if not path.is_dir():
            continue
        for child in list(path.iterdir()):
            _rescue(child, f"{dirname}/{child.name}")

    common.ensure_dirs()
    diag(f"[samples] cleared {len(moved)} items -> {trash}")
    return {"removed": moved, "trash": str(trash), "kept_documents": keep_documents}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--load", choices=["profile", "documents", "full"],
                    help="install sample data")
    ap.add_argument("--clear", action="store_true",
                    help="move all finance_data/ contents to .trash/")
    ap.add_argument("--keep-documents", action="store_true",
                    help="with --clear, leave inbox/ alone")
    ap.add_argument("--force", action="store_true",
                    help="with --load, overwrite an existing profile.json")
    args = ap.parse_args()

    if not args.load and not args.clear:
        ap.error("nothing to do: pass --load or --clear")

    if args.clear:
        result = clear(keep_documents=args.keep_documents)
        print(f"Cleared {len(result['removed'])} items.")
        print(f"Recoverable from {result['trash']} until you delete it.")
    if args.load:
        try:
            result = load(args.load, force=args.force)
        except FileExistsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Loaded {len(result['written'])} sample files ({args.load}).")
        if args.load in {"profile", "full"}:
            print("Next: python finance/analyze.py && python finance/render.py")
        else:
            print("Next: python finance/extract.py")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        # Exercise clear() against a temporary data directory rather than the
        # real one, so the self-test can never touch anybody's plan.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "finance_data"
            (fake / "inbox").mkdir(parents=True)
            (fake / "extracted").mkdir()
            (fake / "logs").mkdir()
            (fake / "profile.json").write_text("{}", encoding="utf-8")
            (fake / "inbox" / "doc.csv").write_text("a,b\n", encoding="utf-8")

            saved_fin, saved_inbox = common.FIN_DATA, common.INBOX
            saved_ex, saved_logs = common.EXTRACTED, common.LOGS
            globals()["FIN_DATA"] = fake
            common.FIN_DATA, common.INBOX = fake, fake / "inbox"
            common.EXTRACTED, common.LOGS = fake / "extracted", fake / "logs"
            try:
                res = clear()
                assert "profile.json" in res["removed"], res
                assert "inbox/doc.csv" in res["removed"], res
                assert not (fake / "profile.json").exists()
                # nothing is actually destroyed
                assert (Path(res["trash"]) / "profile.json").is_file()
                # directories come back so the next run has somewhere to write
                assert (fake / "inbox").is_dir() and (fake / "extracted").is_dir()
                # keep_documents leaves the inbox alone
                (fake / "profile.json").write_text("{}", encoding="utf-8")
                (fake / "inbox" / "keep.csv").write_text("a\n", encoding="utf-8")
                res2 = clear(keep_documents=True)
                assert "profile.json" in res2["removed"]
                assert (fake / "inbox" / "keep.csv").is_file(), res2
                # a data directory that is not finance_data/ is refused outright
                common.FIN_DATA = globals()["FIN_DATA"] = Path(tmp) / "somewhere_else"
                common.FIN_DATA.mkdir(exist_ok=True)
                try:
                    clear()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("clear() must refuse an unexpected directory")
            finally:
                common.FIN_DATA, common.INBOX = saved_fin, saved_inbox
                common.EXTRACTED, common.LOGS = saved_ex, saved_logs
                globals()["FIN_DATA"] = saved_fin

        # the shipped sample profile must parse and carry the sections that make
        # the whole dashboard reachable
        prof = common.load_json(SAMPLES / "profile.json")
        assert prof and prof["version"] == common.PROFILE_VERSION
        for key in ("household", "income", "social_security", "mortgage", "assets",
                    "holdings", "equity_comp", "spending_detail", "payroll_detail",
                    "investment_activity", "healthcare", "capital_expenses",
                    "assumptions"):
            assert key in prof, f"sample profile missing {key}"
        assert "unknown_taxable_basis_as_all_gain" not in prof["assumptions"]
        assert prof["assumptions"]["taxable_basis_unknown_is_gain"] is True
        for doc in ("checking_ledger.csv", "card_ledger.csv",
                    "brokerage_activity.csv", "positions.csv",
                    "vesting_schedule.csv"):
            assert (SAMPLE_DOCS / doc).is_file(), f"missing sample document {doc}"
        print("[OK] samples")
        raise SystemExit(0)
    raise SystemExit(main())
