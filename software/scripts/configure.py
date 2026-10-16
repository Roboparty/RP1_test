#!/usr/bin/env python3
"""Create first-install credentials and a private TLS CA. Never overwrite an installation."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import secrets
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", default="localhost", help="Browser/HMI DNS name or server IPv4")
    parser.add_argument("--bind", default="127.0.0.1", help="Use 0.0.0.0 for a LAN deployment")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--project", default="rp1-test-platform")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9.-]+", args.hostname) or not 1 <= args.port <= 65535:
        parser.error("Invalid hostname or port")
    ipaddress.IPv4Address(args.bind)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.project):
        parser.error("Invalid Compose project name")
    secret_dir = ROOT / "deployment/secrets"
    if (ROOT / ".env").exists() or secret_dir.exists():
        parser.error("Configuration already exists. Keep existing credentials; edit .env manually.")
    os.umask(0o077)
    secret_dir.mkdir(parents=True, mode=0o700)
    admin_password = secrets.token_urlsafe(24)
    settings = {
        "COMPOSE_PROJECT_NAME": args.project, "RP1_VERSION": (ROOT / "VERSION").read_text().strip(),
        "WEB_BIND_ADDRESS": args.bind, "WEB_PORT": str(args.port),
        "PLATFORM_URL": f"https://{args.hostname}:{args.port}",
        "POSTGRES_DB": "rp1_reliability", "POSTGRES_USER": "rp1_admin", "POSTGRES_PASSWORD": secrets.token_hex(32),
        "RP1_APP_USER": "rp1_app", "RP1_APP_PASSWORD": secrets.token_hex(32),
        "RP1_READONLY_USER": "rp1_readonly", "RP1_READONLY_PASSWORD": secrets.token_hex(32),
        "SERVICE_API_KEY_HMAC_SECRET": secrets.token_hex(48),
        "SEED_ADMIN_USERNAME": "admin", "SEED_ADMIN_PASSWORD": admin_password,
    }
    try:
        ipaddress.IPv4Address(args.hostname)
        extra_san = "IP:" + args.hostname
    except ValueError:
        extra_san = "DNS:" + args.hostname
    commands = [
        ["req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "3650", "-sha256",
         "-subj", "/CN=RP1 Site Local CA", "-keyout", "root-ca.key", "-out", "root-ca.crt",
         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
        ["req", "-new", "-newkey", "rsa:3072", "-nodes", "-subj", "/CN=" + args.hostname,
         "-keyout", "server.key", "-out", "server.csr"],
        ["x509", "-req", "-in", "server.csr", "-CA", "root-ca.crt", "-CAkey", "root-ca.key",
         "-CAcreateserial", "-out", "server.crt", "-days", "365", "-sha256", "-extfile", "server.ext"],
    ]
    (secret_dir / "server.ext").write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1," + extra_san + "\n")
    for command in commands:
        subprocess.run(["openssl", *command], cwd=secret_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    # Compose bind-mounts these files into containers with different UIDs.
    # The parent remains 0700; only the selected files are readable inside their container.
    for name in ("server.crt", "server.key", "root-ca.crt"):
        (secret_dir / name).chmod(0o644)
    (ROOT / ".env").write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
    (secret_dir / "initial-admin.txt").write_text("username=admin\npassword=" + admin_password + "\n")
    print("Configured " + settings["PLATFORM_URL"])
    print("Initial credentials: deployment/secrets/initial-admin.txt (first login requires password change)")
    print("Trust deployment/secrets/root-ca.crt on browser and HMI computers before use.")


if __name__ == "__main__":
    main()
