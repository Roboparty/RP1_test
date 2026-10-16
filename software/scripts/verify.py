#!/usr/bin/env python3
"""Read-only deployment acceptance. Does not connect to or write any PLC."""
import json
from pathlib import Path
import ssl
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
settings = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if line and not line.startswith("#"))
context = ssl.create_default_context(cafile=str(ROOT / "deployment/secrets/root-ca.crt"))
base = settings["PLATFORM_URL"]
for route in ("/healthz", "/readyz", "/", "/openapi.json"):
    with urllib.request.urlopen(base + route, context=context, timeout=15) as response:
        assert response.status == 200
        print("PASS HTTPS " + route)
subprocess.run([str(ROOT / "scripts/compose.sh"), "exec", "-T", "worker", "python", "scripts/check_worker.py"], cwd=ROOT, check=True)
subprocess.run([str(ROOT / "scripts/compose.sh"), "exec", "-T", "archive-worker", "python", "scripts/check_archive_worker.py"], cwd=ROOT, check=True)
print("PASS HTTPS, API/PostgreSQL readiness and worker process/database connectivity")
