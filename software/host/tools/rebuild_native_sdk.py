#!/usr/bin/env python3
"""Rebuild the desktop motor SDK with the matching Ubuntu 24.04 development libraries."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dependency-prefix', type=Path, default=Path('/usr'))
    parser.add_argument('--build-dir', type=Path, default=SOURCE / 'build/native-sdk')
    parser.add_argument('--runtime-dir', type=Path, default=SOURCE / '.local-runtime')
    parser.add_argument('--verify-concurrency', action='store_true', help='Build and run the offline fake-driver concurrency regression')
    args = parser.parse_args()
    prefix = args.dependency_prefix.resolve(strict=True)
    build = args.build_dir.resolve()
    runtime = args.runtime_dir.resolve(strict=True)
    cmake = prefix / 'bin/cmake'
    if not cmake.exists():
        cmake = Path(shutil.which('cmake') or '')
    if not cmake.is_file():
        raise RuntimeError('CMake and the matching development libraries are required')
    libroot = prefix / 'lib/x86_64-linux-gnu'
    names = ['libspdlog.so.1.12', 'libfmt.so.9']
    for name in names:
        (libroot / name).resolve(strict=True)
    paths = [p for p in (SOURCE / 'motors').rglob('*') if p.is_file()
             and (p.suffix in {'.cpp', '.hpp', '.h', '.cmake', '.xml'} or p.name == 'CMakeLists.txt')
             and not any(part in {'build', '__pycache__', '.git'} for part in p.parts)]
    source_hashes = {str(p.relative_to(SOURCE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    env = dict(os.environ, LD_LIBRARY_PATH=str(libroot))
    subprocess.run([str(cmake), '-S', str(SOURCE / 'motors'), '-B', str(build),
                    '-DCMAKE_BUILD_TYPE=Release', '-DRP1_NATIVE_OPTIMIZATION=OFF',
                    f'-DRP1_BUILD_SDK_TESTS={"ON" if args.verify_concurrency else "OFF"}',
                    f'-DCMAKE_PREFIX_PATH={prefix}', f'-DCMAKE_CXX_FLAGS=-I{prefix}/include',
                    '-DBoost_NO_BOOST_CMAKE=ON', f'-DBoost_ROOT={prefix}',
                    f'-DPython3_INCLUDE_DIR={prefix}/include/python3.12',
                    f'-DPython3_EXECUTABLE={sys.executable}',
                    '-DPython3_LIBRARY=/usr/lib/x86_64-linux-gnu/libpython3.12.so.1.0',
                    '-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON', '-DCMAKE_INSTALL_RPATH=$ORIGIN'],
                   env=env, check=True)
    subprocess.run([str(cmake), '--build', str(build), '--target', 'motors_py', '--parallel', '2'], env=env, check=True)
    assert all(hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() == sha for name, sha in source_hashes.items()), 'SDK source changed while compiling'
    if args.verify_concurrency:
        subprocess.run([str(cmake), '--build', str(build), '--target', 'motors_py_testing', '--parallel', '2'], env=env, check=True)
    module = next(build.glob('motors_py.cpython-312-*.so'))
    shutil.copy2(module, runtime / module.name)
    for name in names:
        shutil.copy2((libroot / name).resolve(), runtime / name)
    libraries = {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest() for name in [module.name, *names]}
    report = dict(sdk_source_files_sha256=source_hashes, libraries_sha256=libraries,
                  sdk_built_from='hmi/motors', python='3.12', spdlog='1.12', fmt='9',
                  native_optimization=False, runtime_search_path='$ORIGIN', hardware_actions=[])
    (SOURCE / 'packaging/native-sdk-runtime.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    subprocess.run([sys.executable, str(SOURCE / 'tools/verify_native_sdk.py'), '--runtime-dir', str(runtime)], check=True)
    if args.verify_concurrency:
        subprocess.run([sys.executable, str(SOURCE / 'tools/verify_native_concurrency.py'), '--module-dir', str(build), '--runtime-dir', str(runtime)], check=True)


if __name__ == '__main__':
    main()
