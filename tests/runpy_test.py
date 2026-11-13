#!/usr/bin/env python3
"""End-to-end test: run.py driving a fake fosphor flowgraph with the knob.

Starts tests/fake_flowgraph.py's XML-RPC stand-in for the GNU Radio flowgraph,
runs run.py on the Waveshare knob (PowerMate emulator firmware) through
the kernel powermate driver, injects turns and presses over USB and checks:

  - run.py tunes the "radio" the way it would with a Griffin PowerMate,
  - the knob's screen shows the radio's frequency and gain,
  - the LED breathes while the knob is idle and is steady while in use (the
    knob's own attract mode; run.py leaves the emulator's LED alone),
  - with the limits an RTL-SDR reports: tuning stops at the edge, presets it
    can't reach are skipped and the gain steps through its real values.

A real Griffin can stay plugged in: run.py is pointed at the knob's own event
device. Needs read/write access to that /dev/input/event* node.
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "firmware", "tools"))
import fake_flowgraph  # noqa: E402
import pmknob  # noqa: E402
import selftest  # noqa: E402

# The knob's own attract-mode idle time (PM_ATTRACT_IDLE_MS, 1 s by
# default): the LED checks are timed against it, so a different value set
# with 'pmknob.py attract' breaks them.
IDLE_SECONDS = 1


def main():
    dev = pmknob.find_knob()
    if dev is None:
        sys.exit("no PowerMate emulator found")
    node = selftest.event_node(dev)
    if not node or not os.access(node, os.R_OK | os.W_OK):
        sys.exit("%s is not readable and writable by this user" % node)

    fg = fake_flowgraph.FakeFlowgraph()
    server = fake_flowgraph.serve(fg)
    # the readout left by an earlier run must not count as run.py's
    pmknob.send_display(dev, 0, 0, valid=False)
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "run.py"), node], cwd=REPO,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    failures = 0

    def check(cond, what):
        nonlocal failures
        print("  %s  %s" % ("ok  " if cond else "FAIL", what))
        failures += 0 if cond else 1

    def check_display(what):
        shown = pmknob.read_display(dev)
        check(shown["valid"] and shown["freq_hz"] == int(fg.freq) and shown["gain_db"] == int(fg.gain)
              and (shown["gain_min"], shown["gain_max"]) == (0, 76),
              "screen shows %.0f MHz, %d dB %s" % (fg.freq / 1e6, int(fg.gain), what))

    def led():
        # what the ring does (the knob's firmware runs it), not what run.py asked for
        summary = pmknob.led_summary(pmknob.read_info(dev))
        return "breathing" if summary.startswith("breathing") else summary.split(" (")[0]

    def inject(rotate=0, button=None):
        selftest.inject(dev, rotate=rotate, button=button)

    try:
        time.sleep(2.5)   # run.py polls get_freq, then opens the input device
        print("run.py with the knob")
        check_display("at startup")
        check(led() == "breathing", "idle at startup: LED breathes (%s)" % led())

        inject(rotate=5)
        time.sleep(0.6)
        calls = fg.take()
        check(calls == [("freq", fake_flowgraph.START_FREQ + n * 1000000) for n in range(1, 6)],
              "5 clicks clockwise -> +5 MHz in 1 MHz steps (%s)" % calls)
        check_display("after turning")
        check(led() == "steady 255", "in use: LED steady at full brightness (%s)" % led())

        inject(rotate=-2)
        time.sleep(0.5)
        calls = fg.take()
        check(calls == [("freq", fake_flowgraph.START_FREQ + 4000000),
                        ("freq", fake_flowgraph.START_FREQ + 3000000)],
              "2 clicks counter-clockwise -> -2 MHz (%s)" % calls)

        inject(button=True)
        time.sleep(0.15)
        inject(button=False)
        time.sleep(0.5)
        calls = fg.take()
        check(calls == [("freq", 1950000000)], "short press -> next preset, 1950 MHz (%s)" % calls)
        check_display("after the preset")
        check(led() == "steady 255", "a press keeps the LED steady (%s)" % led())

        inject(button=True)
        time.sleep(0.1)
        inject(rotate=3)
        time.sleep(0.4)
        inject(button=False)
        time.sleep(0.4)
        calls = fg.take()
        check(calls == [("gain", fake_flowgraph.START_GAIN + n) for n in range(1, 4)],
              "hold + 3 clicks -> gain +3 dB, no preset change (%s)" % calls)
        check_display("after the gain change")

        # Keep using it for longer than the idle time: must not start breathing.
        for _ in range(4):
            time.sleep(IDLE_SECONDS * 0.6)
            inject(rotate=1)
        time.sleep(0.3)
        check(led() == "steady 255", "steady through %ds of continued use (%s)" % (IDLE_SECONDS * 2, led()))

        time.sleep(IDLE_SECONDS + 1.5)
        check(led() == "breathing", "breathes again after %ds idle (%s)" % (IDLE_SECONDS, led()))
        inject(rotate=-1)
        time.sleep(0.4)
        check(led() == "steady 255", "and goes steady on the next turn (%s)" % led())
        fg.take()
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        server.shutdown()
        server.server_close()
    if not failures:
        out = limits_pass(dev, node, check)
    if failures:
        print("\nrun.py output:\n" + out)
    print("\n%s" % ("all checks passed" if failures == 0 else "%d check(s) FAILED" % failures))
    sys.exit(1 if failures else 0)


def limits_pass(dev, node, check):
    """run.py against a flowgraph that publishes an RTL-SDR's limits; returns run.py's output."""
    fg = fake_flowgraph.FakeFlowgraph(freq=1760e6, gain=30, like="rtlsdr")
    server = fake_flowgraph.serve(fg)
    limits = fake_flowgraph.knob_limits("rtlsdr")
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "run.py"), node], cwd=REPO,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def inject(rotate=0, button=None):
        selftest.inject(dev, rotate=rotate, button=button)

    try:
        time.sleep(2.5)
        print("\nrun.py with an RTL-SDR's limits")
        check(fg.take() == [("gain", 29)] and fg.gain == 29.7, "starts on a gain the SDR has: 30 -> 29.7 dB")

        inject(rotate=10)
        time.sleep(0.8)
        calls = fg.take()
        top = int(limits["freq_max"])
        check(calls == [("freq", f) for f in (1761000000, 1762000000, 1763000000, 1764000000, top)],
              "10 clicks up from 1760 MHz stop at %.1f MHz (%s)" % (top / 1e6, calls))
        shown = pmknob.read_display(dev)
        check(shown["freq_hz"] == top, "the screen shows the edge, %.1f MHz" % (shown["freq_hz"] / 1e6))

        inject(button=True)
        time.sleep(0.15)
        inject(button=False)
        time.sleep(0.5)
        calls = fg.take()
        check(calls == [("freq", 140000000)], "a press skips 1950-5160 MHz: 140 MHz (%s)" % calls)

        inject(button=True)
        time.sleep(0.1)
        inject(rotate=2)
        time.sleep(0.4)
        inject(button=False)
        time.sleep(0.4)
        fg.take()
        check(fg.gain == 33.8, "hold + 2 clicks: 29.7 -> 32.8 -> 33.8 dB (%s)" % fg.gain)
        shown = pmknob.read_display(dev)
        check((shown["gain_db"], shown["gain_min"], shown["gain_max"]) == (33.8, 0, 49.6),
              "the screen shows 33.8 dB on the SDR's 0-49.6 dB arc (%s)" % shown)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        server.shutdown()
        server.server_close()
    return out


if __name__ == "__main__":
    main()
