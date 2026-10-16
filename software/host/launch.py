#!/usr/bin/env python3
"""Run this checkout or its local deb using relative YAML references."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import yaml

PROGRAM_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PROGRAM_ROOT / "hmi" if (PROGRAM_ROOT / "hmi").is_dir() else PROGRAM_ROOT
stopping = False


def process_start(pid: int) -> str:
    value = Path(f"/proc/{pid}/stat").read_text()
    return value[value.rfind(")") + 2:].split()[19]


def running(state: Path) -> dict:
    try:
        data = json.loads(state.read_text())
        if data["start"] != process_start(int(data["pid"])):
            return {}
        args = Path(f'/proc/{data["pid"]}/cmdline').read_bytes().split(b"\0")
        if str(Path(__file__).resolve()).encode() not in args:
            return {}
        return data
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return {}


def request_stop(_signum=None, _frame=None):
    global stopping
    stopping = True


def seed_configs(root: Path, *, offline: bool, offline_access: str) -> None:
    for directory in ("run", "logs", "data", "config/stations", "data/trajectories"):
        (root / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    seeds = {
        "config/plc_cabinet.yaml": SOURCE_ROOT / "factory_hmi/config/plc_cabinet.yaml",
        "config/reliability_platform.yaml": SOURCE_ROOT / "factory_hmi/config/reliability_platform.yaml",
        "config/can-policy.yaml": SOURCE_ROOT / "packaging/local-can-policy.yaml",
        "config/can-disabled": SOURCE_ROOT / "packaging/can-disabled",
        "config/platform-ca.crt": SOURCE_ROOT / "packaging/platform-ca.crt",
    }
    seeds.update({"config/stations/" + p.name: p for p in (SOURCE_ROOT / "scripts/config").glob("*.yaml")})
    for relative, source in seeds.items():
        destination = root / relative
        if not destination.exists() and source.is_file():
            shutil.copy2(source, destination)
    (root / "config/can-disabled").chmod(0o700)
    if offline:
        # Always replace the private test profile from this checkout. Never use
        # a persisted real-PLC profile, even if someone edited the test state.
        config = yaml.safe_load(seeds["config/plc_cabinet.yaml"].read_text())
        config.update(driver="mock", access_mode=offline_access)
        (root / "config/plc_cabinet.yaml").write_text(yaml.safe_dump(config, sort_keys=False))


def gateway_environment(root: Path, runtime: Path | None, *, offline: bool, driver: str, mode: str) -> dict:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("RP1_FACTORY_", "RP1_RELIABILITY_"))}
    env.pop("PYTHONPATH", None)
    env.update({
        "RP1_FACTORY_DATA_ROOT": "data",
        "RP1_FACTORY_ALLOWED_CONFIG_ROOT": "config/stations",
        "RP1_FACTORY_ALLOWED_TRAJECTORY_ROOT": "data/trajectories",
        "RP1_FACTORY_PLC_CONFIG": "config/plc_cabinet.yaml",
        "RP1_FACTORY_CAN_POLICY": "config/can-policy.yaml" if offline else "/etc/rp1-test-hmi/can-policy.yaml",
        "RP1_FACTORY_CAN_HELPER_PATH": str(root / "config/can-disabled") if offline else "/usr/lib/rp1-test-hmi/factory-hmi-can-helper",
        "RP1_FACTORY_CAN_DISABLED": "1" if offline else "0",
        "RP1_FACTORY_PLATFORM_URL": "" if offline else "https://localhost:8443",
        "RP1_FACTORY_PLATFORM_MODE": "reliability_v1",
        "RP1_FACTORY_STATION_ID": f"RP1-{socket.gethostname()}",
        "RP1_FACTORY_STATION_NAME": f"本机 · {'PLC 模拟' if driver == 'mock' else 'PLC 实机'} · {mode} · {socket.gethostname()}",
        "RP1_FACTORY_BENCH_ID": "",
        "RP1_RELIABILITY_PROFILES": "config/reliability_platform.yaml",
        "PYTHONPATH": str(SOURCE_ROOT),
    })
    ca = root / "config/platform-ca.crt"
    if ca.is_file():
        env["SSL_CERT_FILE"] = str(ca)
    if runtime is not None:
        env["PYTHONPATH"] += os.pathsep + str(runtime)
        library_paths = [runtime]
        qt_libraries = runtime / "PySide6/Qt/lib"
        if qt_libraries.is_dir():
            library_paths.append(qt_libraries)
        env["LD_LIBRARY_PATH"] = os.pathsep.join(str(path) for path in library_paths)
        env["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(runtime / "PySide6/Qt/plugins")
    return env


def wait_health(process: subprocess.Popen, url: str, driver: str, mode: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and process.poll() is None and not stopping:
        try:
            with urlopen(url + "/api/v1/health", timeout=1) as response:
                health = json.load(response)
            with urlopen(url + "/api/v1/plc/snapshot", timeout=1) as response:
                plc = json.load(response)
            if health.get("ok") and plc.get("driver") == driver and plc.get("access_mode") == mode:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("网关启动失败，请检查用户状态目录 logs/gateway.log")


def finish(process: subprocess.Popen | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("start", "stop", "status"), default="start")
    parser.add_argument("--runtime-dir", type=Path, help="Optional offline Python 3.12 release runtime")
    parser.add_argument("--state-dir", type=Path, help="Writable state root; defaults to XDG_STATE_HOME/rp1-test-hmi")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--gateway-only", action="store_true")
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--offline-check", action="store_true", help="Dedicated mock PLC; no hardware or platform connection")
    parser.add_argument("--offline-access", choices=("control", "monitor"), default="control")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise RuntimeError("gateway port must be in [1024, 65535]")
    state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))).expanduser()
    if not state_home.is_absolute():
        raise RuntimeError("XDG_STATE_HOME must be absolute")
    # Offline mode has a separate child even when --state-dir is explicit.
    root = (args.state_dir or state_home / "rp1-test-hmi").expanduser().resolve()
    if args.offline_check:
        root /= "offline-check"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "run").mkdir(exist_ok=True, mode=0o700)
    state = root / "run/launcher.json"
    data = running(state)
    if args.action == "status":
        print(json.dumps({"running": bool(data), "state_root": str(root), **data}, ensure_ascii=False))
        return 0
    if args.action == "stop":
        if data:
            os.kill(int(data["pid"]), signal.SIGTERM)
            for _ in range(75):
                if not running(state):
                    print("HMI and its own gateway stopped.")
                    return 0
                time.sleep(0.2)
            raise RuntimeError("Stop still in progress; see logs.")
        print("HMI is already stopped.")
        return 0
    runtime = args.runtime_dir
    if runtime is None:
        for candidate in (PROGRAM_ROOT / "runtime", SOURCE_ROOT / ".local-runtime"):
            if candidate.is_dir():
                runtime = candidate
                break
    if runtime is not None:
        runtime = runtime.expanduser().resolve(strict=True)
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("Bundled release runtime requires Python 3.12")
    os.umask(0o077)
    with (root / "run/launcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("HMI is already running.")
            return 0
        seed_configs(root, offline=args.offline_check, offline_access=args.offline_access)
        config = yaml.safe_load((root / "config/plc_cabinet.yaml").read_text())
        driver, mode = config.get("driver"), config.get("access_mode")
        if driver not in {"mock", "modbus_tcp"} or mode not in {"monitor", "control"}:
            raise RuntimeError("Explicit PLC driver and access_mode are required")
        if args.offline_check and driver != "mock":
            raise RuntimeError("Offline validation must use mock PLC")
        with socket.socket() as check:
            check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            check.bind(("127.0.0.1", args.port))
        env = gateway_environment(root, runtime, offline=args.offline_check, driver=driver, mode=mode)
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        gateway = desktop = None
        url = f"http://127.0.0.1:{args.port}"
        with (root / "logs/gateway.log").open("a") as glog, (root / "logs/desktop.log").open("a") as dlog:
            try:
                gateway = subprocess.Popen([sys.executable, "-m", "factory_hmi.gateway", "--host", "127.0.0.1", "--port", str(args.port), "--data-root", "data"], cwd=root, env=env, stdout=glog, stderr=subprocess.STDOUT)
                state.write_text(json.dumps({"pid": os.getpid(), "start": process_start(os.getpid()), "gateway_pid": gateway.pid, "gateway": url, "driver": driver, "access_mode": mode, "read_only": mode == "monitor"}))
                wait_health(gateway, url, driver, mode)
                if not args.gateway_only:
                    desktop = subprocess.Popen([sys.executable, "-m", "factory_hmi.desktop", "--gateway-url", url], cwd=root, env=env, stdout=dlog, stderr=subprocess.STDOUT)
                print(f"HMI ready: PLC {driver}, {mode}, {url}", flush=True)
                deadline = time.monotonic() + args.duration if args.duration > 0 else None
                while not stopping:
                    if gateway.poll() is not None:
                        raise RuntimeError("Gateway exited unexpectedly; see logs.")
                    if desktop is not None and desktop.poll() is not None:
                        if desktop.returncode:
                            raise RuntimeError("Desktop exited unexpectedly; see logs.")
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    time.sleep(0.2)
            finally:
                finish(desktop)
                finish(gateway)
                state.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
