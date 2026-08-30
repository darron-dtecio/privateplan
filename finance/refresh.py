"""Recompute and render the finance dashboard after a confirmed change."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

for script in ("analyze.py", "render.py"):
    result = subprocess.run([sys.executable, str(HERE / script)], cwd=HERE.parent)
    if result.returncode:
        raise SystemExit(result.returncode)
