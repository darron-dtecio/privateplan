"""Outbound contact identity for polite third-party API access.

SEC EDGAR's fair-access policy requires every request to carry a User-Agent
naming a real contact address, and blocks or throttles generic ones. Set
PRIVATEPLAN_CONTACT to an address you monitor before using the stock pipeline:

    PowerShell:  $env:PRIVATEPLAN_CONTACT = "you@example.com"
    bash/zsh:    export PRIVATEPLAN_CONTACT="you@example.com"

This address is the only personal detail the pipeline transmits, and it goes
only to the public data sources it queries. The finance app sends nothing.
See docs/PRIVACY.md.

Deliberately never raises at import: pipeline/sources.py imports edgar.py, and
the finance app imports sources.py, so an import-time failure here would take
down the offline half of the application over a missing stock-research setting.
"""

from __future__ import annotations

import os

APP_NAME = "PrivatePlan"
APP_VERSION = "1.0"
_PLACEHOLDER = "set PRIVATEPLAN_CONTACT"

_warned: set[str] = set()


def contact() -> str:
    """The configured contact address, or "" when unset."""
    return os.environ.get("PRIVATEPLAN_CONTACT", "").strip()


def user_agent(purpose: str = "personal research") -> str:
    return f"{APP_NAME}/{APP_VERSION} ({purpose}; {contact() or _PLACEHOLDER})"


def warn_if_unset(service: str = "SEC EDGAR") -> None:
    """Warn once per service that requests are going out without a contact."""
    if contact() or service in _warned:
        return
    _warned.add(service)
    print(f"[contact] PRIVATEPLAN_CONTACT is not set — {service} expects a real "
          f"contact address in the User-Agent header and may throttle or block "
          f"requests without one.")
    print('          PowerShell:  $env:PRIVATEPLAN_CONTACT = "you@example.com"')
    print("          bash/zsh:    export PRIVATEPLAN_CONTACT='you@example.com'")
    print("          See docs/SETUP.md.")


if __name__ == "__main__":
    assert _PLACEHOLDER in user_agent() or contact() in user_agent()
    assert user_agent().startswith("PrivatePlan/")
    os.environ["PRIVATEPLAN_CONTACT"] = "tester@example.com"
    assert "tester@example.com" in user_agent("equity research")
    assert "equity research" in user_agent("equity research")
    print("[OK] contact")
