#!/usr/bin/env python3
"""tests/simulator.sh starts the fake flowgraph and run.py, and Ctrl-C stops both.

Runs the script on a pseudo-terminal, turns and presses the Waveshare knob
with test requests (checking the default made-up limits: a preset skipped,
an uneven gain step), then types Ctrl-C the way a terminal does (SIGINT to the
foreground process group) and checks that nothing is left running and port
8080 is free again. Needs read/write access to the knob's /dev/input/event*
node; stop fosphor_knob.sh / run.py first.
"""

import os
import pty
import select
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "firmware", "tools"))
import pmknob  # noqa: E402
import run_all  # noqa: E402
import selftest  # noqa: E402

START_FREQ = 2424000000


class Terminal:
    def __init__(self, argv):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(REPO)
            os.execv(argv[0], argv)
        self.output = ""

    def expect(self, text, timeout):
        deadline = time.time() + timeout
        while text not in self.output:
            left = deadline - time.time()
            if left <= 0 or not select.select([self.fd], [], [], left)[0]:
                return False
            try:
                data = os.read(self.fd, 4096)
            except OSError:
                return False
            if not data:
                return False
            self.output += data.decode(errors="replace").replace("\r\n", "\n")
        return True

    def wait(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                return os.waitstatus_to_exitcode(status)
            self.expect("\0", 0.1)      # keep draining the output
        return None


def children(pid):
    try:
        with open("/proc/%d/task/%d/children" % (pid, pid)) as f:
            return [int(p) for p in f.read().split()]
    except OSError:
        return []


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def press(dev):
    selftest.inject(dev, button=True)
    time.sleep(0.15)
    selftest.inject(dev, button=False)


def main():
    dev = pmknob.find_knob()
    if dev is None:
        sys.exit("no PowerMate emulator found")
    if run_all.run_py_running() or not run_all.port_free():
        sys.exit("stop fosphor_knob.sh / run.py first: run.py is running or port 8080 is taken")
    failures = 0

    def check(cond, what):
        nonlocal failures
        print("  %s  %s" % ("ok  " if cond else "FAIL", what))
        failures += 0 if cond else 1

    term = Terminal([os.path.join(HERE, "simulator.sh"), "--freq", str(START_FREQ)])
    started = []
    try:
        print("tests/simulator.sh")
        check(term.expect("Found gain", 15), "fake flowgraph and run.py start")
        check(term.expect("Limits: 130.000 to 2440.000 MHz, gain -3 to 55 dB", 5),
              "run.py gets the made-up limits")
        started = children(term.pid)
        check(len(started) == 2, "two processes started (%s)" % started)
        time.sleep(0.5)     # run.py opens the input device after reading the gain

        selftest.inject(dev, rotate=1)
        check(term.expect("set_freq(%d" % (START_FREQ + 1000000), 3), "a turn tunes the fake radio +1 MHz")
        press(dev)
        check(term.expect("set_freq(1950000000", 3), "a press steps to the next preset, 1950 MHz")
        for _ in range(3):
            time.sleep(0.3)
            press(dev)
        check(term.expect("set_freq(140000000", 3) and "set_freq(5160" not in term.output,
              "three more presses skip 5160 MHz for 140 MHz")

        selftest.inject(dev, button=True)
        time.sleep(0.1)
        selftest.inject(dev, rotate=4)
        time.sleep(0.4)
        selftest.inject(dev, button=False)
        check(term.expect("set_gain(37.5)", 3), "hold + 4 clicks: 31, 32, 33, then the jump to 37.5 dB")

        os.write(term.fd, b"\x03")
        code = term.wait(5)
        check(term.expect("Simulator stopped.", 1), "Ctrl-C stops it")
        check(code == 0, "exit status 0 (%s)" % code)
        left = [pid for pid in started if alive(pid)]
        check(not left, "nothing left running (%s)" % left)
        check(run_all.port_free(), "port 8080 free again")
    finally:
        for pid in started + [term.pid]:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
        try:
            os.waitpid(term.pid, 0)
        except ChildProcessError:
            pass
    if failures:
        print("\nsimulator.sh output:\n" + term.output)
    print("\n%s" % ("all checks passed" if failures == 0 else "%d check(s) FAILED" % failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
