#!/usr/bin/env python3
"""Host-side helper for the Waveshare knob running the PowerMate emulator.

Talks to the knob with USB vendor requests, which work while the kernel's
powermate driver is bound; no root needed with udev/99-fosphor-knob.rules.
inject, haptic, screenshot and plug-hint need a firmware build with
PM_TEST_REQUESTS (the default); i2c needs PM_DEBUG_I2C as well.

    pmknob.py info                   firmware status, counters and settings
    pmknob.py pulses                 recent encoder pulses (debouncer tuning)
    pmknob.py touches [--watch]      what the touch controller reported (missed touches)
    pmknob.py led 128                set static LED brightness, like the kernel does
    pmknob.py led --pulse            pulse the LED
    pmknob.py inject --rotate 3 --press
                                     simulate turns and presses
    pmknob.py haptic 1               play haptic effect 1 (DRV2605L ROM library number)
    pmknob.py haptic --set detent 3  effect for press/release/detent (0 = off), kept in flash
    pmknob.py haptic --recalibrate   measure the actuator again
    pmknob.py i2c 0x5a 0x00 [N | --write VALUE]
                                     read/write a register on the I2C bus
    pmknob.py display 2424e6 32      show a frequency (Hz) and gain (dB) like run.py does
    pmknob.py display [--clear]      read back / blank the readout
    pmknob.py rotate 2               rotate the screen (quarter turns clockwise), kept in flash
    pmknob.py screenshot out.png
    pmknob.py plug-hint on|off       force the "flip the USB-C plug" hint (to look at it)
    pmknob.py attract 1000 [--invite 15000]
                                     the screen breathes after 1 s unused (0 = host owns
                                     the LED) and takes turns of 15 s between the readout
                                     and "THIS IS INTERACTIVE!" (0 = never); kept in flash
    pmknob.py reboot [--bootloader]  restart, or ask for the ROM bootloader (hold a finger
                                     on the knob's screen to confirm)
    pmknob.py flash [BUILD_DIR]      reboot into the ROM bootloader (confirmed the same way)
                                     and flash a build
    pmknob.py rescue [BUILD_DIR]     for firmware that hangs: start this, then replug the
                                     knob; it is caught in the bootloader at power-up and flashed
"""

import argparse
import glob
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib

import usb.core
import usb.util

VID, PID = 0x077D, 0x0410
ROM_VID, ROM_PID = 0x303A, 0x1001     # ESP32-S3 USB-Serial-JTAG: the ROM, or firmware that leaves it up

VENDOR_OUT = 0x40
VENDOR_IN = 0xC0
VENDOR_OUT_INTERFACE = 0x41   # how the Linux powermate driver sends LED commands

# Vendor requests; arguments and payloads are in firmware/main/usb_powermate.h.
REQ_LED = 0x01
REQ_REBOOT = 0xF0
REQ_INFO = 0xF1
REQ_INJECT = 0xF2
REQ_PULSE_LOG = 0xF3
REQ_HAPTIC = 0xF4
REQ_I2C = 0xF5
REQ_HAPTIC_SET = 0xF6
REQ_DISPLAY = 0xF7
REQ_ROTATION = 0xF8
REQ_SNAPSHOT = 0xF9
REQ_SNAPSHOT_READ = 0xFA
REQ_FORCE_NO_HOST = 0xFB
REQ_ATTRACT = 0xFC
REQ_TOUCH_LOG = 0xFD

# REQ_LED commands, the low byte of wValue (the Linux driver's SET_* values)
LED_CMD_STATIC_BRIGHTNESS = 0x01
LED_CMD_PULSE_ASLEEP = 0x02
LED_CMD_PULSE_AWAKE = 0x03
LED_CMD_PULSE_MODE = 0x04

# info["led_mode"] (pm_led_mode_t)
LED_MODE_HOST = 0             # attract mode off: as the host programmed it
LED_MODE_IN_USE = 1           # steady
LED_MODE_ATTRACT = 2          # breathing: left alone for a while
LED_MODE_NO_HOST = 3          # breathing: no USB connection

HAPTIC_EVENTS = ("press", "release", "detent")   # REQ_HAPTIC_SET's wValue

# info["screen"] (ui_screen_t)
SCREEN_READOUT = 1
SCREEN_LOGO = 2
SCREEN_INVITE = 3
SCREEN_NO_HOST = 4
SCREEN_FLASH_CONFIRM = 5
SCREENS = {0: "? (firmware before 1.4)", SCREEN_READOUT: "the readout", SCREEN_LOGO: "the RFHS logo",
           SCREEN_INVITE: "the invitation", SCREEN_NO_HOST: "the NO-USB HINT",
           SCREEN_FLASH_CONFIRM: "the FLASH PROMPT"}

DISPLAY_FORMAT = "<BBhhhQ"    # pm_display_msg_t
DISPLAY_VALID = 0x01
DISPLAY_NO_GAIN = 0x02

INFO_FORMAT = "<4sBBBBIIHBBBBBBhBBBBBBHHIIIIIIBBBBBBBBBBHHHHBBBBH"   # pm_info_t
INFO_FIELDS = (
    "magic info_version fw_major fw_minor fw_patch uptime_ms led_commands led_speed "
    "led_brightness led_table led_asleep led_awake led_level usb_suspended pending_rotation "
    "button_touch button_injected button_reported touch_chip_id touch_fingers haptic_ok "
    "touch_x touch_y touch_presses enc_steps_cw enc_steps_ccw enc_short_pulses reports_sent "
    "i2c_errors display_ok haptic_status haptic_lra_period haptic_cal_source "
    "haptic_press haptic_release haptic_detent display_rotation display_has_values plug_hint ui_fps_x10 "
    "ui_glow_us ui_refresh_ms attract_ms led_mode flash_confirm backlight_level screen invite_ms"
).split()
INFO_SIZE = struct.calcsize(INFO_FORMAT)
assert INFO_SIZE == 84
# Firmware 1.1 sent 80 bytes. Fields are only ever appended (or take the
# place of reserved zeros), so its info still reads, with zeros for the newer
# fields: a knob on old firmware can still be inspected and updated.
INFO_MIN_SIZE = 80
INFO_MAGIC = b"PMEK"


def read_info(dev):
    data = bytes(dev.ctrl_transfer(VENDOR_IN, REQ_INFO, 0, 0, 128, timeout=1000))
    if len(data) < INFO_MIN_SIZE:
        raise ValueError("info is %d bytes, expected at least %d: firmware and pmknob.py don't match" %
                         (len(data), INFO_MIN_SIZE))
    data = data.ljust(INFO_SIZE, b"\0")
    info = dict(zip(INFO_FIELDS, struct.unpack_from(INFO_FORMAT, data)))
    if info["magic"] != INFO_MAGIC:
        raise ValueError("unexpected info magic %r" % info["magic"])
    return info


def is_emulator(dev):
    """True for the PowerMate emulator; a genuine Griffin stalls the info request.

    Only the magic is checked, so firmware of another version still counts
    (and can be reflashed)."""
    try:
        data = bytes(dev.ctrl_transfer(VENDOR_IN, REQ_INFO, 0, 0, 128, timeout=1000))
    except usb.core.USBError:
        return False
    return data[:4] == INFO_MAGIC


def find_knob():
    """The emulated PowerMate, skipping any genuine Griffin units, or None."""
    for dev in usb.core.find(find_all=True, idVendor=VID, idProduct=PID):
        if is_emulator(dev):
            return dev
    return None


def require_knob():
    dev = find_knob()
    if dev is None:
        sys.exit("no PowerMate emulator found (a genuine Griffin doesn't answer the info request)")
    return dev


def cmd_info(args):
    dev = require_knob()
    i = read_info(dev)
    print("firmware        %d.%d.%d, up %.1f s" % (
        i["fw_major"], i["fw_minor"], i["fw_patch"], i["uptime_ms"] / 1000))
    print("led             brightness %d, pulse awake %d asleep %d, speed %d, table %d, "
          "rendered %d, %d commands" % (
              i["led_brightness"], i["led_awake"], i["led_asleep"], i["led_speed"], i["led_table"],
              i["led_level"], i["led_commands"]))
    print("usb             %s, %d reports sent" % (
        "suspended" if i["usb_suspended"] else "active", i["reports_sent"]))
    print("button          touch %d, injected %d, last reported %d" % (
        i["button_touch"], i["button_injected"], i["button_reported"]))
    print("encoder         %d cw, %d ccw, %d bounces rejected, %d pending" % (
        i["enc_steps_cw"], i["enc_steps_ccw"], i["enc_short_pulses"], i["pending_rotation"]))
    print("touch           chip 0x%02x, %d fingers at (%d, %d), %d presses" % (
        i["touch_chip_id"], i["touch_fingers"], i["touch_x"], i["touch_y"], i["touch_presses"]))
    print("hardware        display %s, haptic %s, %d I2C errors" % (
        "ok" if i["display_ok"] else "FAILED", "ok" if i["haptic_ok"] else "missing", i["i2c_errors"]))
    calibration = ("none", "loaded from flash", "run this boot")[min(i["haptic_cal_source"], 2)]
    period_us = i["haptic_lra_period"] * 98.46     # the DRV2605L counts it in 98.46 us steps
    resonance = "%.0f Hz" % (1e6 / period_us) if period_us else "not measured yet"
    print("haptic          calibration %s, status 0x%02x, resonance %s" % (
        calibration, i["haptic_status"], resonance))
    if not i["attract_ms"]:
        attract = "off (host owns the LED)"
    elif i["invite_ms"]:
        attract = "after %d ms idle, invitation in turns of %d ms" % (i["attract_ms"], i["invite_ms"])
    else:
        attract = "after %d ms idle, no invitation" % i["attract_ms"]
    flash_prompt = ", WAITING FOR A TOUCH TO ALLOW FLASHING" if i["flash_confirm"] else ""
    print("led ring        %s, attract mode %s%s" % (led_summary(i), attract, flash_prompt))
    print("haptic effects  press %d, release %d, detent %d" % (
        i["haptic_press"], i["haptic_release"], i["haptic_detent"]))
    print("screen          shows %s, rotation %d, %s, %.1f refreshes/s (last %d ms, ring %.1f ms)" % (
        SCREENS.get(i["screen"], "?"), i["display_rotation"],
        "readout values set" if i["display_has_values"] else "no readout values",
        i["ui_fps_x10"] / 10, i["ui_refresh_ms"], i["ui_glow_us"] / 1000))


def led_summary(info):
    """What the ring does, in a few words; starts with 'breathing' or 'steady'
    (the hardware tests rely on that)."""
    mode = info["led_mode"]
    if mode == LED_MODE_ATTRACT:
        return "breathing (attract, backlight now %d%%)" % info["backlight_level"]
    if mode == LED_MODE_NO_HOST:
        return "breathing (no host, backlight now %d%%)" % info["backlight_level"]
    if mode == LED_MODE_IN_USE:
        return "steady 255 (in use)"
    if info["led_awake"]:
        return "breathing (host)"
    return "steady %d (host)" % info["led_brightness"]


PULSE_ENTRY = struct.Struct("<HBB")   # pm_pulse_log_entry_t


def cmd_pulses(args):
    dev = require_knob()
    data = bytes(dev.ctrl_transfer(VENDOR_IN, REQ_PULSE_LOG, 0, 0, 128, timeout=1000))
    data = data[:len(data) - len(data) % PULSE_ENTRY.size]
    prev = None
    for t, channel, low in PULSE_ENTRY.iter_unpack(data):
        gap = "" if prev is None else "+%d ms" % ((t - prev) & 0xFFFF)
        prev = t
        channel_name = "A cw " if (channel & 1) == 0 else "B ccw"
        verdict = "counted" if channel & 0x80 else "REJECTED"
        print("%5d  %s  low %3d ms  %-8s %s" % (t, channel_name, low, verdict, gap))


# touch_log_header_t and touch_log_entry_t (firmware/main/touch.h)
TOUCH_HEADER = struct.Struct("<I8B")
TOUCH_ENTRY = struct.Struct("<HHBBBBHH")
TOUCH_CONFIG_REGS = (0xEC, 0xED, 0xEE, 0xF9, 0xFA, 0xFB, 0xFC, 0xFE)   # the header's 8 bytes
# indexed by touch_outcome_t
TOUCH_OUTCOMES = ("", "PRESS", "release", "IGNORED (outside the active zone)", "prompt touch, hold it",
                  "boot hold", "CONFIRMED, rebooting", "down before the prompt: lift first")
TOUCH_EVENTS = ("down", "up", "contact", "-")
# CST816 gesture codes
TOUCH_GESTURES = {0x01: "slide up", 0x02: "slide down", 0x03: "slide left", 0x04: "slide right",
                  0x05: "single click", 0x0B: "double click", 0x0C: "long press"}


def read_touch_log(dev, since=0):
    """(irq count, controller config {reg: value}, [entries after seq 'since'])."""
    data = bytes(dev.ctrl_transfer(VENDOR_IN, REQ_TOUCH_LOG, since & 0xFFFF, 0, 128, timeout=1000))
    if len(data) < TOUCH_HEADER.size:
        raise ValueError("touch log needs firmware 1.3 or later")
    irqs, *config = TOUCH_HEADER.unpack_from(data)
    entries = []
    for off in range(TOUCH_HEADER.size, len(data) - TOUCH_ENTRY.size + 1, TOUCH_ENTRY.size):
        seq, t, flags, gesture, fingers, event, x, y = TOUCH_ENTRY.unpack_from(data, off)
        entries.append({"seq": seq, "t_ms": t, "irq": bool(flags & 1), "failed": bool(flags & 2),
                        "outcome": flags >> 4, "gesture": gesture, "fingers": fingers & 0x0F,
                        "event": event, "x": x, "y": y})
    return irqs, dict(zip(TOUCH_CONFIG_REGS, config)), entries


def format_touch(e, prev_t):
    gap = "" if prev_t is None else "+%d ms" % ((e["t_ms"] - prev_t) & 0xFFFF)
    if e["failed"]:
        what = "READ FAILED"
    else:
        plural = " " if e["fingers"] == 1 else "s"
        what = "%d finger%s %-7s at (%3d, %3d)" % (
            e["fingers"], plural, TOUCH_EVENTS[e["event"] & 3], e["x"], e["y"])
        if e["gesture"]:
            what += ", gesture %s" % TOUCH_GESTURES.get(e["gesture"], "0x%02x" % e["gesture"])
    outcome = TOUCH_OUTCOMES[e["outcome"]] if e["outcome"] < len(TOUCH_OUTCOMES) else "?"
    return "%5d  %-9s %-58s %-8s %s" % (e["t_ms"], "interrupt" if e["irq"] else "poll", what, gap, outcome)


def cmd_touches(args):
    dev = require_knob()
    irqs, config, entries = read_touch_log(dev)
    print("controller registers at start-up: %s" % " ".join("%02X=%02x" % kv for kv in config.items()))
    print("%d interrupts so far; 'poll' = found by polling without an interrupt" % irqs)
    prev_t = None
    seq = 0
    for e in entries:
        print(format_touch(e, prev_t))
        prev_t, seq = e["t_ms"], e["seq"]
    if not args.watch:
        return
    print("watching, Ctrl-C to stop")
    last_irqs = irqs
    try:
        while True:
            time.sleep(0.05)
            irqs, _, entries = read_touch_log(dev, seq)
            for e in entries:
                line = format_touch(e, prev_t)
                if irqs != last_irqs:
                    line += "  [%d irqs]" % (irqs - last_irqs)
                print(line)
                prev_t, seq, last_irqs = e["t_ms"], e["seq"], irqs
    except KeyboardInterrupt:
        pass


def led_command(dev, command, index, table=0):
    """An LED command as the Linux powermate driver sends it."""
    dev.ctrl_transfer(VENDOR_OUT_INTERFACE, REQ_LED, (table << 8) | command, index, None, timeout=1000)


def cmd_led(args):
    dev = require_knob()
    if args.pulse:
        led_command(dev, LED_CMD_PULSE_MODE, 0x0001, table=args.table)   # normal speed
        led_command(dev, LED_CMD_PULSE_AWAKE, 1)
    else:
        led_command(dev, LED_CMD_PULSE_AWAKE, 0)
        led_command(dev, LED_CMD_STATIC_BRIGHTNESS, args.brightness)


def cmd_inject(args):
    dev = require_knob()
    button = 0xFFFF               # unchanged
    if args.press:
        button = 1
    elif args.release:
        button = 0
    # A request carries an int8 rotation, so a bigger turn takes several.
    # There is always at least one, which also carries the button.
    remaining = args.rotate
    while True:
        step = max(-127, min(127, remaining))
        dev.ctrl_transfer(VENDOR_OUT, REQ_INJECT, step & 0xFF, button, None, timeout=1000)
        remaining -= step
        button = 0xFFFF
        if remaining == 0:
            break


def cmd_haptic(args):
    dev = require_knob()
    if args.set:
        event, effect = args.set[0], int(args.set[1])
        if event not in HAPTIC_EVENTS:
            sys.exit("event must be one of: " + ", ".join(HAPTIC_EVENTS))
        dev.ctrl_transfer(VENDOR_OUT, REQ_HAPTIC_SET, HAPTIC_EVENTS.index(event), effect, None, timeout=1000)
        return
    effect = 0xFF if args.recalibrate else args.effect     # 0xFF: recalibrate instead
    dev.ctrl_transfer(VENDOR_OUT, REQ_HAPTIC, effect, 0, None, timeout=1000)


def cmd_i2c(args):
    dev = require_knob()
    wvalue = (int(args.addr, 0) << 8) | int(args.reg, 0)
    try:
        if args.write is not None:
            dev.ctrl_transfer(VENDOR_OUT, REQ_I2C, wvalue, int(args.write, 0), None, timeout=1000)
        else:
            data = dev.ctrl_transfer(VENDOR_IN, REQ_I2C, wvalue, 0, args.count, timeout=1000)
            print(" ".join("%02x" % b for b in data))
    except usb.core.USBError as e:
        sys.exit("refused (%s): the firmware needs CONFIG_PM_DEBUG_I2C, or the chip didn't answer" % e)


def set_attract(dev, ms, invite_ms=None):
    """Attract mode idle time (0 hands the LED to the host) and the turns of
    the invitation (0 = never; None keeps the current setting)."""
    if invite_ms is None:
        invite_ms = read_info(dev)["invite_ms"]
    dev.ctrl_transfer(VENDOR_OUT, REQ_ATTRACT, max(0, min(65535, ms)), max(0, min(65535, invite_ms)), None,
                      timeout=1000)


def cmd_attract(args):
    set_attract(require_knob(), args.ms, args.invite)


def send_display(dev, freq_hz, gain_db, gain_min=0, gain_max=76, valid=True):
    """Set the readout, as run.py does through knob_display.py. gain_db None:
    the receiver has no gain control; valid=False blanks the readout."""
    flags = (DISPLAY_VALID if valid else 0) | (DISPLAY_NO_GAIN if gain_db is None else 0)
    if gain_db is None:
        gain_db = gain_min = gain_max = 0
    msg = struct.pack(DISPLAY_FORMAT, 1, flags, int(round(gain_db * 10)),
                      int(round(gain_min * 10)), int(round(gain_max * 10)), int(freq_hz))
    dev.ctrl_transfer(VENDOR_OUT, REQ_DISPLAY, 0, 0, msg, timeout=1000)


def read_display(dev):
    size = struct.calcsize(DISPLAY_FORMAT)
    data = bytes(dev.ctrl_transfer(VENDOR_IN, REQ_DISPLAY, 0, 0, size, timeout=1000))
    if len(data) != size:
        raise ValueError("display reply is %d bytes" % len(data))
    version, flags, gain, gmin, gmax, freq = struct.unpack(DISPLAY_FORMAT, data)
    return {"valid": bool(flags & DISPLAY_VALID), "freq_hz": freq,
            "gain_db": None if flags & DISPLAY_NO_GAIN else gain / 10,
            "gain_min": gmin / 10, "gain_max": gmax / 10}


def cmd_display(args):
    dev = require_knob()
    if args.clear:
        send_display(dev, 0, 0, valid=False)
    elif args.freq is None:
        print(read_display(dev))
    else:
        send_display(dev, float(args.freq), None if args.no_gain else args.gain, args.min, args.max)


def cmd_plug_hint(args):
    dev = require_knob()
    dev.ctrl_transfer(VENDOR_OUT, REQ_FORCE_NO_HOST, 1 if args.state == "on" else 0, 0, None, timeout=1000)


def cmd_rotate(args):
    dev = require_knob()
    dev.ctrl_transfer(VENDOR_OUT, REQ_ROTATION, args.quarter_turns & 3, 0, None, timeout=1000)


def write_png(path, width, height, rgb):
    """Save 8-bit RGB pixels as a PNG, without needing an imaging library."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\0" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(rows, 9)))
        f.write(chunk(b"IEND", b""))


SNAPSHOT_HEADER = struct.Struct("<HHHBxI")   # ui_snapshot_info_t


def cmd_screenshot(args):
    dev = require_knob()
    hdr = bytes(dev.ctrl_transfer(VENDOR_IN, REQ_SNAPSHOT, 0, 0, SNAPSHOT_HEADER.size, timeout=3000))
    if len(hdr) != SNAPSHOT_HEADER.size:
        sys.exit("unexpected snapshot header (%d bytes)" % len(hdr))
    width, height, stride, cf, size = SNAPSHOT_HEADER.unpack(hdr)
    if cf != 0x12:                # LVGL's RGB565
        sys.exit("unexpected color format 0x%02x" % cf)
    # don't let a confused (or hostile) device make us read forever
    if not (0 < width <= 1024 and 0 < height <= 1024 and stride >= 2 * width and size == stride * height):
        sys.exit("implausible snapshot %dx%d, stride %d, %d bytes" % (width, height, stride, size))
    data = bytearray()
    while len(data) < size:
        off = len(data)
        chunk = bytes(dev.ctrl_transfer(VENDOR_IN, REQ_SNAPSHOT_READ, off >> 16, off & 0xFFFF,
                                        min(4096, size - off), timeout=3000))
        if not chunk:
            sys.exit("snapshot ended early at %d of %d bytes" % (off, size))
        data += chunk
    # little-endian RGB565 to 8 bits per channel, the top bits repeated into the bottom ones
    rgb = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            v = data[y * stride + 2 * x] | (data[y * stride + 2 * x + 1] << 8)
            r, g, b = (v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F
            o = (y * width + x) * 3
            rgb[o:o + 3] = bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
    write_png(args.output, width, height, bytes(rgb))
    print("saved %dx%d screenshot to %s" % (width, height, args.output))


def cmd_reboot(args):
    dev = require_knob()
    dev.ctrl_transfer(VENDOR_OUT, REQ_REBOOT, 1 if args.bootloader else 0, 0, None, timeout=1000)
    usb.util.dispose_resources(dev)
    if args.bootloader:
        print("if the knob shows 'Update firmware?', hold a finger on its screen")


def sysfs_dir(dev):
    """The /sys/bus/usb/devices entry of a pyusb device, or None."""
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(d + "/busnum") as f:
                bus = int(f.read())
            with open(d + "/devnum") as f:
                num = int(f.read())
        except (OSError, ValueError):
            continue
        if (bus, num) == (dev.bus, dev.address):
            return d
    return None


def wait_for_rom_port(timeout):
    """Wait up to timeout seconds (0: just look) for the ESP32-S3's
    USB-Serial-JTAG port; return its /dev/ttyACM* path, or None."""
    end = time.time() + timeout
    while True:
        for dev in usb.core.find(find_all=True, idVendor=ROM_VID, idProduct=ROM_PID):
            d = sysfs_dir(dev)
            ttys = glob.glob(d + "/*:*/tty/ttyACM*") if d else []
            if ttys:
                return "/dev/" + os.path.basename(ttys[0])
        if time.time() >= end:
            return None
        time.sleep(0.2)


def esptool_command(args):
    if args.esptool:
        return args.esptool.split()
    try:
        import esptool  # noqa: F401
        return [sys.executable, "-m", "esptool"]
    except ImportError:
        pass
    for exe in ("esptool.py", "esptool"):
        path = shutil.which(exe)
        if path:
            return [path]
    # ESP-IDF's install.sh keeps it in its own Python environment, which
    # works without sourcing export.sh first
    tools = os.environ.get("IDF_TOOLS_PATH", os.path.expanduser("~/.espressif"))
    found = sorted(glob.glob(os.path.join(tools, "python_env", "*", "bin", "esptool.py")))
    if found:
        return [found[-1]]
    sys.exit("esptool not found: install ESP-IDF (see firmware/README.md), run from an ESP-IDF shell "
             "(. export.sh) or pass --esptool")


def require_build(build_dir):
    if not os.path.exists(os.path.join(build_dir, "flash_args")):
        sys.exit("%s has no flash_args, run 'idf.py build' first" % build_dir)


def flash_build(args, port, before):
    """before is esptool's --before: no_reset when the chip already waits in
    the bootloader."""
    print("flashing via", port)
    # After a forced download boot the usual RTS reset leaves the chip in the
    # bootloader; a watchdog reset brings it back up properly.
    cmd = esptool_command(args) + ["--chip", "esp32s3", "-p", port, "-b", "460800", "--before", before,
                                   "--after", "watchdog_reset", "write_flash", "@flash_args"]
    if subprocess.call(cmd, cwd=os.path.abspath(args.build_dir)) != 0:
        sys.exit("esptool failed")
    end = time.time() + 10
    while time.time() < end:
        if find_knob() is not None:
            print("knob is back as a PowerMate")
            return
        time.sleep(0.3)
    sys.exit("flashed, but the knob didn't come back as a PowerMate")


def cmd_rescue(args):
    """Every power-up shows the ROM's USB serial port for a moment before the
    firmware takes USB over. Catch it, park the chip in the bootloader with
    esptool's USB-Serial-JTAG reset sequence, and flash."""
    import serial                 # pyserial, needed only here
    require_build(args.build_dir)
    print("waiting for the knob: unplug it and plug it back in")
    end = time.time() + args.timeout
    while time.time() < end:
        port = wait_for_rom_port(0)
        if port is None:
            time.sleep(0.005)     # poll fast: the port is only there for a moment
            continue
        try:
            # esptool's USBJTAGSerialReset (esptool/reset.py) with shorter waits
            p = serial.Serial()
            p.port = port
            p.dtr = p.rts = False             # idle
            p.open()
            p.dtr, p.rts = True, False        # set IO0 (boot mode)
            time.sleep(0.05)
            p.rts, p.dtr = True, False        # reset, through (1,1) rather than (0,0)
            p.rts = True                      # again: Windows only passes DTR on with an RTS write
            time.sleep(0.05)
            p.dtr = p.rts = False             # out of reset, into the bootloader
            p.close()
        except (OSError, serial.SerialException):
            continue
        time.sleep(1.5)
        port = wait_for_rom_port(0)
        if port is None:
            print("missed it, keep replugging")
            continue
        flash_build(args, port, "no_reset")
        return
    sys.exit("gave up waiting")


def cmd_flash(args):
    build = os.path.abspath(args.build_dir)
    require_build(build)
    port = wait_for_rom_port(0)
    if port is not None:
        # Already on USB-Serial-JTAG (other firmware, or the ROM): esptool can
        # reset into the bootloader itself.
        flash_build(args, port, "default_reset")
        return
    dev = require_knob()
    print("asking the knob to reboot into the ROM bootloader: if it shows "
          "'Update firmware?', hold a finger on its screen")
    dev.ctrl_transfer(VENDOR_OUT, REQ_REBOOT, 1, 0, None, timeout=1000)
    usb.util.dispose_resources(dev)
    port = wait_for_rom_port(20)
    if port is None:
        sys.exit("the bootloader's serial port didn't show up")
    flash_build(args, port, "no_reset")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info").set_defaults(func=cmd_info)
    sub.add_parser("pulses").set_defaults(func=cmd_pulses)
    p = sub.add_parser("touches")
    p.add_argument("--watch", action="store_true", help="keep printing as touches come in")
    p.set_defaults(func=cmd_touches)
    p = sub.add_parser("led")
    p.add_argument("brightness", type=int, nargs="?", default=255)
    p.add_argument("--pulse", action="store_true")
    p.add_argument("--table", type=int, default=0, help="pulse waveform, 0-2")
    p.set_defaults(func=cmd_led)
    p = sub.add_parser("inject")
    p.add_argument("--rotate", type=int, default=0)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--press", action="store_true")
    g.add_argument("--release", action="store_true")
    p.set_defaults(func=cmd_inject)
    p = sub.add_parser("haptic")
    p.add_argument("effect", type=int, nargs="?", default=1)
    p.add_argument("--recalibrate", action="store_true")
    p.add_argument("--set", nargs=2, metavar=("EVENT", "EFFECT"),
                   help="set the effect for press, release or detent (0 = none); kept in flash")
    p.set_defaults(func=cmd_haptic)
    p = sub.add_parser("i2c")
    p.add_argument("addr")
    p.add_argument("reg")
    p.add_argument("count", type=int, nargs="?", default=1)
    p.add_argument("--write")
    p.set_defaults(func=cmd_i2c)
    p = sub.add_parser("display")
    p.add_argument("freq", nargs="?", help="frequency in Hz, e.g. 2424e6; omit to read back")
    p.add_argument("gain", type=float, nargs="?", default=0)
    p.add_argument("--min", type=float, default=0, help="gain at the start of the arc")
    p.add_argument("--max", type=float, default=76, help="gain at the end of the arc")
    p.add_argument("--clear", action="store_true")
    p.add_argument("--no-gain", action="store_true", help="the receiver has no gain control")
    p.set_defaults(func=cmd_display)
    p = sub.add_parser("rotate")
    p.add_argument("quarter_turns", type=int)
    p.set_defaults(func=cmd_rotate)
    p = sub.add_parser("attract")
    p.add_argument("ms", type=int)
    p.add_argument("--invite", type=int, metavar="MS",
                   help="turns between the readout and the invitation (0 = never); default: unchanged")
    p.set_defaults(func=cmd_attract)
    p = sub.add_parser("plug-hint")
    p.add_argument("state", choices=("on", "off"))
    p.set_defaults(func=cmd_plug_hint)
    p = sub.add_parser("screenshot")
    p.add_argument("output")
    p.set_defaults(func=cmd_screenshot)
    p = sub.add_parser("reboot")
    p.add_argument("--bootloader", action="store_true")
    p.set_defaults(func=cmd_reboot)
    default_build = os.path.join(os.path.dirname(__file__), "..", "build")
    esptool_help = "esptool command to use (default: python -m esptool)"
    p = sub.add_parser("flash")
    p.add_argument("build_dir", nargs="?", default=default_build)
    p.add_argument("--esptool", help=esptool_help)
    p.set_defaults(func=cmd_flash)
    p = sub.add_parser("rescue")
    p.add_argument("build_dir", nargs="?", default=default_build)
    p.add_argument("--esptool", help=esptool_help)
    p.add_argument("--timeout", type=float, default=600, help="seconds to wait for the replug")
    p.set_defaults(func=cmd_rescue)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
