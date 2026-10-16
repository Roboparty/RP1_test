"""Central archive snapshots for maintenance backups; no site access on import."""
import json
from pathlib import Path
import re
import subprocess

ARCHIVE_DESTINATION = "/var/lib/rp1/archives"
ARCHIVE_SNAPSHOT = "archive.tar.gz"


def archive_mount_spec(mounts: list[dict]) -> str:
    matches = [mount for mount in mounts if mount.get("Destination") == ARCHIVE_DESTINATION]
    if len(matches) != 1:
        raise RuntimeError("API must have exactly one persistent central archive mount")
    mount = matches[0]
    kind = mount.get("Type")
    source = mount.get("Name") if kind == "volume" else mount.get("Source")
    if kind not in {"volume", "bind"} or not source or "," in source:
        raise RuntimeError("Unsupported archive mount; snapshot cannot be made safely")
    return f"type={kind},src={source},dst=/archive,readonly"


def write_archive_snapshot(output: Path, api_container: str, image: str) -> None:
    inspected = subprocess.check_output(
        ["docker", "inspect", "--format", "{{json .Mounts}}", api_container], text=True
    )
    mount = archive_mount_spec(json.loads(inspected))
    script = (
        "import sys,tarfile; "
        "archive=tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz'); "
        "archive.add('/archive',arcname='.'); archive.close()"
    )
    with (output / ARCHIVE_SNAPSHOT).open("xb") as handle:
        subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--user", "0", "--mount", mount,
             image, "python", "-c", script], check=True, stdout=handle
        )


def restore_archive_snapshot(backup: Path, project: str, image: str) -> None:
    """Always target a new project volume, never the source site's NAS/bind path."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise ValueError("Invalid restore project")
    volume = project + "_archive-data"
    existing = subprocess.check_output(["docker", "volume", "ls", "--format", "{{.Name}}"], text=True).splitlines()
    if volume in existing:
        raise RuntimeError("Archive restore target volume already exists")
    subprocess.run(["docker", "volume", "create", volume], check=True, stdout=subprocess.DEVNULL)
    snapshot = backup / ARCHIVE_SNAPSHOT
    script = "import os,sys,tarfile\n"
    if snapshot.exists():
        script += "tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz').extractall('/archive',filter='data')\n"
    script += """os.chown('/archive',10001,10001)
for root,dirs,files in os.walk('/archive'):
    for name in dirs+files:
        path=os.path.join(root,name)
        os.chown(path,10001,10001,follow_symlinks=False)
        if name=='raw.csv' and not os.path.islink(path):
            os.chmod(path,0o440)
"""
    command = ["docker", "run", "--rm", "-i", "--network", "none", "--user", "0", "--mount",
               f"type=volume,src={volume},dst=/archive", image, "python", "-c", script]
    if snapshot.exists():
        with snapshot.open("rb") as handle:
            subprocess.run(command, check=True, stdin=handle)
    else:
        subprocess.run(command, check=True, stdin=subprocess.DEVNULL)
