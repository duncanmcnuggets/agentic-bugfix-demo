"""Test path setup for the standalone target application."""

import sys
from pathlib import Path

TARGET_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(TARGET_SRC))

