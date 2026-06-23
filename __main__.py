#!/usr/bin/env python3
"""loops-skill CLI entry point.

This is the "second of the two entries" the skill promises (CLI + Mavis
skill command). Run as:

    python -m loops_skill --help
    python -m loops_skill run --goal "..." --max-iter 30

The Mavis skill command simply re-imports `loop_main` from
`scripts/loop_runtime.py` and calls it.
"""
import sys
from pathlib import Path

# Make the scripts/ dir importable when this file is run as a module.
_SCRIPTS = Path(__file__).resolve().parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from loop_runtime import loop_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(loop_main())
