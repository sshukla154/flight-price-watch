"""Makes the repo-root scripts (flightwatch_core.py, check_price.py,
check_gorakhpur.py) importable from tests/ regardless of pytest's own
import-mode defaults or invocation directory -- this repo is a flat
script layout, not a package, so there's no other mechanism that would
put the repo root on sys.path for test collection.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
