#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'

if [[ -n "${RP1_MOTORS_SDK_ROOT:-}" ]]; then
  test -d "${RP1_MOTORS_SDK_ROOT}" || {
    echo "RP1_MOTORS_SDK_ROOT is not a directory: ${RP1_MOTORS_SDK_ROOT}" >&2
    exit 2
  }
  PYTHONPATH=src .venv/bin/python -c \
    'from rp1_test.adapters.roboparty_motors import import_motors_py; import_motors_py()'
fi

if [[ -n "${RP1_DESCRIPTION_ROOT:-}" ]]; then
  test -f "${RP1_DESCRIPTION_ROOT}/mjcf/rp1_flat.xml" \
    || test -f "${RP1_DESCRIPTION_ROOT}/mjcf/robot.xml" \
    || {
      echo "RP1_DESCRIPTION_ROOT has no supported MJCF model" >&2
      exit 2
    }
fi

echo "Ready. Run: .venv/bin/rp1-test --help"
echo "Optional MuJoCo: .venv/bin/python -m pip install -e '.[sim,test]'"
echo "External SDK/model provenance: docs/dependencies.md"
