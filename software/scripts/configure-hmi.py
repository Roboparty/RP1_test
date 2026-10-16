#!/usr/bin/env python3
"""Configure the current reliability platform; operator login happens in the HMI."""
import argparse
import getpass
import grp
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
from urllib.parse import urlsplit

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--station-id", required=True)
parser.add_argument("--bench-id", required=True)
parser.add_argument("--url", required=True)
parser.add_argument("--ca", type=Path, required=True)
parser.add_argument("--outputs-off", action="store_true", help="Confirm cabinet and motors are stopped before restarting gateway")
args = parser.parse_args()
if os.geteuid() != 0 or not args.outputs_off:
    parser.error("Run with sudo and --outputs-off during a maintenance window")
url = urlsplit(args.url)
if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
    parser.error("Use an HTTPS platform origin, without credentials or path")
if not re.fullmatch(r"[A-Za-z0-9_-]+", args.station_id) or not re.fullmatch(r"(?:RD|MP|OUT)-[0-9]{2,}", args.bench_id):
    parser.error("Invalid station ID or bench ID (example: RD-01)")
root = Path("/etc/rp1-test-hmi")
env = root / "gateway.env"
if not env.is_file() or not args.ca.is_file():
    parser.error("Install the HMI package and provide the site's CA certificate first")
subprocess.run(["openssl", "x509", "-in", str(args.ca), "-noout"], check=True)
shutil.copyfile(args.ca, "/usr/local/share/ca-certificates/rp1-site.crt")
os.chmod("/usr/local/share/ca-certificates/rp1-site.crt", 0o644)
subprocess.run(["update-ca-certificates"], check=True)
updates = {
    "RP1_FACTORY_PLATFORM_MODE": "reliability_v1", "RP1_FACTORY_PLATFORM_URL": args.url.rstrip("/"),
    "RP1_FACTORY_STATION_NAME": args.station_id,
    "RP1_FACTORY_STATION_ID": args.station_id, "RP1_FACTORY_BENCH_ID": args.bench_id,
    "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
}
lines = [line for line in env.read_text().splitlines() if line.split("=", 1)[0] not in updates and not line.startswith("RP1_FACTORY_MACHINE_TOKEN=")]
env.write_text("\n".join(lines + [f"{key}={value}" for key, value in updates.items()]) + "\n")
subprocess.run(["systemctl", "disable", "--now", "rp1-test-uploader.service"], check=True)
subprocess.run(["systemctl", "restart", "rp1-test-gateway.service"], check=True)
print("Station configured. Complete first password change on the web, then log into the HMI with TEST_EXECUTOR or SYSTEM_ADMIN.")
