#!/usr/bin/env python
from powermate import PowerMateBase, LedEvent, MAX_BRIGHTNESS
import glob
import telnetlib
import subprocess
import os
try:
    import xmlrpclib
except ImportError:
    import xmlrpc
    import xmlrpc.client
import time
import sys

debugMode = 1
currentMode = "OFF"
targetip = "127.0.0.1"

'''
Edit the presets to values in MHz.
Example:
presets = [750, 877, 915, 1950, 2134, 2424, 2530]
'''
presets = [140, 433, 915, 1950, 2134, 2424]

pos = 0
currentFreq = 855000000
step = 1000000
currentGain = 32

def setFreq(freq1):
    freq = float(freq1)
    try:
        xml = xmlrpclib.Server('http://' + targetip + ':8080');
    except NameError:
        xml = xmlrpc.client.Server('http://' + targetip + ':8080');
    xml.set_freq(freq)
    try:
        xml2 = xmlrpclib.Server('http://' + targetip + ':8080');
    except NameError:
        xml2 = xmlrpc.client.Server('http://' + targetip + ':8080');
    xml2.set_freq(freq)

def setGain(gain1):
    gain = float(gain1)
    try:
        xml = xmlrpclib.Server('http://' + targetip + ':8080');
    except NameError:
        xml = xmlrpc.client.Server('http://' + targetip + ':8080');
    xml.set_gain(gain)

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
    global presets
    global pos
    global currentFreq

    pos += 1
    
    if pos >= len(presets):
        pos = 0

    preset_freq = presets[pos]
    currentFreq = preset_freq*1000000
    setFreq(preset_freq*1000000)

class ExamplePowerMate(PowerMateBase):
  def __init__(self, path):
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
    self._brightness = max(0, min(MAX_BRIGHTNESS, self._brightness + rotation))
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
