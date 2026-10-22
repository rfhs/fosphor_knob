#!/usr/bin/env python3
"""Show the receiver's frequency and gain on the knob's screen.

Only the Waveshare ESP32-S3 knob running the PowerMate emulator firmware
(firmware/ in this repo) has a screen. A genuine Griffin PowerMate refuses the
request and is left alone after that, so calling show() is always safe.
is_emulator() tells the two apart for run.py.

Talks to the knob with USB vendor requests through usbfs, using nothing but
the standard library. That needs read/write access to the knob's
/dev/bus/usb node (root, or the udev rule in udev/). show() never raises: no
knob, no permission or an unplugged knob just means nothing is displayed.
"""

import ctypes
import fcntl
import glob
import os
import struct
import time

VENDOR_ID = "077d"
PRODUCT_ID = "0410"
VENDOR_OUT = 0x40           # bmRequestType: vendor, to the device
VENDOR_IN = 0xC0            # bmRequestType: vendor, from the device
REQ_INFO = 0xF1             # see firmware/main/usb_powermate.h
REQ_DISPLAY = 0xF7
DISPLAY_VERSION = 1
DISPLAY_VALID = 0x01
DISPLAY_NO_GAIN = 0x02
TIMEOUT_MS = 200            # the knob answers in a few ms
RESCAN_SECONDS = 5          # with no knob found, look again at most this often


class _CtrlTransfer(ctypes.Structure):
    """struct usbdevfs_ctrltransfer from <linux/usbdevice_fs.h>"""
    _fields_ = [
        ("bRequestType", ctypes.c_uint8),
        ("bRequest", ctypes.c_uint8),
        ("wValue", ctypes.c_uint16),
        ("wIndex", ctypes.c_uint16),
        ("wLength", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),   # ms
        ("data", ctypes.c_void_p),
    ]


# _IOWR('U', 0, struct usbdevfs_ctrltransfer)
_USBDEVFS_CONTROL = (3 << 30) | (ctypes.sizeof(_CtrlTransfer) << 16) | (ord("U") << 8)

_knobs = {}          # device node -> open fd, for knobs that accept the request
_not_knobs = set()   # nodes that failed the request (genuine PowerMates, mostly), until replugged
_last_scan = 0.0


def _sysfs_attr(dev, name):
    with open(os.path.join(dev, name)) as f:
        return f.read().strip()


def _dev_node(dev):
    """The /dev/bus/usb node of a USB device directory in sysfs."""
    bus = int(_sysfs_attr(dev, "busnum"))
    num = int(_sysfs_attr(dev, "devnum"))
    return "/dev/bus/usb/%03d/%03d" % (bus, num)


def _device_nodes():
    """/dev/bus/usb nodes of every 077d:0410 device: Griffins and emulators."""
    for dev in glob.glob("/sys/bus/usb/devices/*"):
        try:
            vid = _sysfs_attr(dev, "idVendor")
            pid = _sysfs_attr(dev, "idProduct")
            if vid != VENDOR_ID or pid != PRODUCT_ID:
                continue
            node = _dev_node(dev)
        except (OSError, ValueError):
            continue
        yield node


def _control(fd, request_type, request, buf):
    """A control transfer of len(buf) bytes; returns how many were moved."""
    xfer = _CtrlTransfer(request_type, request, 0, 0, len(buf), TIMEOUT_MS, ctypes.addressof(buf))
    return fcntl.ioctl(fd, _USBDEVFS_CONTROL, xfer)


def _send(fd, payload):
    _control(fd, VENDOR_OUT, REQ_DISPLAY, ctypes.create_string_buffer(payload, len(payload)))


def usb_node(event_path):
    """The /dev/bus/usb node of the USB device behind an input event device."""
    name = os.path.basename(os.path.realpath(event_path))
    # walk up from the input device in sysfs to the USB device it belongs to
    d = os.path.realpath(os.path.join("/sys/class/input", name))
    while d != "/":
        try:
            return _dev_node(d)
        except (OSError, ValueError):
            d = os.path.dirname(d)
    return None


def is_emulator(event_path):
    """True if the input device is a Waveshare knob running the emulator
    firmware: its info reply starts with "PMEK", a Griffin stalls the request."""
    node = usb_node(event_path)
    if node is None:
        return False
    try:
        fd = os.open(node, os.O_RDWR)
    except OSError:
        return False
    try:
        magic = ctypes.create_string_buffer(4)     # only the start of pm_info_t
        return _control(fd, VENDOR_IN, REQ_INFO, magic) == 4 and magic.raw == b"PMEK"
    except OSError:
        return False
    finally:
        os.close(fd)


def _scan(payload):
    """Look for knobs, using the display request itself as the probe."""
    global _last_scan
    _last_scan = time.time()
    for node in _device_nodes():
        if node in _knobs or node in _not_knobs:
            continue
        try:
            fd = os.open(node, os.O_RDWR)
        except OSError:
            continue        # no access: try again next scan
        try:
            _send(fd, payload)
            _knobs[node] = fd
        except OSError:
            # A Griffin stalls the request. Any other failure also rules the
            # node out; a replugged knob comes back with a new node.
            _not_knobs.add(node)
            os.close(fd)


def _message(freq_hz, gain_db, gain_min, gain_max, valid):
    """pm_display_msg_t from firmware/main/usb_powermate.h"""
    def tenths(db):
        return max(-32768, min(32767, int(round(db * 10))))
    flags = DISPLAY_VALID if valid else 0
    if gain_db is None:
        flags |= DISPLAY_NO_GAIN
        gain_db = gain_min = gain_max = 0
    return struct.pack("<BBhhhQ", DISPLAY_VERSION, flags, tenths(gain_db), tenths(gain_min),
                       tenths(gain_max), max(0, min(2 ** 64 - 1, int(freq_hz))))


def _publish(payload):
    lost = False
    for node, fd in list(_knobs.items()):
        try:
            _send(fd, payload)
        except OSError:
            # unplugged or reset: forget it, and look again right away
            os.close(fd)
            del _knobs[node]
            lost = True
    if not _knobs and (lost or time.time() - _last_scan >= RESCAN_SECONDS):
        _scan(payload)


def show(freq_hz, gain_db, gain_min=0, gain_max=76):
    """Display a frequency (Hz) and a gain (dB) with its range; a gain of
    None means the receiver has no gain control."""
    try:
        _publish(_message(freq_hz, gain_db, gain_min, gain_max, True))
    except Exception:
        pass


def clear():
    """Blank the readout."""
    try:
        _publish(_message(0, 0, 0, 0, False))
    except Exception:
        pass


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3:
        show(float(sys.argv[1]), float(sys.argv[2]))
    elif len(sys.argv) == 2 and sys.argv[1] == "clear":
        clear()
    else:
        sys.exit("usage: knob_display.py FREQ_HZ GAIN_DB | clear")
    print("knob found" if _knobs else "no knob found (or no permission)")
