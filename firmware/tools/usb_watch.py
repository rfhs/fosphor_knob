#!/usr/bin/env python3
"""Watch USB devices come and go and say what each one is.

Made for checking what the Waveshare knob turns into when its USB-C plug is
flipped: run it, flip the plug, flip it back, read the log.

    usb_watch.py [--seconds N]
"""

import argparse
import errno
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import usb.core
    import pmknob
except ImportError:          # identification by IDs only, without pyusb
    usb = None


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def describe(sysdir):
    vid, pid = read(sysdir + "/idVendor"), read(sysdir + "/idProduct")
    strings = " / ".join(s for s in (read(sysdir + "/manufacturer"), read(sysdir + "/product")) if s)
    # cdc_acm puts its tty at <interface>/tty/ttyACM0, usb-serial at <interface>/ttyUSB0
    ttys = glob.glob(sysdir + "/*:*/tty/*") + glob.glob(sysdir + "/*:*/tty?*")
    ttys = sorted(os.path.basename(t) for t in ttys)
    drivers = sorted({os.path.basename(os.path.realpath(d)) for d in glob.glob(sysdir + "/*:*/driver")})

    what = "unknown device"
    if (vid, pid) == ("077d", "0410"):
        what = "PowerMate (could not ask it which kind)"
        if usb is not None:
            bus, num = int(read(sysdir + "/busnum")), int(read(sysdir + "/devnum"))
            dev = usb.core.find(bus=bus, address=num)
            if dev is not None:
                try:
                    info = pmknob.read_info(dev)
                    what = "Waveshare knob running the PowerMate emulator firmware %d.%d.%d" % (
                        info["fw_major"], info["fw_minor"], info["fw_patch"])
                except usb.core.USBError as e:
                    if e.errno == errno.EACCES:
                        what = "PowerMate (no permission to ask it which kind)"
                    else:
                        what = "genuine Griffin PowerMate (does not answer the emulator's info request)"
                except ValueError:
                    what = "genuine Griffin PowerMate (does not answer the emulator's info request)"
    elif (vid, pid) == ("303a", "1001"):
        what = "ESP32-S3 USB-Serial-JTAG: the knob's main chip in its ROM bootloader or other firmware"
    elif vid == "1a86":
        what = "WCH CH34x USB-serial adapter: on the knob, the path to the second chip (ESP32-U4WDH)"
    elif vid == "303a":
        what = "Espressif device"

    extra = []
    if drivers:
        extra.append("driver " + ",".join(drivers))
    if ttys:
        extra.append("/dev/" + ",/dev/".join(ttys))
    return "%s:%s %s -> %s%s" % (vid, pid, "(%s)" % strings if strings else "", what,
                                 " [%s]" % "; ".join(extra) if extra else "")


def snapshot():
    """{port: (bus, devnum)} of the attached devices; a device that comes back
    on the same port gets a new devnum."""
    devs = {}
    for d in glob.glob("/sys/bus/usb/devices/*"):
        name = os.path.basename(d)
        if ":" in name or name.startswith("usb") or not os.path.exists(d + "/idVendor"):
            continue
        devs[name] = (read(d + "/busnum"), read(d + "/devnum"))
    return devs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=600, help="how long to watch")
    args = ap.parse_args()

    t0 = time.time()

    def log(msg):
        print("%s +%6.1fs  %s" % (time.strftime("%H:%M:%S"), time.time() - t0, msg), flush=True)

    seen = snapshot()
    for port in sorted(seen):
        log("present  port %-6s %s" % (port, describe("/sys/bus/usb/devices/" + port)))
    log("watching for %d s: flip the knob's plug now" % args.seconds)
    while time.time() - t0 < args.seconds:
        time.sleep(0.1)
        now = snapshot()
        for port in sorted(set(seen) - set(now)):
            log("REMOVED  port %s" % port)
            del seen[port]
        for port in sorted(now):
            if seen.get(port) == now[port]:
                continue
            time.sleep(0.3)    # let drivers bind and tty nodes appear
            sysdir = "/sys/bus/usb/devices/" + port
            if snapshot().get(port) != now[port] or not read(sysdir + "/idVendor"):
                continue       # still re-enumerating: report it once it settles
            log("ADDED    port %-6s %s" % (port, describe(sysdir)))
            seen[port] = now[port]
    log("done")


if __name__ == "__main__":
    main()
