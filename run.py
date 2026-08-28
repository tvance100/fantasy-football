#!/usr/bin/env python
"""Entry point: `python run.py` (or `uv run run.py`)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ffball.main import main  # noqa: E402

if __name__ == "__main__":
    main()
