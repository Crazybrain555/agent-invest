#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CLI entry for factor correlation tool."""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.tools.corr.cli import main


if __name__ == "__main__":
    main()
