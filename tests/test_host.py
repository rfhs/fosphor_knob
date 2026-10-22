#!/usr/bin/env python3
"""Hardware-free tests of the host-side code, run by CI.

    python3 -m unittest discover -s tests -v

tests/run_all.py runs these and the hardware tests.
"""

import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import xmlrpc.client

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "firmware", "tools"))

import fake_flowgraph  # noqa: E402
import knob_display  # noqa: E402
import powermate  # noqa: E402
import run  # noqa: E402
from test_firmware import firmware_struct_size  # noqa: E402

try:
    import griffin_test  # needs pyusb
except ImportError:
    griffin_test = None


class WireFormats(unittest.TestCase):
    """knob_display.py and the firmware must agree on the readout message."""

    def test_display_message_size(self):
        self.assertEqual(len(knob_display._message(1, 2, 3, 4, True)), firmware_struct_size("pm_display_msg_t"))

    def test_display_message_fields(self):
        msg = knob_display._message(2424e6, 32.5, 0, 76, True)
        self.assertEqual(struct.unpack("<BBhhhQ", msg), (1, 1, 325, 0, 760, 2424000000))
        self.assertEqual(struct.unpack("<BBhhhQ", knob_display._message(0, 0, 0, 0, False))[1], 0)

    def test_display_message_without_gain(self):
        version, flags, gain, gmin, gmax, freq = struct.unpack(
            "<BBhhhQ", knob_display._message(2400e6, None, 0, 76, True))
        self.assertEqual(flags, knob_display.DISPLAY_VALID | knob_display.DISPLAY_NO_GAIN)

    def test_display_message_clamps(self):
        version, flags, gain, gmin, gmax, freq = struct.unpack(
            "<BBhhhQ", knob_display._message(-5, 1e9, -1e9, 0, True))
        self.assertEqual((gain, gmin, freq), (32767, -32768, 0))


class GriffinTest(unittest.TestCase):
    """griffin_test.py reads the input device's capabilities from sysfs."""

    @unittest.skipIf(griffin_test is None, "pyusb not installed")
    def test_capability_bits(self):
        bits = griffin_test.LONG_BITS
        # BTN_0 (0x100) as the kernel prints it: one word per long, most significant first
        words = ["0"] * (0x100 // bits + 1)
        words[0] = "%x" % (1 << (0x100 % bits))
        key = " ".join(words)
        self.assertTrue(griffin_test.has_bit(key, 0x100))
        self.assertFalse(griffin_test.has_bit(key, 0x101))
        self.assertFalse(griffin_test.has_bit("1", 0x100))
        self.assertTrue(griffin_test.has_bit("17", 2))      # ev: SYN KEY REL MSC
        self.assertTrue(griffin_test.has_bit("17", 4))
        self.assertFalse(griffin_test.has_bit("17", 3))


class KnobDisplay(unittest.TestCase):
    def test_no_knob_is_harmless(self):
        # Pretend nothing is plugged in, even on a machine with a knob attached.
        real = knob_display._device_nodes
        knob_display._device_nodes = lambda: iter(())
        try:
            knob_display.show(915e6, 30)
            knob_display.clear()
        finally:
            knob_display._device_nodes = real


class FakeFlowgraph(unittest.TestCase):
    def test_round_trip(self):
        fg = fake_flowgraph.FakeFlowgraph(freq=100e6, gain=10)
        server = fake_flowgraph.serve(fg, port=0)
        try:
            port = server.server_address[1]
            proxy = xmlrpc.client.ServerProxy("http://127.0.0.1:%d" % port)
            self.assertEqual(proxy.get_freq(), 100e6)
            proxy.set_freq(101e6)
            proxy.set_gain(11.0)
            self.assertEqual(fg.take(), [("freq", 101000000), ("gain", 11)])
            self.assertEqual(proxy.get_gain(), 11.0)
        finally:
            server.shutdown()
            server.server_close()


class FindKnob(unittest.TestCase):
    """The PowerMate comes first; the plug warning only without one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.byid = os.path.join(self.tmp.name, "by-id")
        self.sysfs = os.path.join(self.tmp.name, "sysfs")
        os.makedirs(self.byid)
        os.makedirs(self.sysfs)

    def tearDown(self):
        self.tmp.cleanup()

    def add_usb(self, port, vid, pid):
        d = os.path.join(self.sysfs, port)
        os.makedirs(d)
        for name, value in (("idVendor", vid), ("idProduct", pid)):
            with open(os.path.join(d, name), "w") as f:
                f.write(value + "\n")

    def add_knob(self):
        path = os.path.join(self.byid, "usb-Griffin_Technology_Inc._Griffin_PowerMate-event-if00")
        open(path, "w").close()
        return path

    def find(self):
        return run.findKnob(os.path.join(self.byid, "*PowerMate*"), self.sysfs)

    def test_knob_wins_even_with_a_ch340(self):
        knob = self.add_knob()
        self.add_usb("5-1", "1a86", "7523")
        self.assertEqual(self.find(), (knob, None))

    def test_ch340_without_knob_warns_about_the_plug(self):
        self.add_usb("5-1", "1a86", "7523")
        device, problem = self.find()
        self.assertIsNone(device)
        self.assertIn("flip it over", problem)

    def test_nothing_found(self):
        self.add_usb("3-3", "32ac", "001c")
        self.assertEqual(self.find(), (None, "No PowerMate found."))


class IdleBreathing(unittest.TestCase):
    """run.py breathes the LED when idle and holds it steady while in use."""

    def make_knob(self):
        pm = run.ExamplePowerMate.__new__(run.ExamplePowerMate)
        powermate.PowerMateEventHandler.__init__(pm)
        pm._lock = threading.Lock()
        pm._last_use = 0.0
        pm._breathing = False
        pm._drive_led = True          # a real Griffin
        pm.sent = []
        pm.send = pm.sent.append
        return pm

    def led_values(self, pm):
        return [e.value for e in pm.sent]

    def feed(self, pm, event):
        # what EventHandler.handle_events does with each event
        try:
            pm.handle_event(event)
        except powermate.EventNotImplemented:
            pass

    def test_starts_breathing_when_idle(self):
        pm = self.make_knob()
        pm._idle_tick()
        pm._idle_tick()
        self.assertEqual(self.led_values(pm), [powermate.LedEvent.pulse().value])

    def test_use_makes_it_steady_and_idle_brings_breathing_back(self):
        pm = self.make_knob()
        pm._idle_tick()
        self.feed(pm, powermate.Event(0, 0, powermate.ROTATE, 7, 0))   # REL_DIAL 0: no tuning
        self.assertEqual(self.led_values(pm)[-1], powermate.LedEvent(brightness=powermate.MAX_BRIGHTNESS).value)
        pm._idle_tick()
        self.assertEqual(len(pm.sent), 2, "no breathing right after use")
        pm._last_use = time.time() - run.idleSeconds - 1
        pm._idle_tick()
        self.assertEqual(self.led_values(pm)[-1], powermate.LedEvent.pulse().value)

    def test_idle_watcher_stops_when_the_knob_is_gone(self):
        pm = self.make_knob()

        def gone(event):
            raise OSError(19, "No such device")
        pm.send = gone
        done = threading.Event()

        def watch():
            pm._watch_idle()
            done.set()
        threading.Thread(target=watch, daemon=True).start()
        self.assertTrue(done.wait(2), "_watch_idle should return once sending fails")

    def test_emulator_led_is_left_to_its_firmware(self):
        pm = self.make_knob()
        pm._drive_led = False
        self.feed(pm, powermate.Event(0, 0, powermate.ROTATE, 7, 0))
        self.assertEqual(pm.sent, [], "run.py must not fight the knob's own attract mode")

    def test_led_echo_is_not_use(self):
        pm = self.make_knob()
        pm._idle_tick()
        self.feed(pm, powermate.Event(0, 0, powermate.EV_MSC, powermate.MSC_PULSELED, 0))
        self.assertEqual(len(pm.sent), 1, "EV_MSC (our own LED writes) must not stop the breathing")


if __name__ == "__main__":
    unittest.main()
