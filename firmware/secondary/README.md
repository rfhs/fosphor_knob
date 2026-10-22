# Firmware for the knob's second chip

The Waveshare knob has a second chip, a classic ESP32 (ESP32-U4WDH). Its
factory firmware runs Bluetooth audio and HID: it advertises as
`TAIJI_KNOB_HID` / `TAIJI_KNOB_AUDIO`, so anyone nearby can try to pair with
it, and it draws tens of milliamps. The PowerMate emulator doesn't use it.

This firmware mutes the audio DAC and puts the chip into deep sleep with its
radios never started, until the next reset.

## Flashing

The chip is reached through the knob's CH340 USB-serial adapter, which the
computer only sees with the USB-C plug in the other way round. From an
ESP-IDF shell:

```sh
cd firmware/secondary
idf.py build
./flash.sh          # flip the plug when asked, flip it back when it says so
```

`flash.sh` saves the chip's factory firmware first (once per chip, named
after its MAC address, in `~/esp/waveshare-knob-factory-backup/`), flashes,
and checks with `bluetoothctl` that nothing advertises as `TAIJI_KNOB` any
more. To go back to the factory firmware:

```sh
esptool.py --chip esp32 -p /dev/ttyUSB0 write_flash 0x0 ~/esp/waveshare-knob-factory-backup/second_chip_<mac>_factory_4MB.bin
```

CI builds it too (`firmware.yml`, `secondary/` in the artifacts).
