#!/usr/bin/env python3
"""Exercise production bindings with two native fake motors, without any bus."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

PROBE = r'''
import json, sys, threading, time
sys.path.insert(0, sys.argv[1])
import motors_py_testing as sdk
calibrating = sdk._test_create_motor()
controlling = sdk._test_create_motor()
ticks = []
ready = threading.Event()
stop = threading.Event()
result = []
errors = []
def control():
    while not stop.is_set():
        controlling.motor_mit_cmd(float(len(ticks)), 0., 1., 1., 0.)
        assert controlling.get_motor_pos() == float(len(ticks))
        ticks.append(time.monotonic())
        ready.set()
        stop.wait(.005)
def calibrate():
    try:
        result.append(calibrating.set_motor_zero())
    except BaseException as exc:
        errors.append(repr(exc))
thread = threading.Thread(target=control)
thread.start()
assert ready.wait(2)
time.sleep(.03)
start = time.monotonic()
worker = threading.Thread(target=calibrate)
worker.start()
worker.join(3)
elapsed = time.monotonic() - start
time.sleep(.03)
stop.set(); thread.join(2)
assert not worker.is_alive() and not thread.is_alive(), 'worker did not finish'
assert not errors and result == [True], errors
gaps = [b-a for a,b in zip(ticks,ticks[1:])]
max_gap = max(gaps)
during = sum(start <= tick <= start+elapsed for tick in ticks)
report = dict(elapsed_s=elapsed, independent_commands_during_calibration=during,
              max_command_gap_s=max_gap, fake_drivers=2, hardware_actions=[],
              ok=during >= 20 and max_gap < .2)
print(json.dumps(report))
assert report['ok'], 'other station control stalled during native calibration'
'''

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--module-dir', type=Path, required=True)
    p.add_argument('--runtime-dir', type=Path, required=True)
    a = p.parse_args()
    env = dict(os.environ, LD_LIBRARY_PATH=str(a.runtime_dir.resolve()))
    env.pop('PYTHONPATH', None)
    env.pop('LD_PRELOAD', None)
    subprocess.run([sys.executable, '-I', '-c', PROBE, str(a.module_dir.resolve())],
                   env=env, check=True, timeout=10)

if __name__ == '__main__':
    main()
