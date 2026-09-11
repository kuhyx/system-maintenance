#!/usr/bin/env python3
"""Replace (or delete) a marker-delimited block in a text file.

    _replace_block.py <file> <begin-marker> <end-marker> <replacement>

An empty replacement removes the block and the blank line before it. Used
by install_home_tidy.sh for the ~/.zshrc nag block; kept out of bash so
the edit is exact and never a fragile sed range.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def replace_block(text: str, begin: str, end: str, replacement: str) -> str:
    """Swap the first ``begin``..``end`` block for ``replacement``."""
    pattern = re.compile(r"\n?" + re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.DOTALL)
    new = ("\n" + replacement + "\n") if replacement else "\n"
    return pattern.sub(lambda _m: new, text, count=1)


def main(argv: list[str]) -> int:
    """CLI entry."""
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1])
    path.write_text(replace_block(path.read_text(encoding="utf-8"), argv[2], argv[3], argv[4]), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
