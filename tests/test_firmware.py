#!/usr/bin/env python3
"""Hardware-free checks of the Waveshare knob's firmware tools, run by CI.

    python3 -m unittest discover -s tests -p test_firmware.py -v

The firmware pins the size of every struct it sends or receives over USB
with a _Static_assert, and firmware/tools/pmknob.py unpacks them by position,
so the two must agree.
"""

import glob
import os
import re
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "firmware", "tools"))

try:
    import pmknob  # needs pyusb
except ImportError:
    pmknob = None

FIRMWARE_MAIN = os.path.join(REPO, "firmware", "main")


def firmware_struct_size(name):
    """The size a _Static_assert in the firmware pins a wire struct to."""
    for path in sorted(glob.glob(os.path.join(FIRMWARE_MAIN, "*.[ch]"))):
        with open(path) as f:
            m = re.search(r"_Static_assert\(sizeof\(%s\) == (\d+)" % name, f.read())
        if m:
            return int(m.group(1))
    raise AssertionError("no _Static_assert for %s in the firmware" % name)


class WireFormats(unittest.TestCase):
    @unittest.skipIf(pmknob is None, "pyusb not installed")
    def test_pmknob_formats(self):
        self.assertEqual(pmknob.INFO_SIZE, firmware_struct_size("pm_info_t"))
        self.assertEqual(struct.calcsize(pmknob.DISPLAY_FORMAT), firmware_struct_size("pm_display_msg_t"))
        self.assertEqual(pmknob.DISPLAY_FORMAT, "<BBhhhQ")
        self.assertEqual(pmknob.TOUCH_HEADER.size, firmware_struct_size("touch_log_header_t"))
        self.assertEqual(pmknob.TOUCH_ENTRY.size, firmware_struct_size("touch_log_entry_t"))
        self.assertEqual(pmknob.SNAPSHOT_HEADER.size, firmware_struct_size("ui_snapshot_info_t"))


if __name__ == "__main__":
    unittest.main()
