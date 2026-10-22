#!/usr/bin/env python3
"""run.py keeps going when the knob is unplugged and plugged back in.

Runs run.py the way fosphor_knob.sh does (no device argument) against
tests/fake_flowgraph.py, then "unplugs" the Waveshare knob by rebooting it
over USB, and checks that run.py reconnects: the readout comes back, the LED
breathes again when idle and turning the ring tunes the radio.

A genuine Griffin plugged in at the same time would share the knob's
/dev/input/by-id name, so its kernel driver is unbound for the duration.
Needs read/write access to the knob's /dev/input/event* node.
"""

import glob
import os
import subprocess
import sys
import time

import usb.core

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "firmware", "tools"))
import fake_flowgraph  # noqa: E402
import pmknob  # noqa: E402
import selftest  # noqa: E402


def genuine_griffins():
    return [dev for dev in usb.core.find(find_all=True, idVendor=pmknob.VID, idProduct=pmknob.PID)
            if not pmknob.is_emulator(dev)]


def breathing(info):
    return pmknob.led_summary(info).startswith("breathing")


def wait_for(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        result = cond()
        if result:
            return result
        time.sleep(0.2)
    return None


def main():
    if pmknob.find_knob() is None:
        sys.exit("no PowerMate emulator found")
    griffins = [d for d in genuine_griffins() if d.is_kernel_driver_active(0)]
    for d in griffins:
        d.detach_kernel_driver(0)
    # udev moves the shared by-id name over to the knob: let it settle
    wait_for(lambda: glob.glob("/dev/input/by-id/*PowerMate*"), 5)
    time.sleep(0.5)
    # the readout left by an earlier run must not count as run.py's
    pmknob.send_display(pmknob.find_knob(), 0, 0, valid=False)

    fg = fake_flowgraph.FakeFlowgraph()
    server = fake_flowgraph.serve(fg)
    # run.py writes through its own append-mode handle (always at the end of
    # the file); we read through separate opens. A shared offset would let
    # run.py write over the start of the log.
    log_path = os.path.join(HERE, ".reconnect_test_run.log")
    open(log_path, "w").close()
    log = open(log_path, "a")
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(REPO, "run.py")], cwd=REPO,
                            stdout=log, stderr=subprocess.STDOUT)
    failures = 0

    def check(cond, what):
        nonlocal failures
        print("  %s  %s" % ("ok  " if cond else "FAIL", what))
        failures += 0 if cond else 1

    def output():
        with open(log_path) as f:
            return f.read()

    def readout_shown_and_breathing():
        """(dev, info, shown) once the knob shows a readout and its LED
        breathes; None until then, also while the knob is rebooting."""
        dev = pmknob.find_knob()
        if dev is None:
            return None
        try:
            info = pmknob.read_info(dev)
            shown = pmknob.read_display(dev)
        except usb.core.USBError:
            return None
        if shown["valid"] and breathing(info):
            return dev, info, shown
        return None

    try:
        print("run.py without a device argument")
        check(wait_for(lambda: "Using " in output(), 10) is not None, "finds the knob")
        state = wait_for(readout_shown_and_breathing, 8)
        check(state is not None, "starts on the knob: readout shown, LED breathing when idle")
        check(output().count("Using ") == 1, "opened one device")

        print("unplug and replug (knob reboot)")
        dev = pmknob.find_knob()
        dev.ctrl_transfer(pmknob.VENDOR_OUT, pmknob.REQ_REBOOT, 0, 0, None, timeout=1000)
        check(wait_for(lambda: "Lost the knob" in output(), 10) is not None, "run.py notices the knob is gone")
        check(proc.poll() is None, "run.py is still running")
        back = wait_for(lambda: output().count("Using ") >= 2, 20)
        check(back is not None, "run.py picks the knob up again")
        state = wait_for(readout_shown_and_breathing, 10)
        check(state is not None, "readout restored and LED breathing again")
        if state:
            dev, info, shown = state
            check(shown["freq_hz"] == int(fg.freq) and shown["gain_db"] == int(fg.gain),
                  "readout shows %.0f MHz, %d dB" % (fg.freq / 1e6, fg.gain))
            fg.take()
            selftest.inject(dev, rotate=1)
            time.sleep(0.5)
            calls = fg.take()
            check(calls == [("freq", fake_flowgraph.START_FREQ + 1000000)], "a turn tunes the radio (%s)" % calls)
            check(not breathing(pmknob.read_info(dev)), "and makes the LED steady")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        server.shutdown()
        server.server_close()
        for d in genuine_griffins():
            if not d.is_kernel_driver_active(0):
                d.attach_kernel_driver(0)
    if failures:
        print("\nrun.py output:\n" + output())
    log.close()
    os.unlink(log_path)
    print("\n%s" % ("all checks passed" if failures == 0 else "%d check(s) FAILED" % failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
