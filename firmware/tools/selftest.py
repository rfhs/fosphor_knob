#!/usr/bin/env python3
"""Automated checks for the PowerMate emulator, no hands needed.

1. USB descriptors match a genuine Griffin PowerMate.
2. LED vendor requests (as sent by the Linux driver) change the LED state,
   and the frequency/gain readout takes and returns its values.
3. Raw HID reports for injected turns and presses (temporarily unbinds the
   kernel driver, then rebinds it).
4. With no computer talking to it (simulated by unconfiguring it), the knob
   shows the "no USB connection / flip the plug" hint, and drops it again.
5. Attract mode: left alone, the screen breathes on the backlight without
   being redrawn; use makes it steady again. While it breathes, the middle
   of the screen takes turns between the readout and the invitation to try
   the knob; without readout values it shows the RFHS logo.
6. Requests the firmware must refuse are refused, and asking for flash
   mode only puts up the prompt (it needs a touch on the knob), which
   blinks the backlight.
7. If the knob's /dev/input/event* node is readable: injected turns and
   presses come out of the kernel powermate driver as REL_DIAL / BTN_0, and
   EV_MSC/MSC_PULSELED writes reach the LED.

Needs a firmware build with PM_TEST_REQUESTS (the default). Settings it
changes (attract mode, the readout) are put back afterwards. Exit status is
0 when everything that could run passed.
"""

import glob
import os
import select
import struct
import sys
import time

import usb.core
import usb.util

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pmknob  # noqa: E402

EXPECTED_DEVICE = bytes.fromhex("12 01 00 02 00 00 00 08 7d 07 10 04 08 00 01 02 00 01")
EXPECTED_CONFIG = bytes.fromhex(
    "09 02 29 00 01 01 00 c0 32  09 04 00 00 02 03 00 00 00  09 21 11 01 00 01 22 4a 00"
    "07 05 81 03 06 00 0a  07 05 01 03 01 00 0a")
EXPECTED_REPORT = bytes.fromhex(
    "05 0c 09 01 a1 01 05 09 19 01 29 01 15 00 25 01 75 01 95 01 81 02 75 07 95 01 81 01"
    "05 01 09 33 15 81 25 7f 75 08 95 01 81 02 05 0c 09 01 15 00 26 ff 00 75 08 95 04 81 02"
    "09 01 75 08 95 01 91 02 09 01 75 08 95 08 b1 02 c0")
EXPECTED_STRINGS = {
    0: bytes.fromhex("04030904"),
    1: b"\x34\x03" + "Griffin Technology Inc.\0\0".encode("utf-16-le"),
    2: b"\x2e\x03" + "Griffin PowerMate\0\0\0\0\0".encode("utf-16-le"),
}

EV_SYN, EV_KEY, EV_REL, EV_MSC = 0, 1, 2, 4
REL_DIAL, BTN_0, MSC_PULSELED = 7, 0x100, 1
EVENT = struct.Struct("llHHi")     # struct input_event

failures = 0


def check(cond, what):
    global failures
    print("  %s  %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        failures += 1
    return cond


def refused(dev, *args):
    try:
        dev.ctrl_transfer(*args, timeout=1000)
        return False
    except usb.core.USBError:
        return True


def test_descriptors(dev):
    print("descriptors")
    # GET_DESCRIPTOR (6): wValue = (type << 8) | index
    devd = bytes(dev.ctrl_transfer(0x80, 6, 0x0100, 0, 18))
    # bMaxPacketSize0 may legitimately differ (full speed vs the original's low speed)
    check(devd[:7] + devd[8:] == EXPECTED_DEVICE[:7] + EXPECTED_DEVICE[8:],
          "device descriptor (EP0 size %d, original 8)" % devd[7])
    check(bytes(dev.ctrl_transfer(0x80, 6, 0x0200, 0, 255)) == EXPECTED_CONFIG, "configuration descriptor")
    for idx, want in EXPECTED_STRINGS.items():
        got = bytes(dev.ctrl_transfer(0x80, 6, 0x0300 | idx, 0x0409 if idx else 0, 255))
        check(got == want, "string descriptor %d" % idx)
    check(refused(dev, 0x80, 6, 0x0303, 0x0409, 255), "string descriptor 3 stalls")


def test_led(dev):
    print("LED commands (attract mode off meanwhile, so the host owns the LED)")
    attract = pmknob.read_info(dev)["attract_ms"]
    pmknob.set_attract(dev, 0)
    try:
        led_commands(dev)
    finally:
        pmknob.set_attract(dev, attract)
    check(pmknob.read_info(dev)["attract_ms"] == attract, "attract mode restored (%d ms)" % attract)


def led_commands(dev):
    led = pmknob.led_command
    led(dev, pmknob.LED_CMD_STATIC_BRIGHTNESS, 0x42)
    check(pmknob.read_info(dev)["led_brightness"] == 0x42, "static brightness")
    led(dev, pmknob.LED_CMD_PULSE_AWAKE, 1)
    led(dev, pmknob.LED_CMD_PULSE_ASLEEP, 0)
    i = pmknob.read_info(dev)
    check(i["led_awake"] == 1 and i["led_asleep"] == 0, "pulse awake/asleep flags")
    # Linux driver encoding: wIndex = (arg << 8) | op, for a speed of 255 - arg
    # (op 0), 255 (op 1) or 255 + arg (op 2); the table goes in wValue
    for speed, table, op, arg in ((255, 2, 1, 0), (100, 1, 0, 155), (400, 0, 2, 145)):
        led(dev, pmknob.LED_CMD_PULSE_MODE, (arg << 8) | op, table=table)
        i = pmknob.read_info(dev)
        check(i["led_speed"] == speed and i["led_table"] == table,
              "pulse mode -> speed %d table %d (got %d/%d)" % (speed, table, i["led_speed"], i["led_table"]))
    time.sleep(0.3)
    levels = set()
    for _ in range(20):
        levels.add(pmknob.read_info(dev)["led_level"])
        time.sleep(0.05)
    check(len(levels) > 5, "pulsing changes the rendered level (%d distinct values)" % len(levels))
    # back to what the kernel driver sets up at probe
    led(dev, pmknob.LED_CMD_PULSE_AWAKE, 0)
    led(dev, pmknob.LED_CMD_PULSE_ASLEEP, 1)
    led(dev, pmknob.LED_CMD_PULSE_MODE, 0x0001)     # normal speed, table 0
    led(dev, pmknob.LED_CMD_STATIC_BRIGHTNESS, 0x80)
    time.sleep(0.05)
    check(pmknob.read_info(dev)["led_level"] == 0x80, "static level rendered")


def test_attract_breathing(dev):
    print("attract mode: the backlight breathes when idle, the ring stays put")
    before = pmknob.read_info(dev)["attract_ms"]
    pmknob.set_attract(dev, 300)             # shortened for the test
    try:
        inject(dev, button=True)
        inject(dev, button=False)
        time.sleep(0.1)
        i = pmknob.read_info(dev)
        check(i["led_mode"] == pmknob.LED_MODE_IN_USE and i["backlight_level"] >= 95, "steady right after use")
        time.sleep(0.4)
        check(pmknob.read_info(dev)["led_mode"] == pmknob.LED_MODE_ATTRACT, "breathing after 0.3 s")
        levels = set()
        for _ in range(25):
            levels.add(pmknob.read_info(dev)["backlight_level"])
            time.sleep(0.1)
        time.sleep(1.2)                      # the refresh figure covers the last second
        i = pmknob.read_info(dev)
        check(len(levels) >= 4 and min(levels) < 40,
              "the backlight breathes (levels %s)" % sorted(levels))
        check(i["led_level"] == 255 and i["ui_fps_x10"] < 20,
              "with the ring steady, so the screen is not redrawn (%.1f refreshes/s)" % (i["ui_fps_x10"] / 10))
        inject(dev, button=True)
        inject(dev, button=False)
        time.sleep(0.1)
        i = pmknob.read_info(dev)
        check(i["led_mode"] == pmknob.LED_MODE_IN_USE and i["backlight_level"] >= 95,
              "a touch brings it straight back: steady, backlight %d%%" % i["backlight_level"])
    finally:
        pmknob.set_attract(dev, before)
    check(pmknob.read_info(dev)["attract_ms"] == before, "setting restored (%d ms)" % before)


def wait_for(dev, condition, timeout):
    """Read the info until condition(info) holds or timeout seconds pass; returns the last info."""
    end = time.time() + timeout
    while True:
        info = pmknob.read_info(dev)
        if condition(info) or time.time() > end:
            return info
        time.sleep(0.05)


def test_screens(dev):
    print("the middle of the screen: the readout, the invitation in attract mode, the RFHS logo")
    info = pmknob.read_info(dev)
    before = (info["attract_ms"], info["invite_ms"])
    shown = pmknob.read_display(dev)
    pmknob.send_display(dev, 433920000, 30)
    pmknob.set_attract(dev, 300, 1000)       # shortened for the test
    try:
        inject(dev, button=True)
        inject(dev, button=False)
        i = wait_for(dev, lambda i: i["screen"] == pmknob.SCREEN_READOUT, 6)    # 6 s: past the boot splash
        check(i["screen"] == pmknob.SCREEN_READOUT, "the readout while in use")
        i = wait_for(dev, lambda i: i["led_mode"] == pmknob.LED_MODE_ATTRACT, 1)
        check(i["screen"] == pmknob.SCREEN_READOUT, "still the readout once attract mode starts")
        start = time.time()
        i = wait_for(dev, lambda i: i["screen"] == pmknob.SCREEN_INVITE, 2)
        took = time.time() - start
        check(i["screen"] == pmknob.SCREEN_INVITE and 0.7 < took < 1.4,
              "then the invitation, %.1f s later" % took)
        i = wait_for(dev, lambda i: i["screen"] == pmknob.SCREEN_READOUT, 1.5)
        check(i["screen"] == pmknob.SCREEN_READOUT, "then the readout again")
        wait_for(dev, lambda i: i["screen"] == pmknob.SCREEN_INVITE, 1.5)
        inject(dev, button=True)
        inject(dev, button=False)
        time.sleep(0.1)
        check(pmknob.read_info(dev)["screen"] == pmknob.SCREEN_READOUT, "a touch brings the readout straight back")
        pmknob.send_display(dev, 0, 0, valid=False)
        i = wait_for(dev, lambda i: i["screen"] == pmknob.SCREEN_LOGO, 0.5)
        check(i["screen"] == pmknob.SCREEN_LOGO, "without readout values: the RFHS logo")
    finally:
        pmknob.set_attract(dev, *before)
        pmknob.send_display(dev, shown["freq_hz"], shown["gain_db"], shown["gain_min"], shown["gain_max"],
                            valid=shown["valid"])
    i = pmknob.read_info(dev)
    check((i["attract_ms"], i["invite_ms"]) == before, "settings restored (%d ms, invitation %d ms)" % before)


def test_hardening(dev):
    print("request hardening")
    check(refused(dev, pmknob.VENDOR_OUT_INTERFACE, pmknob.REQ_LED, pmknob.LED_CMD_STATIC_BRIGHTNESS, 0x40, b"\x00"),
          "LED command with an unexpected data stage is refused")
    check(refused(dev, pmknob.VENDOR_OUT, pmknob.REQ_HAPTIC, 200, 0, None),
          "haptic effect out of range is refused")
    check(refused(dev, pmknob.VENDOR_IN, pmknob.REQ_I2C, 0x5A00, 0, 1),
          "raw I2C access is off (CONFIG_PM_DEBUG_I2C)")
    check(refused(dev, pmknob.VENDOR_OUT, pmknob.REQ_ROTATION, 7, 0, None), "rotation out of range is refused")
    # Flash mode (wValue 1) only asks; nobody touches the screen, and we
    # withdraw the request (wValue 2).
    dev.ctrl_transfer(pmknob.VENDOR_OUT, pmknob.REQ_REBOOT, 1, 0, None, timeout=1000)
    time.sleep(0.5)
    check(pmknob.read_info(dev)["flash_confirm"] == 1 and pmknob.find_knob() is not None,
          "flash mode request waits for a touch on the knob instead of rebooting")
    levels = []                              # the figure changes as each 250 ms fade ends
    for _ in range(30):
        i = pmknob.read_info(dev)
        if not levels or levels[-1] != i["backlight_level"]:
            levels.append(i["backlight_level"])
        time.sleep(0.05)
    check(len(levels) >= 5 and min(levels) <= 40 and max(levels) >= 90,
          "the backlight blinks to ask for the touch (levels %s)" % levels)
    check(i["led_level"] == 255 and i["ui_fps_x10"] < 20,
          "with the ring steady, so nothing is redrawn and nothing tears (%.1f refreshes/s)" %
          (i["ui_fps_x10"] / 10))
    dev.ctrl_transfer(pmknob.VENDOR_OUT, pmknob.REQ_REBOOT, 2, 0, None, timeout=1000)
    time.sleep(0.1)
    check(pmknob.read_info(dev)["flash_confirm"] == 0, "and the request can be withdrawn")


def test_display(dev):
    print("display readout")
    before = pmknob.read_display(dev)
    pmknob.send_display(dev, 433920000, 55.5, -10, 70)
    shown = pmknob.read_display(dev)
    check(shown == {"valid": True, "freq_hz": 433920000, "gain_db": 55.5, "gain_min": -10, "gain_max": 70},
          "values round-trip (%s)" % shown)
    time.sleep(0.1)
    check(pmknob.read_info(dev)["display_has_values"] == 1, "readout is shown")
    pmknob.send_display(dev, 433920000, None)
    check(pmknob.read_display(dev)["gain_db"] is None, "a receiver without gain control round-trips")
    pmknob.send_display(dev, 0, 0, valid=False)
    time.sleep(0.1)
    check(not pmknob.read_display(dev)["valid"] and pmknob.read_info(dev)["display_has_values"] == 0,
          "readout can be cleared")
    pmknob.send_display(dev, before["freq_hz"], before["gain_db"], before["gain_min"], before["gain_max"],
                        valid=before["valid"])


def inject(dev, rotate=0, button=None):
    """Simulate a turn of -128..127 steps and/or set the button (None: unchanged)."""
    b = 0xFFFF if button is None else int(button)
    dev.ctrl_transfer(pmknob.VENDOR_OUT, pmknob.REQ_INJECT, rotate & 0xFF, b, None, timeout=1000)


def read_reports(dev, duration):
    out = []
    end = time.time() + duration
    while time.time() < end:
        try:
            out.append(bytes(dev.read(0x81, 6, timeout=50)))
        except usb.core.USBTimeoutError:
            pass
    return out


def test_raw_reports(dev):
    print("raw HID reports (kernel driver temporarily unbound)")
    detached = dev.is_kernel_driver_active(0)
    if detached:
        dev.detach_kernel_driver(0)
    usb.util.claim_interface(dev, 0)
    try:
        check(bytes(dev.ctrl_transfer(0x81, 6, 0x2200, 0, 255)) == EXPECTED_REPORT, "HID report descriptor")
        read_reports(dev, 0.1)
        inject(dev, rotate=3)
        r = read_reports(dev, 0.3)
        check([x[:2] for x in r] == [b"\x00\x01"] * 3, "+3 turn -> three +1 reports (%s)" % [x[:2].hex() for x in r])
        inject(dev, rotate=-2)
        r = read_reports(dev, 0.3)
        check([x[:2] for x in r] == [b"\x00\xff"] * 2, "-2 turn -> two -1 reports")
        inject(dev, button=True)
        r = read_reports(dev, 0.2)
        check([x[:2] for x in r] == [b"\x01\x00"], "press -> one report with button set")
        inject(dev, rotate=1)
        r = read_reports(dev, 0.2)
        check([x[:2] for x in r] == [b"\x01\x01"], "turn while pressed keeps the button bit")
        inject(dev, button=False)
        r = read_reports(dev, 0.2)
        check([x[:2] for x in r] == [b"\x00\x00"], "release -> one report with button clear")
        check(len(read_reports(dev, 0.3)) == 0, "idle -> no reports")
        check(all(len(x) == 6 for x in r), "reports are 6 bytes")
    finally:
        usb.util.release_interface(dev, 0)
        if detached:
            dev.attach_kernel_driver(0)


def test_plug_hint(dev):
    print("no-USB hint (knob unconfigured, kernel driver temporarily unbound)")
    shown = pmknob.read_display(dev)
    check(not pmknob.read_info(dev)["plug_hint"], "no hint while the computer uses the knob")
    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)
    try:
        dev.set_configuration(0)
        t0 = time.time()
        while time.time() - t0 < 5 and not pmknob.read_info(dev)["plug_hint"]:
            time.sleep(0.1)
        waited = time.time() - t0
        check(2.5 < waited < 4, "hint appears %.1f s after losing the computer" % waited)
    finally:
        dev.set_configuration(1)       # the kernel rebinds the powermate driver itself
        time.sleep(0.3)
        if not dev.is_kernel_driver_active(0):
            dev.attach_kernel_driver(0)
    check(not pmknob.read_info(dev)["plug_hint"], "hint goes away once configured again")
    if shown["valid"]:
        pmknob.send_display(dev, shown["freq_hz"], shown["gain_db"], shown["gain_min"], shown["gain_max"])


def event_node(dev):
    """The knob's /dev/input/event* from the kernel powermate driver, or None."""
    d = pmknob.sysfs_dir(dev)
    if d is None:
        return None
    nodes = glob.glob(d + "/*:1.0/input/input*/event*")
    return "/dev/input/" + os.path.basename(nodes[0]) if nodes else None


def read_events(fd, duration):
    out = []
    end = time.time() + duration
    while True:
        left = end - time.time()
        if left <= 0:
            break
        r, _, _ = select.select([fd], [], [], left)
        if r:
            data = os.read(fd, EVENT.size * 64)
            for i in range(0, len(data), EVENT.size):
                _, _, typ, code, value = EVENT.unpack_from(data, i)
                if typ != EV_SYN:
                    out.append((typ, code, value))
    return out


def test_kernel_events(dev):
    print("kernel powermate driver")
    for _ in range(50):
        node = event_node(dev)
        if node:
            break
        time.sleep(0.1)
    if not node:
        check(False, "input device for the knob")
        return
    # The node was just recreated by the driver rebind: give udev a moment to
    # apply any permission rules before deciding it isn't accessible.
    fd = None
    for _ in range(20):
        try:
            fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
            break
        except PermissionError:
            time.sleep(0.1)
    if fd is None:
        print("  skip  %s is not accessible; run as root or add yourself to its group "
              "(usually 'input') to include this part" % node)
        return
    try:
        read_events(fd, 0.2)
        inject(dev, rotate=2)
        ev = read_events(fd, 0.3)
        check(ev == [(EV_REL, REL_DIAL, 1)] * 2, "+2 turn -> REL_DIAL +1, +1 (%s)" % ev)
        inject(dev, rotate=-1)
        check(read_events(fd, 0.3) == [(EV_REL, REL_DIAL, -1)], "-1 turn -> REL_DIAL -1")
        inject(dev, button=True)
        check(read_events(fd, 0.3) == [(EV_KEY, BTN_0, 1)], "press -> BTN_0 down")
        inject(dev, button=False)
        check(read_events(fd, 0.3) == [(EV_KEY, BTN_0, 0)], "release -> BTN_0 up")
        # LED through the input layer, encoded like powermate.py's LedEvent:
        # brightness | speed << 8 | table << 17 | asleep << 19 | awake << 20
        value = 0x37 | (255 << 8) | (2 << 17) | (1 << 19) | (1 << 20)
        os.write(fd, EVENT.pack(0, 0, EV_MSC, MSC_PULSELED, value))
        time.sleep(0.3)
        i = pmknob.read_info(dev)
        check(i["led_awake"] == 1 and i["led_table"] == 2 and i["led_brightness"] == 0x37,
              "EV_MSC/MSC_PULSELED reaches the LED (pulse on, table 2)")
        os.write(fd, EVENT.pack(0, 0, EV_MSC, MSC_PULSELED, 0x80 | (255 << 8) | (1 << 19)))
        time.sleep(0.3)
        i = pmknob.read_info(dev)
        check(i["led_awake"] == 0 and i["led_brightness"] == 0x80, "LED back to static 0x80")
    finally:
        os.close(fd)


def main():
    dev = pmknob.find_knob()
    if dev is None:
        sys.exit("no PowerMate emulator found")
    test_descriptors(dev)
    test_led(dev)
    test_display(dev)
    test_raw_reports(dev)
    test_plug_hint(dev)
    test_attract_breathing(dev)
    test_screens(dev)
    test_hardening(dev)
    test_kernel_events(dev)
    print("\n%s" % ("all checks passed" if failures == 0 else "%d check(s) FAILED" % failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
