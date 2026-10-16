#!/usr/bin/env python3
"""Load the native motor SDK in a clean process; never construct drivers or buses."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
PROBE = r'''
import hashlib, json, sys
from pathlib import Path
runtime, source = (Path(value).resolve() for value in sys.argv[1:3])
sys.path[:0] = [str(source), str(runtime)]
# Match gateway import order, including NumPy, without opening a transport.
from factory_hmi.core.backend import MotorsPyBackend
import motors_py
assert Path(motors_py.__file__).resolve().parent == runtime
required = ['create_motor', 'init_motor', 'deinit_motor', 'get_response_count',
            'get_feedback_count', 'refresh_motor_status', 'set_motor_control_mode',
            'motor_mit_cmd', 'motors_mit_cmd']
for name in required:
    assert hasattr(motors_py.MotorDriver, name), name
assert hasattr(motors_py.MotorControlMode, 'MIT')
loaded = set()
for line in Path('/proc/self/maps').read_text().splitlines():
    fields = line.split(maxsplit=5)
    if len(fields) == 6 and fields[5].startswith('/'):
        loaded.add(Path(fields[5]).resolve())
libraries = {}
for name in ['libspdlog.so.1.12', 'libfmt.so.9']:
    path = runtime / name
    assert path.resolve() in loaded, f'Native library was loaded outside runtime: {name}'
    libraries[name] = hashlib.sha256(path.read_bytes()).hexdigest()
libraries[Path(motors_py.__file__).name] = hashlib.sha256(Path(motors_py.__file__).read_bytes()).hexdigest()
print(json.dumps(dict(ok=True, runtime=str(runtime), libraries_sha256=libraries,
                     required_methods=required, drivers_created=0, hardware_actions=[])))
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir', type=Path, required=True)
    parser.add_argument('--provenance', type=Path, default=SOURCE / 'packaging/native-sdk-runtime.json')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    runtime = args.runtime_dir.expanduser().resolve(strict=True)
    env = dict(os.environ, LD_LIBRARY_PATH=str(runtime))
    env.pop('PYTHONPATH', None)
    env.pop('LD_PRELOAD', None)
    result = subprocess.run([sys.executable, '-I', '-c', PROBE, str(runtime), str(SOURCE)],
                            env=env, capture_output=True, text=True, timeout=20)
    if result.returncode:
        print('Native motor SDK loading failed before any driver was constructed:', file=sys.stderr)
        print(result.stderr.strip(), file=sys.stderr)
        return 1
    report = json.loads(result.stdout)
    provenance = args.provenance.expanduser()
    if provenance.exists():
        metadata = json.loads(provenance.read_text())
        if metadata['libraries_sha256'] != report['libraries_sha256']:
            raise RuntimeError('Native runtime differs from the recorded source build; rebuild SDK provenance')
        if (SOURCE / 'motors').is_dir():
            import hashlib
            for name, sha in metadata['sdk_source_files_sha256'].items():
                if hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() != sha:
                    raise RuntimeError(f'SDK source changed; rebuild native SDK: {name}')
        report['source_build_provenance_checked'] = True
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
