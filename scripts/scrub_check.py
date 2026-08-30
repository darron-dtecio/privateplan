"""Pre-publish scrub gate for PrivatePlan.

Every pattern here must return zero hits before the repo is pushed anywhere.
Exit code is the number of failing patterns.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(".").resolve()
SKIP_DIRS = {".git", ".venv", "__pycache__", "finance_data", "data", "dashboards",
             "node_modules"}
# The gate lists the patterns it hunts for, so scanning itself always "fails".
SKIP_FILES = {"scrub_check.py"}
TEXT_SUFFIXES = {".py", ".json", ".j2", ".md", ".txt", ".html", ".css", ".js",
                 ".ps1", ".sh", ".yml", ".yaml", ".toml", ".cfg", ""}

# (label, regex, note) -- note explains what a hit would mean
PATTERNS = [
    ("author name",        r"darron|inman|darroni\b", "the author's name"),
    ("author email",       r"darroninman|outlook\.com", "the author's email"),
    ("author company",     r"\bdtec[\s\-_]?io\b|DTEC IO", "the author's employer"),
    ("home path",          r"[A-Za-z]:\\Users\\darro|/Users/darro|OneDrive[\\/]Source",
                           "an absolute path from the author's machine"),
    ("LAN address",        r"\b192\.168\.\d+\.\d+|\b10\.\d+\.\d+\.\d+\b",
                           "a private network address"),
    ("masked account",     r"\*6310|\b6310\b", "the author's masked account number"),
    ("real CUSIP",         r"594918104|33739E108", "a real security identifier"),
    ("employer leak",      r"MICROSOFT 401K|MICROSOFT EDIPAY|DEPOSITED MICROSOFT",
                           "the author's employer in a fixture"),
    ("locale leak",        r"villa park|rockwell|econo\s*air|knotty|serrano|placentia",
                           "the author's local vendors"),
    ("household dates",    r"1967-02-17|1971-03-29|2032-02", "the author's real dates"),
    ("spouse name",        r"\btammy\b", "a household member's name"),
    ("advisor name",       r"wells fargo advisor", "a real institution in sample data"),
    ("removed copilot",    r"finance-chat|/finance/ai\b|lm\s*studio|\bopenai\b|import ai\b",
                           "a leftover from the removed AI copilot"),
    ("holdings cluster",   r"\bBUFR\b|\bBUFF\b|\bBUFD\b|\bWTAI\b|\bNUAG\b|\bASHIX\b|\bEMLP\b|\bSPXX\b",
                           "tickers unique to the author's portfolio"),
    ("control chars",      r"[\x00-\x08\x0b\x0c\x0e-\x1f]",
                           "a stray control character (mangled escape)"),
]

files = []
for p in ROOT.rglob("*"):
    if not p.is_file():
        continue
    if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
        continue
    if p.suffix.lower() not in TEXT_SUFFIXES:
        continue
    if p.name in SKIP_FILES:
        continue
    files.append(p)

print(f"scanning {len(files)} files\n")

failures = 0
for label, pattern, note in PATTERNS:
    rx = re.compile(pattern, re.I)
    hits = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:110]}")
    if hits:
        failures += 1
        print(f"FAIL  {label}  ({note})")
        for h in hits[:12]:
            print(f"        {h}")
        if len(hits) > 12:
            print(f"        ... and {len(hits) - 12} more")
        print()
    else:
        print(f"ok    {label}")

print()
if failures:
    print(f"{failures} pattern(s) still match. Not safe to publish.")
else:
    print("Scrub gate clean.")
sys.exit(failures)
