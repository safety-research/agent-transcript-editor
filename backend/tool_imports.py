"""Shared sys.path setup for importing bundled tools.

Small utilities (fix_ids, minimize, check_consistency, neutralize) are bundled
in the vendor/ directory. trusted-monitor is expected as a pip-installed package
(pip install trusted-monitor).
"""

import sys
from pathlib import Path

# Add vendor/ to sys.path so bundled tools are importable
_VENDOR_PATH = str(Path(__file__).resolve().parent / "vendor")
if _VENDOR_PATH not in sys.path:
    sys.path.insert(0, _VENDOR_PATH)
