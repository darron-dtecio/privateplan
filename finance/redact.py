"""Identity-PII redaction applied to every parsed document BEFORE storage.

Policy (per user, 2026-08-01): financial values — pay amounts, balances,
benefit estimates, stock symbols/quantities — are fine to keep and analyze.
Identity data must never persist or appear in logs: SSN, names, street
addresses, phone numbers, emails, dates of birth, employee/account/loan
numbers.

Extra name/string patterns can be added to finance_data/redact_names.txt
(one per line, case-insensitive) — e.g. household member names, employer
internal codes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import FIN_DATA

NAMES_PATH = FIN_DATA / "redact_names.txt"

_RULES: list[tuple[re.Pattern, str]] = [
    # SSN: 123-45-6789, 123 45 6789, and masked XXX-XX-1234 variants
    (re.compile(r"\b(?:\d{3}|[X*]{3})[- ](?:\d{2}|[X*]{2})[- ]\d{4}\b"), "[SSN]"),
    # email
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[EMAIL]"),
    # phone: 555-123-4567, (555) 123-4567, +1 555 123 4567
    (re.compile(r"(?<![\d.,$])(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"), "[PHONE]"),
    # street address: "1234 Any Street Rd" style
    (re.compile(r"\b\d{1,6}\s+[A-Za-z0-9'. ]{2,30}?\s"
                r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Court|Ct|"
                r"Boulevard|Blvd|Way|Place|Pl|Circle|Cir|Terrace|Ter|Trail|Trl|"
                r"Highway|Hwy|Parkway|Pkwy)\b\.?(?:\s*(?:Apt|Unit|Ste|Suite|#)"
                r"\s*\S{1,8})?", re.I), "[ADDRESS]"),
    # city, ST 12345(-6789)
    (re.compile(r"\b[A-Za-z .'-]{2,30},?\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?\b"), "[ADDRESS]"),
    # labeled identifiers: account/loan/employee/customer/member number → keep label
    (re.compile(r"(?i)\b(acc(?:oun)?t|loan|employee|emp|customer|member|case|file|"
                r"claim|routing|reference|ref)\s*(?:number|no\.?|num|id)?\s*[:#]?\s*"
                r"[A-Z0-9*Xx-]*\d[A-Z0-9*Xx-]{3,}"), r"\1 # [ID]"),
    # date of birth lines
    (re.compile(r"(?i)\b(date\s+of\s+birth|birth\s*date|dob|born)\b\W{0,10}"
                r"[A-Za-z0-9 ,/.-]{4,20}\d"), r"\1 [DOB]"),
    # labeled names: "Name: John Q Public", "Borrower: ...", "Prepared for ..."
    (re.compile(r"(?i)\b(name|borrower|employee|prepared\s+for|statement\s+for|"
                r"issued\s+to)\s*[:]\s*[A-Z][A-Za-z ,.'-]{2,40}"), r"\1: [NAME]"),
    # bare long digit runs (8+): account-like; dollar figures that big carry
    # commas/decimals in documents, so plain 8+ digit runs are identifiers
    (re.compile(r"(?<![\d.,$-])\d{8,}(?![\d.]|,\d)"), "[NUM]"),
]


# Financial amounts are explicitly in scope for analysis and must survive
# redaction intact. They are masked out before the identity rules run and
# restored afterwards, so no rule can eat part of a dollar figure. Only
# amount-shaped tokens are protected: bare integers (street numbers, ZIPs,
# account digits) stay exposed to the identity rules.
_AMOUNT = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|(?<![\d.])\d[\d,]*\.\d{2}(?![\d.])")
_HOLD = ""  # private-use char: not \w, not \d, matches no identity rule


def _name_rules() -> list[tuple[re.Pattern, str]]:
    rules = []
    if NAMES_PATH.exists():
        for line in NAMES_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and len(line) >= 3:
                rules.append((re.compile(re.escape(line), re.I), "[NAME]"))
    return rules


_CACHED_NAMES: list | None = None


def redact_text(text: str) -> str:
    global _CACHED_NAMES
    if _CACHED_NAMES is None:
        _CACHED_NAMES = _name_rules()
    amounts: list[str] = []

    def _stash(m):
        amounts.append(m.group(0))
        return _HOLD

    text = _AMOUNT.sub(_stash, text)
    for rx, repl in _RULES + _CACHED_NAMES:
        text = rx.sub(repl, text)
    if amounts:
        it = iter(amounts)
        text = re.sub(_HOLD, lambda _: next(it, ""), text)
    return text


def redact_pages(pages: list[str]) -> list[str]:
    return [redact_text(p or "") for p in pages]


def redact_tables(tables: list[list[list]]) -> list[list[list]]:
    return [[[redact_text(c) if isinstance(c, str) else c for c in row]
             for row in table] for table in tables]


def redact_grids(grids: dict[str, list[list]]) -> dict[str, list[list]]:
    return {sheet: [[redact_text(c) if isinstance(c, str) else c for c in row]
                    for row in grid] for sheet, grid in grids.items()}


if __name__ == "__main__":
    s = redact_text("""John Q Public   SSN: 123-45-6789   DOB: 02/17/1967
Employee ID: EMP0048291   Phone (555) 867-5309   jq.public@example.com
1234 Maplewood Drive Apt 12
Springfield, CA 95814
Account Number: 9948271003
Name: John Q Public
Gross Pay $6,346.15   YTD 88,846.10   401(k) 634.62
Principal Balance $285,321.55  Interest Rate 4.125%  AAPL 120 shares""")
    for bad in ("123-45-6789", "867-5309", "example.com", "Maplewood",
                "95814", "9948271003", "EMP0048291", "02/17/1967"):
        assert bad not in s, f"leaked: {bad}\n{s}"
    for keep in ("6,346.15", "88,846.10", "634.62", "285,321.55", "4.125",
                 "AAPL", "120 shares"):
        assert keep in s, f"over-redacted: {keep}\n{s}"
    assert "[SSN]" in s and "[PHONE]" in s and "[ADDRESS]" in s and "[NAME]" in s
    # money with commas must survive the long-digit rule
    assert redact_text("$1,234,567.89 total") == "$1,234,567.89 total"
    # amounts are protected even when adjacent text is redacted
    got = redact_text("Amount Due: $3,096.52 a If payment is received after "
                      "9/16/2026, $99.37 late fee. 1234 Maplewood Drive")
    assert "$3,096.52" in got and "$99.37" in got, got
    assert "Maplewood" not in got, got
    # multiple amounts restore in order
    assert redact_text("A $1.11 B $2.22 C $3.33") == "A $1.11 B $2.22 C $3.33"
    # 7-digit plain numbers survive (could be a big balance), 8+ get masked
    assert redact_text("balance 5850000") == "balance 5850000"
    assert redact_text("id 58500001") == "id [NUM]"
    print("redact self-test OK")
