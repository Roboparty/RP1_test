#!/usr/bin/env python3
"""Compatibility entry point for the platform and isolated telemetry demo."""
from pathlib import Path
import subprocess
import sys

software = Path(__file__).resolve().parents[1]
if "--demo" in sys.argv[1:]:
    example = software / "examples/telemetry-demo"
    raise SystemExit(subprocess.call([sys.executable, "scripts/verify_stack.py", *sys.argv[1:]], cwd=example))
raise SystemExit(subprocess.call([sys.executable, "scripts/verify.py", *sys.argv[1:]], cwd=software))
