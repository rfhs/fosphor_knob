"""Which PowerMates are plugged in, and which is which.

Both knobs show up as a Griffin PowerMate (USB 077d:0410) and share one
/dev/input/by-id name, so they are told apart by asking each one for the
emulator's status: a Waveshare knob answers, a genuine Griffin doesn't.
Standard library only (no pyusb), like run.py.
"""

import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import knob_display  # noqa: E402

WAVESHARE = "waveshare"
GRIFFIN = "griffin"
UNKNOWN = "unknown"     # can't open its USB device node to ask
KINDS = (WAVESHARE, GRIFFIN)
NAMES = {WAVESHARE: "Waveshare knob (PowerMate emulator firmware)",
         GRIFFIN: "Griffin PowerMate",
         UNKNOWN: "PowerMate (no access to its USB device node to tell which)"}


def _number(path):
    m = re.search(r"(\d+)$", path)
    return int(m.group(1)) if m else -1


def event_nodes():
    """/dev/input/eventN of every PowerMate the kernel driver has bound."""
    out = []
    for sys_path in glob.glob("/sys/class/input/event*"):
        try:
            with open(os.path.join(sys_path, "device", "name")) as f:
                name = f.read().strip()
        except OSError:
            continue
        if name == "Griffin PowerMate":
            out.append("/dev/input/" + os.path.basename(sys_path))
    return sorted(out, key=_number)


def kind_of(event_node):
    node = knob_display.usb_node(event_node)
    if node is None or not os.access(node, os.R_OK | os.W_OK):
        return UNKNOWN
    return WAVESHARE if knob_display.is_emulator(event_node) else GRIFFIN


def attached():
    """[(kind, /dev/input/eventN)] for every PowerMate plugged in."""
    return [(kind_of(node), node) for node in event_nodes()]


def find(kind):
    """The event node of the first knob of this kind, or None."""
    for k, node in attached():
        if k == kind:
            return node
    return None


if __name__ == "__main__":
    knobs = attached()
    for kind, node in knobs:
        print("%-18s %s" % (node, NAMES[kind]))
    if not knobs:
        print("no PowerMate plugged in (or the kernel's powermate driver isn't loaded)")
