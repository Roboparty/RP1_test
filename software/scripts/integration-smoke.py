#!/usr/bin/env python3
"""Exercise account/HMI presence only in an explicitly disposable HTTPS deployment."""
import argparse
import os
from pathlib import Path
import secrets
import ssl
import sys
import tempfile
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))
from factory_hmi.sync.presence import HmiPresencePublisher
from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient, ReliabilityUploaderWorker
from factory_hmi.sync.outbox import Outbox

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--allow-test-data", action="store_true")
args = parser.parse_args()
settings = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if line and not line.startswith("#"))
if not args.allow_test_data or not settings["COMPOSE_PROJECT_NAME"].endswith("-qa"):
    parser.error("Use only an isolated project ending in -qa, with --allow-test-data")
os.environ["SSL_CERT_FILE"] = str(ROOT / "deployment/secrets/root-ca.crt")
context = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
qa = ROOT / ".qa"
qa.mkdir(exist_ok=True, mode=0o700)
password_file = qa / "smoke-admin-password"
password = password_file.read_text().strip() if password_file.exists() else settings["SEED_ADMIN_PASSWORD"]
username = settings["SEED_ADMIN_USERNAME"]

with httpx.Client(base_url=settings["PLATFORM_URL"], verify=context, timeout=20) as browser:
    def login(value):
        response = browser.post("/api/v1/auth/login", json={"username": username, "password": value})
        response.raise_for_status()
        browser.headers["X-CSRF-Token"] = browser.cookies["rp1_csrf"]
        return response.json()["data"]
    grant = login(password)
    if grant["user"]["must_change_password"]:
        next_password = secrets.token_urlsafe(24)
        response = browser.post("/api/v1/auth/change-password", json={"current_password": password, "new_password": next_password})
        response.raise_for_status()
        password = next_password
        password_file.write_text(password + "\n")
        password_file.chmod(0o600)
        login(password)
    with tempfile.TemporaryDirectory(prefix="rp1-presence-qa-") as directory:
        client = ReliabilityPlatformClient(settings["PLATFORM_URL"])
        run = {"session_key": "qa", "state": "running", "test_id": "SYNTHETIC-QA-ONLY", "sample_id": "QA-SAMPLE", "active_seconds": 120, "completed_cycles": 3}
        publisher = HmiPresencePublisher(client, data_root=Path(directory), snapshot_provider=lambda: {"runs": [run], "plc": {"driver": "mock", "phase": "RUNNING", "stale": False}}, interval=60)
        worker = ReliabilityUploaderWorker(Outbox(Path(directory) / "outbox.sqlite3"), client, presence=publisher)
        def station():
            response = browser.get("/api/v1/hmi/stations", params={"limit": 200})
            response.raise_for_status()
            return next(item for item in response.json()["data"]["items"] if item["installation_id"] == publisher.installation_id)
        try:
            worker.authenticate(username, password)
            assert station()["state"] == "running"
            run["state"] = "paused"
            publisher.send_once()
            assert station()["state"] == "paused"
        finally:
            worker.clear_credentials()
        assert station()["state"] == "offline"
print("PASS HTTPS first-login/password change and actual HMI account presence: running, paused, logout/offline; no CSV or test-record ingestion")
