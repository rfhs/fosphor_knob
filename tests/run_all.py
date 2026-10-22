#!/usr/bin/env python3
"""Run every automated test: the host unit tests, then the hardware tests
for whichever knobs are plugged in.

    tests/run_all.py                    everything that can run here
    tests/run_all.py --knob waveshare   unit tests + the Waveshare knob only
    tests/run_all.py --knob griffin     unit tests + the Griffin only
    tests/run_all.py --no-hardware      unit tests only (what CI runs)

Waveshare knob: firmware/tools/selftest.py, tests/runpy_test.py and
tests/reconnect_test.py (about 30 s). Griffin: tests/griffin_test.py.
A knob that isn't plugged in is skipped, not failed, unless asked for with
--knob. Stop fosphor_knob.sh / run.py first: the run.py tests start their
own against a fake flowgraph on port 8080, and are skipped while run.py is
running or the port is taken. Hands-on checks are separate:
tests/hands_on.py --knob waveshare|griffin.

Exit status is 0 when everything that ran passed.
"""

import argparse
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import knobs  # noqa: E402

UNIT = ("host unit tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests"])
SUITES = {
    knobs.WAVESHARE: [
        ("Waveshare knob self-test", [sys.executable, "firmware/tools/selftest.py"]),
        ("run.py with the Waveshare knob", [sys.executable, "tests/runpy_test.py"]),
        ("run.py reconnecting to the Waveshare knob", [sys.executable, "tests/reconnect_test.py"]),
    ],
    knobs.GRIFFIN: [
        ("Griffin PowerMate checks", [sys.executable, "tests/griffin_test.py"]),
    ],
}
# these start their own run.py and fake flowgraph
RUNPY_TESTS = ("tests/runpy_test.py", "tests/reconnect_test.py")


def run_py_running():
    """PIDs of other run.py processes: one would also act on the injected
    turns and overwrite the knob's readout."""
    pids = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        # ./run.py, python3 run.py or python3 -u run.py
        if any(os.path.basename(a) == b"run.py" for a in args[:3]):
            pids.append(int(pid))
    return pids


def port_free(port=8080):
    s = socket.socket()
    try:
        # the fake flowgraph binds with SO_REUSEADDR too: a port in TIME_WAIT is free
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def have_pyusb():
    try:
        import usb.core  # noqa: F401
        return True
    except ImportError:
        return False


def run(name, cmd, results):
    print("\n=== %s: %s" % (name, " ".join(cmd[1:])), flush=True)
    start = time.time()
    code = subprocess.call(cmd, cwd=REPO)
    results.append((name, "passed" if code == 0 else "FAILED (exit %d)" % code, time.time() - start))
    return code == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--knob", choices=knobs.KINDS, action="append",
                   help="only this knob (may be given twice); fails if it isn't plugged in")
    g.add_argument("--no-hardware", action="store_true", help="host unit tests only")
    args = ap.parse_args()

    results = []
    run(*UNIT, results)

    if not args.no_hardware:
        attached = {kind for kind, _ in knobs.attached()}
        wanted = args.knob or list(knobs.KINDS)
        problems = []
        if knobs.UNKNOWN in attached:
            problems.append("a PowerMate is plugged in but its USB device node isn't accessible, so it "
                            "can't be identified: see 'Permissions' in README.md")
        if not have_pyusb():
            problems.append("the hardware tests need pyusb: pip install pyusb")
        busy = run_py_running()
        for kind in wanted:
            if kind not in attached:
                if args.knob:
                    results.append((knobs.NAMES[kind], "FAILED (not plugged in)", 0))
                else:
                    results.append((knobs.NAMES[kind], "skipped (not plugged in)", 0))
                continue
            if problems:
                results.append((knobs.NAMES[kind], "skipped (%s)" % "; ".join(problems), 0))
                continue
            for name, cmd in SUITES[kind]:
                if cmd[1] in RUNPY_TESTS and (busy or not port_free()):
                    why = ("run.py is already running (pid %s)" % ", ".join(map(str, busy)) if busy
                           else "port 8080 is taken (a flowgraph running?)")
                    results.append((name, "skipped (%s: stop fosphor_knob.sh first)" % why, 0))
                    continue
                run(name, cmd, results)

    print("\n=== summary")
    width = max(len(name) for name, _, _ in results)
    for name, outcome, seconds in results:
        print("  %-*s  %-8s %s" % (width, name, ("%.0f s" % seconds) if seconds else "", outcome))
    failed = [name for name, outcome, _ in results if outcome.startswith("FAILED")]
    print("\n%s" % ("%d failed" % len(failed) if failed else "all passed"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
