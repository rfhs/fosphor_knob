"""fosphor_knob.sh with some SDRs' flowgraphs left out when it was installed.

Runs the script against a scratch install directory (FOSPHOR_KNOB_SHARE)
and a stub lsusb, and only down paths that stop before anything is started:
the knob check (a stub run.py --check) always fails.
"""

import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "fosphor_knob.sh")

USB_IDS = {"uhd": "2500:0022", "bladerf2": "2cf0:5250", "hackrf": "1d50:6089", "rtlsdr": "0bda:2838"}
LSUSB = """#!/bin/sh
for id in ${FAKE_USB}; do
  [ "${id}" = "${2}" ] && echo "Bus 001 Device 002: ID ${id} fake"
done
exit 0
"""
# Stand-ins for everything the script could reach if a test went wrong.
HARMLESS = ("wmctrl", "xtrlock", "sleep", "hackrf_sweep", "mkfifo")


def executable(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


class Launcher(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.share = os.path.join(tmp.name, "share")
        self.bin = os.path.join(tmp.name, "bin")
        os.mkdir(self.share)
        os.mkdir(self.bin)
        executable(os.path.join(self.bin, "lsusb"), LSUSB)
        for name in HARMLESS:
            executable(os.path.join(self.bin, name), "#!/bin/sh\nexit 0\n")
        executable(os.path.join(self.share, "run.py"), "#!/bin/sh\necho knob-check\nexit 1\n")
        self.tmp = tmp.name

    def install(self, *sdrs):
        for sdr in sdrs:
            for suffix in ("", "_sponsors"):
                executable(os.path.join(self.share, "fosphor_knob_%s%s.py" % (sdr, suffix)),
                           "#!/bin/sh\necho started %s\n" % sdr)

    def run_as(self, name, plugged=()):
        link = os.path.join(self.tmp, name)
        if not os.path.exists(link):
            os.symlink(SCRIPT, link)
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                   FOSPHOR_KNOB_SHARE=self.share, FAKE_USB=" ".join(USB_IDS[s] for s in plugged))
        result = subprocess.run(["sh", link], env=env, capture_output=True, text=True, timeout=30)
        return result.returncode, result.stdout + result.stderr

    def test_mode_left_out(self):
        self.install("uhd", "bladerf2", "hackrf")
        for name, sdr in (("fosphor_knob_rtlsdr", "RTL-SDR"), ("fosphor_knob_rtlsdr_sponsors", "RTL-SDR")):
            code, out = self.run_as(name)
            self.assertEqual(code, 1)
            self.assertIn("Support for the %s was disabled when fosphor_knob was installed" % sdr, out)
            self.assertNotIn("knob-check", out, "says so before checking the knob")

    def test_bladerf_and_sweep_left_out(self):
        self.install("uhd")
        for name, sdr in (("fosphor_knob_bladerf", "bladeRF 2.0"), ("fosphor_knob_hackrf_sweep", "HackRF")):
            code, out = self.run_as(name)
            self.assertEqual(code, 1)
            self.assertIn("Support for the %s was disabled" % sdr, out)

    def test_installed_mode_goes_on_to_the_knob_check(self):
        self.install("rtlsdr")
        code, out = self.run_as("fosphor_knob_rtlsdr")
        self.assertEqual(code, 1)
        self.assertIn("knob-check", out)
        self.assertNotIn("disabled", out)

    def test_autodetect_skips_sdrs_left_out(self):
        self.install("rtlsdr")
        for name in ("fosphor_knob", "fosphor_knob_sponsors"):
            code, out = self.run_as(name, plugged=("uhd", "rtlsdr"))
            self.assertIn("Found a USRP, but support for it was disabled when fosphor_knob was installed", out)
            self.assertIn("knob-check", out, "then goes on with the RTL-SDR")

    def test_autodetect_only_sdrs_left_out(self):
        self.install("uhd")
        code, out = self.run_as("fosphor_knob", plugged=("hackrf",))
        self.assertEqual(code, 1)
        self.assertIn("Found a HackRF, but support for it was disabled", out)
        self.assertNotIn("knob-check", out)
        self.assertNotIn("Failed to autodetect", out)

    def test_nothing_plugged_in(self):
        self.install("uhd", "bladerf2", "hackrf", "rtlsdr")
        code, out = self.run_as("fosphor_knob")
        self.assertEqual(code, 1)
        self.assertIn("Failed to autodetect supported hardware", out)


if __name__ == "__main__":
    unittest.main()
