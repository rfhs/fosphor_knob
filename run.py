#!/usr/bin/env python
from powermate import PowerMateBase, LedEvent, MAX_BRIGHTNESS
import glob
import time
import xmlrpc
import xmlrpc.client

debugMode = 0
currentMode = "OFF"
targetip = "127.0.0.1"

'''
Edit the presets to values in MHz.
Example:
presets = [750, 877, 915, 1950, 2134, 2424, 2530]
'''
presets = [140, 433, 915, 1950, 2134, 2424, 5160]

pos = 0
currentFreq = -1
step = 1000000
currentGain = -1


def setFreq(freq1):
    freq = float(freq1)
    xml = xmlrpc.client.Server('http://' + targetip + ':8080')
    xml.set_freq(freq)


def getFreq():
    global currentFreq
    xml = xmlrpc.client.Server('http://' + targetip + ':8080')
    currentFreq = int(xml.get_freq())
    print("Found initial freq %s" % currentFreq)


def getGain():
    global currentGain
    xml = xmlrpc.client.Server('http://' + targetip + ':8080')
    currentGain = int(xml.get_gain())
    print("Found gain %s" % currentGain)


def setGain(gain1):
    gain = int(gain1)
    xml = xmlrpc.client.Server('http://' + targetip + ':8080')
    xml.set_gain(float(gain))


def incGain():
    global currentGain
    currentGain += 1
    if currentGain > 76:
        currentGain = 76
    print("setting Gain %s" % currentGain)
    setGain(currentGain)


def decGain():
    global currentGain
    currentGain -= 1
    if currentGain < 0:
        currentGain = 0
    print("setting Gain %s" % currentGain)
    setGain(currentGain)


def incfreq():
    global currentFreq
    currentFreq += step
    print("setting freq %s" % currentFreq)
    setFreq(currentFreq)


def decfreq():
    global currentFreq
    currentFreq -= step
    print("setting freq %s" % currentFreq)
    setFreq(currentFreq)


def preset():
    global pos
    global currentFreq

    pos += 1

    if pos >= len(presets):
        pos = 0

    preset_freq = presets[pos]
    currentFreq = preset_freq*1000000
    setFreq(preset_freq*1000000)


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


class ExamplePowerMate(PowerMateBase):
    def __init__(self, path):
        wait_for_getFreq()
        getGain()
        super(ExamplePowerMate, self).__init__(path)
        self._pulsing = False
        self._brightness = MAX_BRIGHTNESS

    def short_press(self):
        print('Short press!')
        preset()
        self._pulsing = not self._pulsing
        print(self._pulsing)
        if self._pulsing:
            return LedEvent.pulse()
        else:
            return LedEvent(brightness=self._brightness)

    def long_press(self):
        print('Long press!')

    def rotate(self, rotation):
        if rotation == 1:
            incfreq()
        elif rotation == -1:
            decfreq()

        print('Rotate {}!'.format(rotation))
        self._brightness = \
            max(0, min(MAX_BRIGHTNESS, self._brightness + rotation))
        self._pulsing = False
        return LedEvent(brightness=self._brightness)

    def push_rotate(self, rotation):
        if rotation == 1:
            incGain()
            print("push rotate")

        elif rotation == -1:
            decGain()
            print("push rotate")

        print('Push rotate {}!'.format(rotation))


if __name__ == '__main__':
    pm = ExamplePowerMate(glob.glob('/dev/input/by-id/*PowerMate*')[0])
    pm.run()
