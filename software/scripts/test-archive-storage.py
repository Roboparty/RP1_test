#!/usr/bin/env python3
"""Restore a synthetic central archive into a fresh disposable Docker volume."""
import argparse
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from archive_backup import restore_archive_snapshot, write_archive_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="rp1-archive-qa:local")
    args = parser.parse_args()
    token = uuid4().hex[:12]
    source_volume = "rp1-storage-qa-" + token
    target_project = "rp1-restore-qa-" + token
    container = "rp1-storage-qa-" + token
    source_created = container_created = False
    try:
        subprocess.run(["docker", "volume", "create", "--label", "rp1.disposable=storage-test", source_volume], check=True, stdout=subprocess.DEVNULL)
        source_created = True
        script = (
            "from pathlib import Path; "
            "p=Path('/var/lib/rp1/archives/synthetic-run'); p.mkdir(); "
            "(p/'raw.csv').write_text('time_s,joint_pos_rad\\n0,1\\n1,2\\n'); "
            "(p/'manifest.json').write_text('{\"synthetic\":true}'); "
            "(p/'raw.csv').chmod(0o440)"
        )
        subprocess.run(["docker", "create", "--name", container, "--network", "none", "--mount",
                        f"type=volume,src={source_volume},dst=/var/lib/rp1/archives", args.image, "python", "-c", script],
                       check=True, stdout=subprocess.DEVNULL)
        container_created = True
        subprocess.run(["docker", "start", "--attach", container], check=True, stdout=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory(prefix="rp1-archive-snapshot-qa-") as directory:
            backup = Path(directory)
            write_archive_snapshot(backup, container, args.image)
            restore_archive_snapshot(backup, target_project, args.image)
            check = (
                "from pathlib import Path; "
                "p=Path('/archive/synthetic-run/raw.csv'); "
                "assert p.read_text()=='time_s,joint_pos_rad\\n0,1\\n1,2\\n'; "
                "assert p.stat().st_uid==10001; assert p.stat().st_mode & 0o777==0o440; "
                "assert (p.parent/'manifest.json').read_text()=='{\"synthetic\":true}'"
            )
            subprocess.run(["docker", "run", "--rm", "--network", "none", "--mount",
                            f"type=volume,src={target_project}_archive-data,dst=/archive,readonly", args.image, "python", "-c", check], check=True)
            try:
                restore_archive_snapshot(backup, target_project, args.image)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Existing central archive volume was not protected")
        print("PASS synthetic archive snapshot/restore, ownership, read-only evidence and existing-volume protection")
    finally:
        if container_created:
            subprocess.run(["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL)
        if source_created:
            subprocess.run(["docker", "volume", "rm", source_volume], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["docker", "volume", "rm", target_project + "_archive-data"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
