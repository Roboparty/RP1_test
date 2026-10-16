"""Deb entry point: execute exactly the launcher shipped from the source checkout."""
from pathlib import Path
import os
import sys

program = Path(__file__).resolve().parent
os.execv(sys.executable, [sys.executable, str(program / "launch.py"), *sys.argv[1:]])
