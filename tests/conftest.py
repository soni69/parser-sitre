"""Make the project root importable for tests.

The project keeps source modules at the repo root (`coverage.py`, `utils.py`,
`scraper.py`, ...) instead of a `src/` package layout, so we prepend the
parent directory to `sys.path` here. This also avoids any clash with a
globally installed `coverage` distribution: the project-local module is
resolved first.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
