#!/usr/bin/env python3
"""Exercise the real delivery stack only in new /tmp projects with synthetic data.

Stages are explicit so a real browser can inspect the running stack between them.
No site .env, credentials, runtime CSV, current containers or volumes are reused.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
from uuid import uuid4
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "rp1-archive-deploy-"
TREES = ("service/app", "service/scripts", "service/tests", "frontend/src", "frontend/public", "frontend/scripts",
         "database/alembic", "database/sql", "database/imports", "database/tests", "database/docker-entrypoint-initdb.d",
         "host/factory_hmi", "host/scripts/config")
FILES = ("VERSION", "compose.yaml", "service/Dockerfile", "service/.dockerignore", "service/requirements.txt", "service/requirements.lock",
         "frontend/Dockerfile", "frontend/.dockerignore", "frontend/nginx.conf", "frontend/package.json", "frontend/package-lock.json",
         "frontend/index.html", "frontend/vite.config.ts", "frontend/tsconfig.json", "frontend/tsconfig.app.json", "frontend/tsconfig.node.json",
         "database/Dockerfile", "database/.dockerignore", "database/requirements.txt", "database/requirements.lock", "database/alembic.ini",
         "scripts/compose.sh", "scripts/up.sh", "scripts/verify.py", "scripts/backup.py", "scripts/restore.py", "scripts/archive_backup.py")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def run(command, log, **kwargs):
    # Logs contain subprocess output, never command arguments or environment values.
    with log.open("ab") as handle:
        subprocess.run(command, check=True, stdout=handle, stderr=subprocess.STDOUT, **kwargs)


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def copy_candidate(destination):
    destination.mkdir()
    for relative in TREES:
        source = ROOT / relative
        shutil.copytree(source, destination / relative, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
    for relative in FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    # FactoryService imports these support modules even for the fake backend.
    # Copy Python source only; scripts may also contain local trajectories/data.
    for source in sorted((ROOT / "host/scripts").rglob("*.py")):
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def fingerprint(stage, output):
    entries = {str(path.relative_to(stage)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted(stage.rglob("*")) if path.is_file()}
    write_json(output, entries)
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def create_tls(stage, log):
    directory = stage / "deployment/secrets"
    directory.mkdir(parents=True, mode=0o700)
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=RP1 disposable archive QA CA",
         "-keyout", str(directory / "root-ca.key"), "-out", str(directory / "root-ca.crt")], log)
    run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
         "-keyout", str(directory / "server.key"), "-out", str(directory / "server.csr")], log)
    extensions = directory / "server.ext"
    extensions.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")
    run(["openssl", "x509", "-req", "-days", "2", "-in", str(directory / "server.csr"), "-CA", str(directory / "root-ca.crt"),
         "-CAkey", str(directory / "root-ca.key"), "-CAcreateserial", "-extfile", str(extensions), "-out", str(directory / "server.crt")], log)
    (directory / "root-ca.key").chmod(0o600)
    # Docker bind-backed secrets preserve file modes. The 0700 parent restricts
    # host traversal while nginx's non-root UID can read its mounted key file.
    (directory / "server.key").chmod(0o644)
    for public_file in ("root-ca.crt", "server.crt"):
        (directory / public_file).chmod(0o644)


def validate_owned_state(directory, state):
    """Reject edited state before opening environment files or invoking Docker."""
    directory = directory.resolve(strict=True)
    if directory.parent != Path(tempfile.gettempdir()).resolve() or not directory.name.startswith(PREFIX):
        raise ValueError("Only an owned disposable /tmp run directory is accepted")
    if state["run_dir"] != str(directory) or not re.fullmatch(PREFIX + r"[0-9a-f]{12}", state["project"]):
        raise ValueError("Disposable run identity mismatch")
    if state.get("restore_project", state["project"] + "-restore") != state["project"] + "-restore":
        raise ValueError("Disposable restore identity mismatch")
    if state.get("browser_container", state["project"] + "-browser") != state["project"] + "-browser":
        raise ValueError("Disposable browser identity mismatch")
    for key in ("stage", "restore_stage"):
        if key not in state:
            continue
        stage = Path(state[key]).resolve(strict=True)
        allowed_name = stage.name == "restored" if key == "restore_stage" else bool(re.fullmatch(r"candidate(?:-v[1-9][0-9]*)?", stage.name))
        if stage.parent != directory or not allowed_name:
            raise ValueError("Candidate path is outside the owned run directory")
        environment = stage / ".env"
        if environment.is_symlink():
            raise ValueError("Candidate environment cannot be a symlink")
        if not environment.exists() and key == "restore_stage":
            continue
        expected = state["restore_project"] if key == "restore_stage" else state["project"]
        projects = [line.partition("=")[2] for line in environment.read_text().splitlines() if line.startswith("COMPOSE_PROJECT_NAME=")]
        if projects != [expected]:
            raise ValueError("Candidate environment project does not match owned state")


def load_state(directory):
    directory = directory.resolve(strict=True)
    if directory.parent != Path(tempfile.gettempdir()).resolve() or not directory.name.startswith(PREFIX):
        raise ValueError("Only an owned disposable /tmp run directory is accepted")
    state = json.loads((directory / "run.json").read_text())
    validate_owned_state(directory, state)
    return directory, state


def compose(stage, *arguments, capture=False):
    command = [str(stage / "scripts/compose.sh"), *arguments]
    return subprocess.run(command, cwd=stage, check=True, capture_output=capture, text=capture)


def start():
    os.umask(0o077)
    directory = Path(tempfile.mkdtemp(prefix=PREFIX))
    project = PREFIX + uuid4().hex[:12]
    stage = directory / "candidate"
    copy_candidate(stage)
    digest = fingerprint(stage, directory / "candidate-files.json")
    port = available_port()
    admin_password = secrets.token_urlsafe(30)
    settings = {
        "COMPOSE_PROJECT_NAME": project, "RP1_VERSION": "archive-qa-" + digest[:12],
        "POSTGRES_DB": "rp1_archive_qa", "POSTGRES_USER": "qa_admin", "POSTGRES_PASSWORD": secrets.token_urlsafe(30),
        "RP1_APP_USER": "rp1_app", "RP1_APP_PASSWORD": secrets.token_urlsafe(30),
        "RP1_READONLY_USER": "rp1_readonly", "RP1_READONLY_PASSWORD": secrets.token_urlsafe(30),
        "SERVICE_API_KEY_HMAC_SECRET": secrets.token_hex(32), "SEED_ADMIN_USERNAME": "archive-qa-admin", "SEED_ADMIN_PASSWORD": admin_password,
        "WEB_BIND_ADDRESS": "127.0.0.1", "WEB_PORT": str(port), "PLATFORM_URL": f"https://localhost:{port}", "RP1_ARCHIVE_VOLUME": "archive-data",
    }
    (stage / ".env").write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
    create_tls(stage, directory / "tls.log")
    state = {"project": project, "run_dir": str(directory), "stage": str(stage), "base_url": settings["PLATFORM_URL"],
             "candidate_sha256": digest, "source_version": (ROOT / "VERSION").read_text().strip(), "status": "STARTING"}
    write_json(directory / "run.json", state)
    write_json(directory / "settings.json", {"admin_username": settings["SEED_ADMIN_USERNAME"], "admin_password": admin_password,
        "ca_file": str(stage / "deployment/secrets/root-ca.crt"), "scratch_dir": str(directory), "platform_url": settings["PLATFORM_URL"]})
    print("Disposable run: " + str(directory), flush=True)
    print("Loopback HTTPS: " + state["base_url"], flush=True)
    try:
        run([str(stage / "scripts/up.sh")], directory / "startup.log", cwd=stage)
        state["status"] = "RUNNING"
        write_json(directory / "run.json", state)
        inspect(directory, state)
    except BaseException:
        cleanup(directory, state)
        raise
    return directory, state


def inspect(directory, state, restored=False):
    stage = Path(state["restore_stage"] if restored else state["stage"])
    project = state["restore_project"] if restored else state["project"]
    # Compose supports either a JSON array or one object per line across versions.
    raw = compose(stage, "ps", "--all", "--format", "json", capture=True).stdout.strip()
    services = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines() if line]
    assert {row["Service"] for row in services} >= {"postgres", "api", "worker", "archive-worker", "web"}, "Missing delivery service"
    assert all(row["State"] == "running" and row.get("Health") == "healthy" for row in services if row["Service"] != "migrate"), "Stack not healthy"
    ids = {service: compose(stage, "ps", "--all", "-q", service, capture=True).stdout.strip() for service in ("api", "archive-worker")}
    mounts = {}
    for service, identity in ids.items():
        data = json.loads(subprocess.check_output(["docker", "inspect", identity], text=True))[0]
        assert data["Config"]["Labels"]["com.docker.compose.project"] == project
        mount = next(item for item in data["Mounts"] if item["Destination"] == "/var/lib/rp1/archives")
        assert mount["Type"] == "volume" and mount["Name"] == project + "_archive-data"
        mounts[service] = {"type": mount["Type"], "name": mount["Name"], "destination": mount["Destination"], "rw": mount["RW"]}
    schema = compose(stage, "exec", "-T", "postgres", "psql", "-U", "qa_admin", "-d", "rp1_archive_qa", "-Atc", "SELECT version_num FROM alembic_version", capture=True).stdout.strip()
    # Read the frozen candidate's actual Alembic head, including added modules.
    heads = compose(stage, "run", "--rm", "--no-deps", "migrate", "alembic", "heads", capture=True).stdout
    assert schema == heads.split()[0], (schema, heads)
    write_json(directory / ("stack-health-restored.json" if restored else "stack-health.json"), {"services": [{key: row.get(key) for key in ("Service", "State", "Health", "Image", "Publishers")} for row in services], "archive_mounts": mounts, "schema": schema})
    if state["status"] == "STARTING":
        state["status"] = "RUNNING"
        write_json(directory / "run.json", state)


def exercise(directory, state, phase="seed", installed=False):
    script = ROOT / "scripts/check-archive-deployment.py"
    if not script.is_file():
        raise RuntimeError("HTTPS acceptance driver is not ready")
    # Freeze the executed driver alongside the candidate, not secret configuration.
    target = Path(state["stage"]) / "scripts/check-archive-deployment.py"
    shutil.copy2(script, target)
    versions = json.loads((directory / "driver-versions.json").read_text()) if (directory / "driver-versions.json").exists() else []
    versions.append({"phase": phase, "installed": installed, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    write_json(directory / "driver-versions.json", versions)
    driver_stage = Path(state["restore_stage"] if phase == "verify-restored" else state["stage"])
    web_container = compose(driver_stage, "ps", "-q", "web", capture=True).stdout.strip()
    if not web_container:
        raise RuntimeError("Owned isolated HTTPS container is missing")
    command = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "--network", "container:" + web_container, "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "-v", f"{directory}:{directory}"]
    if installed:
        command += ["-e", "PYTHONPATH=/opt/rp1-test-hmi/hmi:/opt/rp1-test-hmi/runtime", "-e", "LD_LIBRARY_PATH=/opt/rp1-test-hmi/runtime",
                    state["hmi_image"], "/usr/bin/python3", "-c",
                    "import factory_hmi,runpy,sys; from pathlib import Path; "
                    "assert Path(factory_hmi.__file__).is_relative_to('/opt/rp1-test-hmi/hmi'); "
                    "print('Installed HMI source: '+factory_hmi.__file__); "
                    "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"]
        settings_file = directory / "installed/settings.json"
    else:
        command += ["-e", "PYTHONPATH=/hmi", "-v", f"{Path(state['stage']) / 'host'}:/hmi:ro", "rp1-archive-qa:local", "python"]
        settings_file = directory / "settings.json"
    command += [str(target), "--base-url", "https://localhost:8443", "--settings-file", str(settings_file), "--phase", "verify" if phase == "verify-restored" else phase]
    prefix = "installed-" if installed else ""
    run(command, directory / (prefix + phase + ".log"))
    print("PASS deployed HTTPS " + prefix + phase, flush=True)


def exercise_workflow(directory, state):
    stage = Path(state["stage"])
    log = directory / "queue-restart.log"
    run([str(stage / "scripts/compose.sh"), "stop", "archive-worker"], log, cwd=stage)
    exercise(directory, state, "queue")
    run([str(stage / "scripts/compose.sh"), "restart", "api"], log, cwd=stage)
    run([str(stage / "scripts/compose.sh"), "up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", "api"], log, cwd=stage)
    exercise(directory, state, "resume")
    run([str(stage / "scripts/compose.sh"), "up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", "archive-worker"], log, cwd=stage)
    exercise(directory, state, "seed")
    inspect(directory, state)
    raw_evidence(directory, state)


def browser_entry(directory, state):
    """Optional loopback-only UI entry; verified HTTPS remains the real delivery."""
    stage = Path(state["stage"])
    source = (stage / "frontend/nginx.conf").read_text().split("server {\n    listen 8080;", 1)[0]
    source = source.replace("listen 8443 ssl;", "listen 8080;")
    source = "\n".join(line for line in source.splitlines() if "ssl_certificate" not in line and "ssl_protocols" not in line) + "\n"
    configuration = directory / "browser-nginx.conf"
    configuration.write_text(source)
    configuration.chmod(0o644)
    web = compose(stage, "ps", "-q", "web", capture=True).stdout.strip()
    image = subprocess.check_output(["docker", "inspect", "--format", "{{.Config.Image}}", web], text=True).strip()
    port = urlsplit(state["browser_url"]).port if state.get("browser_url") else available_port()
    name = state["project"] + "-browser"
    if state.get("browser_container"):
        label = subprocess.check_output(["docker", "inspect", "--format", '{{index .Config.Labels "rp1.archive.disposable"}}', name], text=True).strip()
        if label != state["project"]:
            raise RuntimeError("Browser container ownership mismatch")
        run(["docker", "rm", "-f", name], directory / "browser-start.log")
    run(["docker", "run", "-d", "--name", name, "--label", "rp1.archive.disposable=" + state["project"],
         "--network", state["project"] + "_default", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
         "-p", f"127.0.0.1:{port}:8080", "-v", f"{configuration}:/etc/nginx/conf.d/default.conf:ro", image], directory / "browser-start.log")
    state["browser_container"] = name
    state["browser_url"] = f"http://localhost:{port}"
    write_json(directory / "run.json", state)
    print("Loopback-only browser UI: " + state["browser_url"])


def refresh(directory, state):
    """Record a successor source snapshot after an acceptance finding is fixed."""
    old = Path(state["stage"])
    generation = int(state.get("generation", 1)) + 1
    stage = directory / f"candidate-v{generation}"
    copy_candidate(stage)
    digest = fingerprint(stage, directory / f"candidate-files-v{generation}.json")
    settings = (old / ".env").read_text().splitlines()
    (stage / ".env").write_text("\n".join("RP1_VERSION=archive-qa-" + digest[:12] if line.startswith("RP1_VERSION=") else line for line in settings) + "\n")
    shutil.copytree(old / "deployment/secrets", stage / "deployment/secrets")
    state.setdefault("candidate_history", []).append({"stage": str(old), "sha256": state["candidate_sha256"]})
    state.update(stage=str(stage), candidate_sha256=digest, generation=generation)
    write_json(directory / "run.json", state)
    run([str(stage / "scripts/up.sh")], directory / f"startup-v{generation}.log", cwd=stage)
    inspect(directory, state)
    if state.get("browser_container"):
        browser_entry(directory, state)
    exercise(directory, state, "verify")
    if state.get("hmi_image"):
        exercise(directory, state, "verify", installed=True)
    print("PASS successor candidate " + digest, flush=True)


def raw_evidence(directory, state, restored=False):
    stage = Path(state["restore_stage"] if restored else state["stage"])
    records = json.loads((directory / "deployment-result.json").read_text())["records"]
    values = list(records.values())
    if (directory / "installed/deployment-result.json").is_file():
        values += list(json.loads((directory / "installed/deployment-result.json").read_text())["records"].values())
    expected = [{key: item[key] for key in ("execution_uuid", "sha256", "size_bytes")} for item in values]
    script = """import hashlib,json,stat
from pathlib import Path
expected = json.loads(input())
results=[]
for item in expected:
    path=Path('/var/lib/rp1/archives')/item['execution_uuid']/'raw.csv'
    info=path.stat()
    with path.open('rb') as handle:
        digest=hashlib.file_digest(handle,'sha256').hexdigest()
    assert digest==item['sha256'] and info.st_size==item['size_bytes']
    assert stat.S_IMODE(info.st_mode)==0o440 and info.st_uid==10001
    results.append(dict(item,mode=oct(stat.S_IMODE(info.st_mode)),uid=info.st_uid))
print(json.dumps(results))
"""
    command = [str(stage / "scripts/compose.sh"), "exec", "-T", "api", "python", "-c", script]
    receipt = subprocess.run(command, input=json.dumps(expected) + "\n", capture_output=True, text=True, check=True, cwd=stage)
    write_json(directory / ("raw-restored.json" if restored else "raw-original.json"), json.loads(receipt.stdout))


def recover(directory, state):
    stage = Path(state["stage"])
    run([str(stage / "scripts/compose.sh"), "restart", "api", "archive-worker", "web"], directory / "restart.log", cwd=stage)
    run([str(stage / "scripts/compose.sh"), "up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", "api", "archive-worker", "web"], directory / "restart.log", cwd=stage)
    run(["python3", str(stage / "scripts/verify.py")], directory / "restart.log", cwd=stage)
    exercise(directory, state, "verify")
    if state.get("hmi_image"):
        exercise(directory, state, "verify", installed=True)
    backup = directory / "backup"
    run(["python3", str(stage / "scripts/backup.py"), "--maintenance", "--output", str(backup)], directory / "backup.log", cwd=stage)
    clone = directory / "restored"
    shutil.copytree(stage, clone, ignore=shutil.ignore_patterns(".env", "secrets", "__pycache__"))
    state["restore_project"] = state["project"] + "-restore"
    port = available_port()
    state["restore_stage"] = str(clone)
    state["restore_url"] = f"https://localhost:{port}"
    write_json(directory / "run.json", state)
    run(["python3", str(clone / "scripts/restore.py"), str(backup), "--project", state["restore_project"], "--port", str(port)], directory / "restore.log", cwd=clone)
    inspect(directory, state, restored=True)
    exercise(directory, state, "verify-restored")
    if state.get("hmi_image"):
        exercise(directory, state, "verify-restored", installed=True)
    raw_evidence(directory, state, restored=True)
    # The original project remains independently queryable after restore.
    exercise(directory, state, "verify")
    if state.get("hmi_image"):
        exercise(directory, state, "verify", installed=True)
    raw_evidence(directory, state)
    inspect(directory, state)
    state["status"] = "RECOVERY_PASSED"
    write_json(directory / "run.json", state)
    print("PASS real restart and delivery backup/restore", flush=True)


def cleanup(directory, state):
    validate_owned_state(directory, state)
    failures = []
    if state.get("browser_container"):
        try:
            name = state["browser_container"]
            label = subprocess.check_output(["docker", "inspect", "--format", '{{index .Config.Labels "rp1.archive.disposable"}}', name], text=True).strip()
            if label != state["project"]:
                raise RuntimeError("Browser container ownership mismatch")
            run(["docker", "rm", "-f", name], directory / "cleanup.log")
            state.pop("browser_container")
        except subprocess.CalledProcessError:
            failures.append("browser")
    for key in ("restore_stage", "stage"):
        if key in state:
            stage = Path(state[key])
            if not (stage / ".env").is_file():
                continue
            try:
                run([str(stage / "scripts/compose.sh"), "down", "-v", "--remove-orphans"], directory / "cleanup.log", cwd=stage)
            except subprocess.CalledProcessError:
                failures.append(key)
    if failures:
        raise RuntimeError("Disposable cleanup failed for: " + ", ".join(failures))
    state["status"] = "CLEANED"
    write_json(directory / "run.json", state)
    print("Disposable projects removed; source, evidence and private QA settings retained in " + str(directory))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("all", "start", "exercise", "package", "browser", "refresh", "recover", "inspect", "cleanup"), default="all")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--hmi-image", help="Clean Ubuntu image with the candidate HMI installed; required for package phase")
    parser.add_argument("--keep", action="store_true", help="Keep owned synthetic services available for browser verification")
    args = parser.parse_args()
    if args.phase in {"start", "all"}:
        if args.run_dir:
            parser.error("start always creates a fresh unpredictable directory")
        directory, state = start()
    else:
        if not args.run_dir:
            parser.error("--run-dir is required for this phase")
        directory, state = load_state(args.run_dir)
    try:
        if args.phase in {"all", "exercise"}:
            exercise_workflow(directory, state)
        if args.phase == "browser":
            browser_entry(directory, state)
        if args.phase == "refresh":
            refresh(directory, state)
        if args.phase == "package":
            if not args.hmi_image:
                parser.error("--hmi-image is required for package phase")
            installed = directory / "installed"
            installed.mkdir(mode=0o700, exist_ok=True)
            settings = json.loads((directory / "settings.json").read_text())
            settings["scratch_dir"] = str(installed)
            write_json(installed / "settings.json", settings)
            state["hmi_image"] = args.hmi_image
            write_json(directory / "run.json", state)
            exercise(directory, state, "seed", installed=True)
            exercise(directory, state, "verify", installed=True)
            raw_evidence(directory, state)
        if args.phase in {"all", "recover"}:
            recover(directory, state)
        if args.phase == "inspect":
            inspect(directory, state)
        if args.phase == "cleanup":
            cleanup(directory, state)
    finally:
        if args.phase == "all" and not args.keep:
            cleanup(directory, state)


if __name__ == "__main__":
    main()
