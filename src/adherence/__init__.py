"""adherence-suite — deterministic agent-adherence benchmark.

Runtime code is stdlib-only. `REPO_ROOT` is the checkout root: scenarios,
results files and fixture mirrors live there, not inside the package.
"""
from __future__ import annotations

from pathlib import Path

__version__ = "0.2.0"

#: Checkout root — src/adherence/__init__.py -> src/adherence -> src -> root
REPO_ROOT = Path(__file__).resolve().parents[2]
