#!/usr/bin/env python3
from powermate import PowerMateBase, LedEvent, MAX_BRIGHTNESS, PUSH, ROTATE
import knob_display
import glob
import math
import os
import sys
import threading
import time
import xmlrpc.client

targetip = "127.0.0.1"

'''
Edit the presets to values in MHz.
Example:
presets = [750, 877, 915, 1950, 2134, 2424, 2530]
'''
presets = [915, 1950, 2134, 2424, 5160, 140, 433]

pos = 0
currentFreq = -1
step = 1000000
currentGain = -1
# Used when the flowgraph doesn't publish the SDR's limits (get_knob_limits).
minGain = 0
maxGain = 76

# What the flowgraph says the knob may do (sdr_limits.describe()), or None.
limits = None
# The SDR's gain range can change with the band, so it is asked again after
# tuning, before the gain is changed.
limitsStale = False

# A Griffin's LED breathes to attract visitors after this many seconds
# without use, and goes steady as soon as it is used. The Waveshare knob's
# firmware does this itself, with its own idle time.
idleSeconds = float(os.environ.get('FOSPHOR_KNOB_IDLE_SECONDS', 1))


def gainRange():
    if limits:
        return limits['gain_min'], limits['gain_max']
    return minGain, maxGain


def showReadout():
    knob_display.show(currentFreq, currentGain, *gainRange())


def setFreq(freq1):
    freq = float(freq1)
    xml = xmlrpc.client.Server('http://' + targetip + ':8080')
    xml.set_freq(freq)
    knob_display.show(freq, currentGain, *gainRange())


def getFreq():
    global currentFreq
    xml = xmlrpc.client.Server('http://' + targetip + ':8080')
    currentFreq = int(xml.get_freq())
    print("Found initial freq %s" % currentFreq)


def getGain():
    global currentGain
    xml = xmlrpc.client.Server('http://' + targetip + ':8080')
    try:
        currentGain = float(xml.get_gain())
        print("Found gain %s" % currentGain)
    except xmlrpc.client.Fault:
        # the hackrf_sweep flowgraphs have no gain control
        currentGain = None
        print("This flowgraph has no gain control")


def setGain(gain1):
    gain = float(gain1)
    xml = xmlrpc.client.Server('http://' + targetip + ':8080')
    xml.set_gain(gain)
    knob_display.show(currentFreq, gain, *gainRange())


def clampFreq(freq, limits):
    """freq (Hz) moved inside the limits, on whole Hz."""
    if not limits:
        return freq
    return min(max(freq, math.ceil(limits['freq_min'])), math.floor(limits['freq_max']))


def snapGain(gain, limits):
    """The gain the SDR can really do nearest to gain."""
    if not limits:
        return gain
    if limits.get('gain_values'):
        return min(limits['gain_values'], key=lambda v: abs(v - gain))
    return min(max(gain, limits['gain_min']), limits['gain_max'])


def nextGain(gain, direction, limits):
    """The gain one click up (direction 1) or down (-1), or None at the edge."""
    if limits and limits.get('gain_values'):
        if direction > 0:
            higher = [v for v in limits['gain_values'] if v > gain + 1e-6]
            return min(higher) if higher else None
        lower = [v for v in limits['gain_values'] if v < gain - 1e-6]
        return max(lower) if lower else None
    low, high = (limits['gain_min'], limits['gain_max']) if limits else (minGain, maxGain)
    step = max(limits.get('gain_step', 0), 1) if limits else 1
    new = min(max(gain + direction * step, low), high)
    return None if new == gain else new


def nextPreset(pos, presets, limits):
    """(index, Hz) of the next preset after pos that the SDR can tune to,
    or None when it can't tune to any."""
    for i in range(1, len(presets) + 1):
        p = (pos + i) % len(presets)
        freq = presets[p] * 1000000
        if not limits or limits['freq_min'] <= freq <= limits['freq_max']:
            return p, freq
    return None


def fetchLimits(wait=30):
    """Ask the flowgraph what the knob may do: a dict, or None when it
    can't say (an older flowgraph, or the driver couldn't tell)."""
    xml = xmlrpc.client.Server('http://' + targetip + ':8080')
    deadline = time.time() + wait
    while True:
        try:
            got = xml.get_knob_limits()
        except xmlrpc.client.Fault:
            return None
        if got.get('error'):
            print("The SDR's limits are unknown (%s): no limits" % got['error'])
            return None
        if got:
            return got
        # {}: the flowgraph is still opening the SDR
        if time.time() > deadline:
            print("The flowgraph didn't tell the SDR's limits within %d s: no limits" % wait)
            return None
        time.sleep(0.2)


def refreshLimits():
    """Ask for the limits again and bring the frequency and gain inside them."""
    global limits, limitsStale, currentFreq, currentGain
    limits = fetchLimits()
    limitsStale = False
    if limits is None:
        return
    freq = clampFreq(currentFreq, limits)
    if freq != currentFreq:
        currentFreq = freq
        setFreq(freq)
    if currentGain is not None:
        gain = snapGain(currentGain, limits)
        if gain != currentGain:
            currentGain = gain
            setGain(gain)


def changeGain(direction):
    global currentGain
    if currentGain is None:
        return
    if limitsStale:
        refreshLimits()
    gain = nextGain(currentGain, direction, limits)
    if gain is None:
        knob_display.edge()
        return
    currentGain = gain
    print("setting Gain %s" % currentGain)
    setGain(currentGain)


def incGain():
    changeGain(1)


def decGain():
    changeGain(-1)


def tune(freq):
    global currentFreq, limitsStale
    freq = clampFreq(freq, limits)
    if freq == currentFreq:
        knob_display.edge()
        return
    currentFreq = freq
    limitsStale = limits is not None
    print("setting freq %s" % currentFreq)
    setFreq(currentFreq)


def incfreq():
    tune(currentFreq + step)


def decfreq():
    tune(currentFreq - step)


def preset():
    global pos, currentFreq, limitsStale
    found = nextPreset(pos, presets, limits)
    if found is None:
        knob_display.edge()
        return
    pos, currentFreq = found
    limitsStale = limits is not None
    setFreq(currentFreq)


def wait_for_getFreq():
    max_time = 19
    while 1:
        if (max_time < 0):
            print("Gave up waiting for xmlrpc startup")
            exit(1)
        try:
            getFreq()
            print("We get signal")
            break
        except:
            print("Waiting for xmlrpc startup... %s" % max_time)
            time.sleep(1)
            max_time = max_time - 1


def usbDevicePresent(vendor, product, sysfs='/sys/bus/usb/devices'):
    for dev in glob.glob(os.path.join(sysfs, '*')):
        try:
            with open(os.path.join(dev, 'idVendor')) as f:
                vid = f.read().strip()
            with open(os.path.join(dev, 'idProduct')) as f:
                pid = f.read().strip()
        except OSError:
            continue
        if (vid, pid) == (vendor, product):
            return True
    return False


def findKnob(byId='/dev/input/by-id/*PowerMate*', sysfs='/sys/bus/usb/devices'):
    """Return (device, None) for the knob, or (None, what is wrong)."""
    knobs = glob.glob(byId)
    if knobs:
        return knobs[0], None
    # The Waveshare knob plugged in upside down shows up as its second
    # chip's CH340 serial adapter instead of a PowerMate.
    if usbDevicePresent('1a86', '7523', sysfs):
        return None, ("No PowerMate found, but a CH340 USB serial adapter "
                      "(1a86:7523) is connected. If that is the Waveshare knob, "
                      "its USB-C plug is in upside down: flip it over at the knob.")
    return None, "No PowerMate found."


def permissionProblem(device):
    return ("No permission to read and write %s. run.py needs it to talk to "
            "the knob: run it as root, add the user to the 'input' group, or "
            "add a udev rule for the knob." % os.path.realpath(device))


def check():
    """Startup check: is a knob plugged in, and can we use it?"""
    device, problem = findKnob()
    if device is None:
        print(problem)
        return 1
    if not os.access(device, os.R_OK | os.W_OK):
        print(permissionProblem(device))
        return 1
    if knob_display.is_emulator(device):
        print("Waveshare knob ready: %s" % device)
        return 0
    # Without access to its USB node we can't ask the knob what it is, so it
    # may still be the Waveshare knob.
    node = knob_display.usb_node(device)
    if node and not os.access(node, os.R_OK | os.W_OK):
        print("Knob ready: %s. No access to %s, so if this is the Waveshare "
              "knob its readout will stay blank." % (device, node))
    else:
        print("Griffin PowerMate ready: %s" % device)
    return 0


def waitForKnob():
    """Find the knob, waiting for it to be plugged in (again)."""
    reported = None
    while True:
        device, problem = findKnob()
        if device:
            return device
        if problem != reported:
            print(problem + " Waiting for it...")
            reported = problem
        time.sleep(1)


class ExamplePowerMate(PowerMateBase):
    def __init__(self, path):
        wait_for_getFreq()
        getGain()
        refreshLimits()
        if limits:
            print("Limits: %.3f to %.3f MHz, gain %g to %g dB" % (
                limits['freq_min'] / 1e6, limits['freq_max'] / 1e6, limits['gain_min'], limits['gain_max']))
        showReadout()
        # LED state, shared by the event thread and the idle thread; both send
        # their LED events under _lock so one can't overtake the other. It is
        # set up before super().__init__(), which starts the event thread.
        self._lock = threading.Lock()
        self._last_use = 0.0        # long ago: breathe from the start
        self._breathing = False
        # The Waveshare knob's firmware runs its own LED (steady in use,
        # breathing when idle); only a real Griffin needs us to drive it.
        self._drive_led = not knob_display.is_emulator(path)
        super(ExamplePowerMate, self).__init__(path)
        if self._drive_led:
            idle = threading.Thread(target=self._watch_idle)
            idle.daemon = True
            idle.start()

    def handle_event(self, event):
        # Only turns and presses are use; the LED events we write come back
        # to us as EV_MSC and must not count.
        if event.type in (PUSH, ROTATE):
            self._in_use()
        return super(ExamplePowerMate, self).handle_event(event)

    def _in_use(self):
        if not self._drive_led:
            return
        with self._lock:
            self._last_use = time.time()
            if self._breathing:
                self._breathing = False
                self.send(LedEvent(brightness=MAX_BRIGHTNESS))

    def _watch_idle(self):
        while True:
            try:
                self._idle_tick()
            except OSError:
                return      # the knob is gone; a new instance takes over
            time.sleep(0.2)

    def _idle_tick(self):
        with self._lock:
            if not self._breathing and time.time() - self._last_use >= idleSeconds:
                self._breathing = True
                self.send(LedEvent.pulse())

    def short_press(self):
        print('Short press!')
        preset()

    def long_press(self):
        print('Long press!')

    def rotate(self, rotation):
        if rotation == 1:
            incfreq()
        elif rotation == -1:
            decfreq()

        print('Rotate {}!'.format(rotation))

    def push_rotate(self, rotation):
        if rotation == 1:
            incGain()
            print("push rotate")

        elif rotation == -1:
            decGain()
            print("push rotate")

        print('Push rotate {}!'.format(rotation))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--check':
        sys.exit(check())
    # A device argument picks the knob when more than one is plugged in (they
    # share the /dev/input/by-id name). Its eventN name can change when it is
    # replugged, so with an argument there is no reconnecting.
    if len(sys.argv) > 1:
        try:
            ExamplePowerMate(sys.argv[1]).run()
        except PermissionError:
            sys.exit(permissionProblem(sys.argv[1]))
        sys.exit(0)
    # Otherwise keep going when the knob is unplugged: wait for it to come
    # back and pick it up again.
    while True:
        device = waitForKnob()
        print("Using %s" % device)
        try:
            ExamplePowerMate(device).run()
        except PermissionError:
            sys.exit(permissionProblem(device))
        except OSError as e:
            print("Lost the knob (%s)" % e)
        time.sleep(1)
