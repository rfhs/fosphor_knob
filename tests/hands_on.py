#!/usr/bin/env python3
"""Quick hands-on check of a knob: you turn and press, it watches.

Works with the Waveshare knob (PowerMate emulator firmware) and with a
genuine Griffin PowerMate. Run it on its own, not while run.py is running:

    tests/hands_on.py --knob waveshare
    tests/hands_on.py --knob griffin
    tests/hands_on.py [/dev/input/eventN]     (default: the only knob plugged in)

Every step has a timeout; press Enter during a step to skip it. Pass
conditions are generous: a couple of clicks in the right direction is enough.
Needs read/write access to the knob's /dev/input/event* node, and to its
USB device node to recognise the Waveshare knob (it is taken for a Griffin
otherwise).
"""

import argparse
import os
import select
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import knob_display  # noqa: E402
import knobs  # noqa: E402
import run  # noqa: E402
from powermate import LedEvent, MAX_BRIGHTNESS  # noqa: E402

EV_KEY, EV_REL = 1, 2
REL_DIAL, BTN_0 = 7, 0x100
EVENT = struct.Struct("llHHi")
STEP_SECONDS = 30

results = []


def say(text=""):
    print(text, flush=True)


def record(name, outcome):
    results.append((name, outcome))
    say("  -> %s\n" % outcome.upper())


def ask(question):
    while True:
        answer = input("  %s [y/n/s=skip] " % question).strip().lower()
        if answer in ("y", "yes"):
            return "pass"
        if answer in ("n", "no"):
            return "fail"
        if answer in ("s", "skip", ""):
            return "skipped"


def drain(fd):
    while select.select([fd], [], [], 0)[0]:
        if not os.read(fd, EVENT.size * 64):
            break


def watch(fd, done, seconds=STEP_SECONDS):
    """Feed input events to done(kind, code, value) until it returns True.
    Returns 'pass', 'skipped' (Enter pressed) or 'timeout'."""
    drain(fd)
    end = time.time() + seconds
    while time.time() < end:
        ready, _, _ = select.select([fd, sys.stdin], [], [], end - time.time())
        if sys.stdin in ready:
            sys.stdin.readline()
            return "skipped"
        if fd in ready:
            data = os.read(fd, EVENT.size * 64)
            for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _, _, kind, code, value = EVENT.unpack_from(data, i)
                if done(kind, code, value):
                    return "pass"
    return "timeout"


def turn_step(fd, name, direction, prompt):
    say(prompt + " (Enter to skip)")
    clicks = 0

    def done(kind, code, value):
        nonlocal clicks
        if kind == EV_REL and code == REL_DIAL and value * direction > 0:
            clicks += 1
        return clicks >= 2
    record(name, watch(fd, done))


def press_step(fd, prompt):
    say(prompt + " (Enter to skip)")
    presses = 0

    def done(kind, code, value):
        nonlocal presses
        if kind == EV_KEY and code == BTN_0 and value == 0:
            presses += 1
        return presses >= 2
    record("press", watch(fd, done))


def hold_turn_step(fd, prompt):
    say(prompt + " (Enter to skip)")
    held = False

    def done(kind, code, value):
        nonlocal held
        if kind == EV_KEY and code == BTN_0:
            held = bool(value)
        return kind == EV_REL and code == REL_DIAL and held
    record("hold and turn", watch(fd, done))


def send_led(fd, event):
    os.write(fd, event.raw())


def pick_device():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device", nargs="?", help="the knob's /dev/input/eventN")
    ap.add_argument("--knob", choices=knobs.KINDS, help="which knob, when both are plugged in")
    args = ap.parse_args()
    if args.device:
        return args.device
    if args.knob:
        device = knobs.find(args.knob)
        if device is None:
            sys.exit("no %s found (tests/knobs.py lists what is plugged in)" % knobs.NAMES[args.knob])
        return device
    attached = knobs.attached()
    if len(attached) > 1:
        sys.exit("more than one knob plugged in: pick one with --knob waveshare or --knob griffin")
    device, problem = run.findKnob()
    if device is None:
        sys.exit(problem)
    return device


def main():
    device = pick_device()
    try:
        fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    except PermissionError:
        sys.exit(run.permissionProblem(device))
    emulator = knob_display.is_emulator(device)
    name = "Waveshare knob, PowerMate emulator firmware" if emulator else "Griffin PowerMate"
    say("Knob: %s (%s)\n" % (name, os.path.realpath(device)))
    push = "tap the middle of the screen" if emulator else "press the knob down"

    turn_step(fd, "turn clockwise", +1, "1. Turn the knob clockwise a few clicks.")
    turn_step(fd, "turn counter-clockwise", -1, "2. Turn it counter-clockwise a few clicks.")
    press_step(fd, "3. Now %s a few times." % push)
    hold_turn_step(fd, "4. %s and keep holding, turn a click or two, then let go." %
                   ("Hold a finger on the screen" if emulator else "Push the knob down"))

    say("5. The LED.")
    if emulator:
        say("  Hands off the knob for 3 seconds...")
        time.sleep(3)
        record("breathes when idle",
               ask("Is the whole screen breathing (slowly fading in and out, ring steady)?"))
        say("  Now turn it slowly for a few seconds and watch the screen.")
        time.sleep(4)
        record("steady in use", ask("Did it come straight back to full brightness and stay there while you turned it?"))
    else:
        send_led(fd, LedEvent.pulse())
        time.sleep(2)
        record("LED breathes", ask("Is the LED in the base breathing?"))
        send_led(fd, LedEvent(brightness=MAX_BRIGHTNESS))
        time.sleep(1)
        record("LED steady", ask("Is it steady and bright now?"))

    if emulator:
        say("6. The screen and haptics.")
        knob_display.show(1234.5e6, 42)
        time.sleep(0.5)
        record("readout", ask("Does the screen show 1234.5 MHz and 42 dB GAIN?"))
        knob_display.clear()
        say("  The readout is cleared now: turn the knob a click and look at the screen.")
        record("logo", ask("Does it show the RF Hackers Sanctuary logo?"))
        say("  Hands off the knob for about 20 seconds...")
        time.sleep(18)
        record("invitation", ask("Does the screen say THIS IS INTERACTIVE! (taking turns with the readout)?"))
        record("haptics", ask("Did you feel a click on each turn and each tap?"))
        say("7. Optional: unplug the USB-C cable (or flip the plug) for 5 seconds, then plug it back in.")
        record("no-USB message", ask("Did the screen say NO USB CONNECTION while it was unplugged?"))

    os.close(fd)
    say("Summary")
    width = max(len(name) for name, _ in results)
    for name, outcome in results:
        say("  %-*s  %s" % (width, name, outcome))
    failed = [name for name, outcome in results if outcome in ("fail", "timeout")]
    skipped = sum(outcome == "skipped" for _, outcome in results)
    if failed:
        say("\n%d problem(s): %s" % (len(failed), ", ".join(failed)))
    else:
        say("\nno problems%s" % (" (%d skipped)" % skipped if skipped else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
