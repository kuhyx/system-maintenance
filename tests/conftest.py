"""Put the ``bin/`` scripts on ``sys.path`` for the tests that import them.

``bin/`` holds standalone scripts rather than an installed package, so the
``_usage_report_*`` modules are importable only once their directory is on
``sys.path``. In the testsAndMisc monorepo a shared conftest did this for
several script directories at once; here there is only one.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1] / "bin"

if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))
