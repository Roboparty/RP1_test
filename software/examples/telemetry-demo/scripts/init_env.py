"""Generate a private local .env once. Never overwrite a running stack's secrets."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

SOFTWARE = Path(__file__).resolve().parents[1]
SECRETS = {
    "POSTGRES_PASSWORD",
    "RP1_APP_PASSWORD",
    "RP1_READONLY_PASSWORD",
    "SEED_ADMIN_PASSWORD",
    "SERVICE_API_KEY_HMAC_SECRET",
    "INFLUX_TOKEN",
    "INFLUX_ADMIN_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
}


def initialize(output: Path) -> bool:
    runtime = output.parent / ".runtime"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.exists():
        return False
    lines = []
    for line in (SOFTWARE / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            if key in SECRETS:
                value = secrets.token_hex(32)
            elif key == "LOCAL_UID":
                value = str(os.getuid()) if hasattr(os, "getuid") else "1000"
            elif key == "LOCAL_GID":
                value = str(os.getgid()) if hasattr(os, "getgid") else "1000"
            line = key + "=" + value
        lines.append(line)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SOFTWARE / ".env")
    args = parser.parse_args()
    created = initialize(args.output.resolve())
    print(
        "Local configuration created."
        if created
        else "Existing configuration preserved."
    )


if __name__ == "__main__":
    main()
