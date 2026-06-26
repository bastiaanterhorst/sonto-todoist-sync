#!/usr/bin/env python3
"""Convenience entry point: `python run.py --status` etc. Equivalent to `python -m syncer`."""

import sys

from syncer.main import main

if __name__ == "__main__":
    sys.exit(main())
