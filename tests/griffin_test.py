#!/usr/bin/env python3
"""Automated checks for a genuine Griffin PowerMate, no hands needed.

1. Its USB descriptors are the ones the emulator firmware copies, checked
   against the same expectations as the emulator's self-test; with a
   Waveshare knob plugged in too, the two are also compared with each other.
2. The kernel's powermate driver is bound, and its input device offers what
   run.py uses (REL_DIAL, BTN_0, EV_MSC/MSC_PULSELED) and can be opened.
3. LED writes through the input device are accepted (static brightness and
   pulsing); it is left steady at full brightness.

Turning, pressing and what the LED actually does need a hand and an eye:
tests/hands_on.py --knob griffin. Reading the HID report descriptor
unbinds the kernel driver for a moment (it is bound again straight away).
Needs pyusb, and read/write access to the Griffin's USB and input nodes.
"""

import os
import struct
import sys
import time

import usb.core
import usb.util

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "firmware", "tools"))
import pmknob  # noqa: E402
import selftest  # noqa: E402
from powermate import LedEvent, MAX_BRIGHTNESS  # noqa: E402

EV_KEY, EV_REL, EV_MSC = 1, 2, 4
REL_DIAL, BTN_0, MSC_PULSELED = 7, 0x100, 1
LONG_BITS = struct.calcsize("l") * 8

failures = 0


def check(cond, what):
    global failures
    print("  %s  %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        failures += 1
    return cond


def has_bit(capabilities, bit):
    """Bit test on a sysfs capability bitmap (hex longs, most significant first)."""
    words = capabilities.split()
    index = len(words) - 1 - bit // LONG_BITS
    return index >= 0 and (int(words[index], 16) >> (bit % LONG_BITS)) & 1 == 1


def descriptors(dev):
    out = {"device": bytes(dev.ctrl_transfer(0x80, 6, 0x0100, 0, 18)),
           "configuration": bytes(dev.ctrl_transfer(0x80, 6, 0x0200, 0, 255))}
    for idx in selftest.EXPECTED_STRINGS:
        out["string %d" % idx] = bytes(dev.ctrl_transfer(0x80, 6, 0x0300 | idx, 0x0409 if idx else 0, 255))
    return out


def test_descriptors(dev):
    print("descriptors")
    got = descriptors(dev)
    check(got["device"] == selftest.EXPECTED_DEVICE, "device descriptor (%s)" % got["device"].hex(" "))
    check(got["configuration"] == selftest.EXPECTED_CONFIG, "configuration descriptor")
    for idx, want in selftest.EXPECTED_STRINGS.items():
        check(got["string %d" % idx] == want, "string descriptor %d" % idx)
    rebind = dev.is_kernel_driver_active(0)
    try:
        if rebind:
            dev.detach_kernel_driver(0)
        report = bytes(dev.ctrl_transfer(0x81, 6, 0x2200, 0, len(selftest.EXPECTED_REPORT)))
        check(report == selftest.EXPECTED_REPORT, "HID report descriptor (%d bytes)" % len(report))
    finally:
        # pyusb claimed the interface for the transfer: attaching the kernel
        # driver fails with "Resource busy" until it is released
        usb.util.release_interface(dev, 0)
        if rebind:
            dev.attach_kernel_driver(0)
    return got


def test_same_as_emulator(griffin_descriptors):
    emulator = pmknob.find_knob()
    if emulator is None:
        print("compared with the Waveshare knob: skipped, none plugged in")
        return
    print("compared with the Waveshare knob")
    theirs = descriptors(emulator)
    for name, ours in griffin_descriptors.items():
        other = theirs[name]
        if name == "device":
            # bMaxPacketSize0 may differ: the emulator runs at full speed
            same = ours[:7] + ours[8:] == other[:7] + other[8:]
        else:
            same = ours == other
        check(same, "same %s descriptor" % name)


def wait_for_event_node(dev, timeout=5):
    """The driver was just rebound (test_descriptors): wait for the input
    node to come back and for udev to set its permissions."""
    end = time.time() + timeout
    while time.time() < end:
        node = selftest.event_node(dev)
        if node and os.access(node, os.R_OK | os.W_OK):
            return node
        time.sleep(0.1)
    return selftest.event_node(dev)


def test_kernel_driver(dev):
    print("kernel powermate driver")
    d = pmknob.sysfs_dir(dev)
    driver = os.path.realpath(os.path.join(d, os.path.basename(d) + ":1.0", "driver")) if d else ""
    check(os.path.basename(driver) == "powermate", "interface bound to the powermate driver (%s)" %
          (os.path.basename(driver) or "none"))
    node = wait_for_event_node(dev)
    if not check(node is not None, "input device %s" % node):
        return None
    caps = os.path.join("/sys/class/input", os.path.basename(node), "device", "capabilities")

    def cap(name):
        with open(os.path.join(caps, name)) as f:
            return f.read()
    ev = cap("ev")
    check(has_bit(ev, EV_REL) and has_bit(cap("rel"), REL_DIAL), "turning: EV_REL / REL_DIAL")
    check(has_bit(ev, EV_KEY) and has_bit(cap("key"), BTN_0), "pressing: EV_KEY / BTN_0")
    check(has_bit(ev, EV_MSC) and has_bit(cap("msc"), MSC_PULSELED), "LED: EV_MSC / MSC_PULSELED")
    if not check(os.access(node, os.R_OK | os.W_OK), "%s is readable and writable by this user" % node):
        return None
    return node


def test_led(node):
    print("LED writes")
    fd = os.open(node, os.O_WRONLY)
    try:
        for name, event in (("off", LedEvent(brightness=0)), ("half", LedEvent(brightness=128)),
                            ("pulsing", LedEvent.pulse()), ("full", LedEvent(brightness=MAX_BRIGHTNESS))):
            try:
                os.write(fd, event.raw())
                ok = True
            except OSError as e:
                ok = False
                name += " (%s)" % e
            check(ok, "accepted: %s" % name)
            time.sleep(0.3)
    finally:
        os.close(fd)


def main():
    griffins = [d for d in usb.core.find(find_all=True, idVendor=pmknob.VID, idProduct=pmknob.PID)
                if not pmknob.is_emulator(d)]
    if not griffins:
        sys.exit("no genuine Griffin PowerMate found")
    dev = griffins[0]
    print("Griffin PowerMate on bus %d, device %d" % (dev.bus, dev.address))
    test_same_as_emulator(test_descriptors(dev))
    node = test_kernel_driver(dev)
    if node:
        test_led(node)
    print("\n%s" % ("all checks passed" if failures == 0 else "%d check(s) FAILED" % failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
