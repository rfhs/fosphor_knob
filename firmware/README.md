# PowerMate emulator for the Waveshare ESP32-S3 knob

Firmware for the [Waveshare ESP32-S3-Knob-Touch-LCD-1.8](https://www.waveshare.com/wiki/ESP32-S3-Knob-Touch-LCD-1.8)
that makes it a drop-in replacement for the discontinued Griffin PowerMate.
The computer sees a Griffin PowerMate (USB `077d:0410`, descriptors copied
byte for byte from a real one), so the Linux `powermate` driver binds to it
and `run.py` works unchanged.

| PowerMate | Waveshare knob |
|---|---|
| turn the knob | turn the ring (one count per click, ~31 per turn) |
| push the knob | touch the middle of the screen |
| push and turn | hold the screen and turn the ring |
| blue LED in the base (brightness, pulse) | cyan glow ring around the screen (see below) |
| | haptic click on press, release and every detent |
| | frequency/gain readout, when the host sends it |
| | the RF Hackers Sanctuary logo when there's no readout, and for 5 seconds at power-up |

Touches that start near the rim are ignored, so a hand turning the ring
doesn't press the button.

The glow ring runs itself ("attract mode"): steady and bright while the knob
is in use, breathing after 1 second untouched to invite people to try it,
and breathing whenever there is no USB connection. The breathing is done by
the backlight: the ring stays at full and the whole screen breathes, so
nothing has to be redrawn. Any touch or turn brings it straight back.
While it breathes, the middle of the screen takes turns of 15 seconds
between the readout (or the logo) and "THIS IS INTERACTIVE! Twist the knob
to control the spectrum analyzer display!", starting with the readout.
`tools/pmknob.py attract 0` hands the LED to the host like a real PowerMate,
pulses drawn on the ring.

## Plug orientation matters

The board has two chips. A switch on the USB-C port connects the computer to
one of them **depending on which way round the plug is inserted**. One way the
computer sees the PowerMate; flipped, it sees a CH340 serial adapter wired to
the second chip, and no PowerMate appears. Mark the working orientation on the
cable and the knob before taking it anywhere.

Whenever the knob has had no working USB connection for 3 seconds (cable
unplugged, plug upside down, or on a phone charger) its screen says "NO USB
CONNECTION / Plug in the USB-C cable, or flip the plug over". This is checked
continuously, not just at power-up, so pulling or flipping the plug while the
knob stays powered is caught too.
`tools/usb_watch.py` shows what the computer sees while you flip the plug.

## Building

Once, install ESP-IDF v5.5.6 (newer 5.5.x should work):

```sh
git clone -b v5.5.6 --depth 1 --recursive --shallow-submodules https://github.com/espressif/esp-idf ~/esp/esp-idf-v5.5.6
~/esp/esp-idf-v5.5.6/install.sh esp32s3
```

Then, in every new shell:

```sh
. ~/esp/esp-idf-v5.5.6/export.sh
cd firmware
idf.py build
```

The first build fetches the components (TinyUSB, the ST77916 panel driver,
LVGL) through the IDF component manager. The result is in `build/`:
`powermate_knob.bin` plus the bootloader and partition table, which
`tools/pmknob.py flash` and `idf.py flash` write together.

The board's second chip has its own small firmware, built and flashed
separately: see [`secondary/`](secondary/README.md).

CI (`.github/workflows/firmware.yml`) builds both on every push in the
`espressif/idf:v5.5.6` container and uploads the binaries; pushing a `v*`
tag publishes them as a release. `powermate_knob-merged.bin` is the single
image to flash at offset `0x0`.

## Build options

The firmware's options are under *PowerMate emulator* in `idf.py
menuconfig`. The ones marked "run time" can also be changed on a running
knob with `tools/pmknob.py`; the knob keeps those in flash, and a value set
that way wins over the build option, also across firmware updates (see
[Run-time settings](#run-time-settings)).

| Option | Default | What it does |
|---|---|---|
| `PM_COUNTS_PER_DETENT` | 1 | PowerMate rotation counts per click of the ring (a real PowerMate gives about 94 per turn) |
| `PM_MAX_DELTA_PER_REPORT` | 1 | largest rotation in one report. 1 sends every count on its own, which is what `run.py` expects; a real PowerMate lumps up to 7 together when turned fast |
| `PM_REVERSE_ROTATION` | off | swap the turning direction |
| `PM_ENCODER_MIN_PULSE_MS` | 2 | pulses on the ring's switch lines shorter than this are contact bounce |
| `PM_TOUCH_ACTIVE_RADIUS` | 150 | touches that start further than this many pixels from the centre (of 360×360) are ignored, so a hand on the rim doesn't press the button |
| `PM_TOUCH_FALLBACK_POLL_MS` | 0 | also read the touch controller this often between touches, in case its interrupt goes missing; 0 = interrupt only (it hasn't been seen to miss a touch) |
| `PM_HAPTIC_PRESS_EFFECT` | 1 | haptic effect on a press: a DRV2605L ROM effect (1 strong click, 4 sharp click, 7 soft bump, 21 medium click, 24 sharp tick, …), 0 = none. Run time: `pmknob.py haptic --set press N`; try one with `pmknob.py haptic N` |
| `PM_HAPTIC_RELEASE_EFFECT` | 2 | the same on release. Run time: `haptic --set release N` |
| `PM_HAPTIC_DETENT_EFFECT` | 2 | the same on each click of the ring (2 is the strong click at 60%). Run time: `haptic --set detent N` |
| `PM_ATTRACT_IDLE_MS` | 1000 | breathe after this many ms unused; 0 hands the LED to the host like a real PowerMate. Run time: `pmknob.py attract MS` |
| `PM_ATTRACT_INVITE_MS` | 15000 | while breathing, take turns of this many ms between the readout (or logo) and the "THIS IS INTERACTIVE!" invitation; 0 = never. Run time: `pmknob.py attract MS --invite MS` |
| `PM_BREATHE_MIN_PERCENT` | 10 | dimmest point of the breathing, in percent of full backlight |
| `PM_BREATHE_PERIOD_MS` | 4000 | length of one breath |
| `PM_LED_MIN_PERCENT` | 0 | dimmest the glow ring goes when the host turns the LED down; 0 = fully off, like a PowerMate |
| `PM_BACKLIGHT_PERCENT` | 100 | screen brightness |
| `PM_DISPLAY_ROTATION` | 2 | screen rotation in quarter turns clockwise (2 = USB port up). Run time: `pmknob.py rotate N` |
| `PM_PULSE_PERIOD_MS` | 2000 | length of one LED pulse at the PowerMate's normal speed, for pulses the host asks for (attract mode off) |
| `PM_BOOT_HOLD_MS` | 3000 | a finger on the screen at power-up held this long reboots into the ROM bootloader |
| `PM_FLASH_NEEDS_TOUCH` | on | a request to reboot into the bootloader only shows a prompt, confirmed by holding the screen; off lets anything that can reach the knob over USB reflash it |
| `PM_TEST_REQUESTS` | on | test requests: simulated turns and presses, playing, changing and recalibrating haptic effects (`pmknob.py haptic`), screenshots, forcing the no-USB hint. The automated Waveshare tests need them |
| `PM_DEBUG_I2C` | off | raw I2C register access over USB (`pmknob.py i2c`; needs `PM_TEST_REQUESTS` too), development only: a wrong write can take a chip off the bus until the next power cycle |

`sdkconfig.defaults` holds the settings that describe the board (16 MB QIO
flash, octal PSRAM, the partition table with a coredump partition, the
console on USB-Serial-JTAG, 1 kHz FreeRTOS tick, dynamic CPU frequency,
LVGL). Leave those alone.

### Changing an option

* **For this checkout**: `idf.py menuconfig`, then `idf.py build`. The choice
  is saved in `sdkconfig`, which isn't in git.
* **For everyone, CI included**: add the line (e.g.
  `CONFIG_PM_ATTRACT_IDLE_MS=2000`) to `sdkconfig.defaults`, then
  `rm sdkconfig` and build. An existing `sdkconfig` keeps the values it
  has: new defaults only reach options it doesn't know yet, so without the
  `rm` the change is silently ignored.
* **A second build next to the normal one**, e.g. a development build
  without the flash prompt:

  ```sh
  printf 'CONFIG_PM_FLASH_NEEDS_TOUCH=n\n' > sdkconfig.local
  idf.py -B build-dev -D SDKCONFIG=build-dev/sdkconfig \
         -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.local" build
  tools/pmknob.py flash build-dev
  ```

  `build-*/` and `sdkconfig.local` are ignored by git.

## Run-time settings

These live in the knob's flash (the `nvs` partition), survive firmware
updates and override the build options:

| Setting | Change it with | Build option |
|---|---|---|
| attract mode idle time, invitation turns | `tools/pmknob.py attract MS [--invite MS]` | `PM_ATTRACT_IDLE_MS`, `PM_ATTRACT_INVITE_MS` |
| haptic effects | `tools/pmknob.py haptic --set press\|release\|detent N` (with `PM_TEST_REQUESTS`) | `PM_HAPTIC_*_EFFECT` |
| screen rotation | `tools/pmknob.py rotate N` | `PM_DISPLAY_ROTATION` |
| haptic calibration | measured on the first boot; `tools/pmknob.py haptic --recalibrate` (with `PM_TEST_REQUESTS`) | — |

`tools/pmknob.py info` shows the values in use. To go back to the build
options, erase the settings (the haptic calibration is redone on the next
boot):

```sh
tools/pmknob.py reboot --bootloader      # confirm by holding the screen
esptool.py --chip esp32s3 -p /dev/ttyACM0 --before no_reset --after watchdog_reset erase_region 0x9000 0x6000
```

## Flashing

Plug the knob in the working way round (`tools/pmknob.py info` should
answer), and `pip install pyusb` once (`pyserial` too, for
`rescue`). `pmknob.py` finds esptool in ESP-IDF's own Python environment, so
it works from any terminal; plain `esptool.py` commands below need the IDF
shell (`. ~/esp/esp-idf-v5.5.6/export.sh`).
Access to the knob's USB node and to `/dev/ttyACM*` comes from the udev rule
`../udev/99-fosphor-knob.rules`; how to install it is at the top of the file.

### Updating a knob that runs this firmware

```sh
cd firmware
idf.py build
tools/pmknob.py flash          # or: tools/pmknob.py flash build-dev
```

1. The knob shows "UPDATE FIRMWARE?" and the whole screen blinks.
2. Hold a finger on the screen. It clicks as soon as it notices the finger,
   and gives a double click after 1.5 seconds.
3. It reboots into the ROM bootloader, gets flashed and restarts with the
   new firmware. The run-time settings stay.

Without that touch nothing happens (the prompt goes away after 15 seconds),
so software that can reach the knob over USB can't silently reflash it.
`PM_FLASH_NEEDS_TOUCH` turns the prompt off for development builds.

### First time (factory firmware still on it)

The factory firmware leaves the ESP32-S3's own USB serial port up, so:

```sh
idf.py -p /dev/ttyACM0 flash
```

Before that, consider saving the factory firmware: see
[Restoring the factory firmware](#restoring-the-factory-firmware).

### A release image from CI

Download `powermate_knob-merged.bin` from the release (or the workflow run's
artifacts), then:

```sh
tools/pmknob.py reboot --bootloader      # confirm by holding the screen
esptool.py --chip esp32s3 -p /dev/ttyACM0 --before no_reset --after watchdog_reset write_flash 0x0 powermate_knob-merged.bin
```

### When the firmware is broken

* **It still runs**: hold a finger on the screen while plugging the knob in,
  for 3 seconds (`PM_BOOT_HOLD_MS`), until it reboots into the bootloader;
  then `tools/pmknob.py flash`.
* **It hangs or crashes on startup**: `tools/pmknob.py rescue`, then unplug
  and replug the knob. Every power-up shows the bootloader's serial port for
  a moment; rescue catches it, keeps the chip in the bootloader and flashes.

Crashes are saved to the `coredump` partition: reboot into the bootloader and
run `idf.py coredump-info`.

## Testing

With the knob plugged in, the automated self-test checks it from the
computer, no hands needed (about 20 s):

```sh
tools/selftest.py
```

It covers the USB descriptors, the LED requests, the readout, raw reports,
the no-USB hint, attract mode and the invitation, request hardening and the
kernel `powermate` driver's events. It needs a build with `PM_TEST_REQUESTS`
(the default) and puts back the settings it changes.

## Tools

| | |
|---|---|
| `tools/pmknob.py info` | firmware status, counters, timings, settings in use |
| `tools/pmknob.py led 128` / `led --pulse` | drive the LED like the Linux driver does |
| `tools/pmknob.py inject --rotate 3 --press` | simulate turns and presses |
| `tools/pmknob.py haptic N` / `haptic --set detent 2` / `haptic --recalibrate` | play an effect / change the press, release or detent click (kept in flash) / measure the actuator again |
| `tools/pmknob.py display 2424e6 32` / `display --clear` / `display` | show a frequency and gain / blank the readout / read it back |
| `tools/pmknob.py rotate N` | screen rotation in quarter turns (kept in flash) |
| `tools/pmknob.py screenshot out.png` | grab what the screen shows |
| `tools/pmknob.py attract 1000 [--invite 15000]` | attract-mode idle time in ms, 0 = host owns the LED; turns of the invitation, 0 = never (kept in flash) |
| `tools/pmknob.py pulses` | raw ring pulses, for tuning the debouncer |
| `tools/pmknob.py touches --watch` | what the touch controller reports and what became of each contact, for chasing missed touches |
| `tools/pmknob.py plug-hint on` | show the flip-the-plug hint without unplugging |
| `tools/pmknob.py i2c …` | raw register access; needs a build with `PM_DEBUG_I2C` |
| `tools/pmknob.py reboot [--bootloader]` | restart, or ask for the ROM bootloader (confirm on the screen) |
| `tools/pmknob.py flash [BUILD_DIR]` / `rescue [BUILD_DIR]` | flash a build, see above |
| `tools/usb_watch.py` | log USB devices coming and going, and what each one is |
| `tools/selftest.py` | the knob's automated hardware self-test |

The `pmknob.py` requests go through usbfs and work alongside the kernel
driver.

## How it works

* **USB** (`main/usb_powermate.c`): TinyUSB with the PowerMate's own
  descriptors. Input reports are the PowerMate's 6 bytes: button, signed
  rotation. Every count goes out in its own report (`run.py` ignores
  rotations other than ±1). The LED is set with the PowerMate's vendor request
  0x01; requests 0xF0–0xFD are extensions (reboot, status, test injection,
  haptics, the readout, rotation, screenshots, attract mode, the touch log),
  documented in `main/usb_powermate.h`. Requests are bounds-checked and the risky ones are
  limited: flash mode needs a touch on the knob, raw I2C access is only in
  `PM_DEBUG_I2C` builds, and `PM_TEST_REQUESTS` (injection, screenshots) can
  be turned off. A real PowerMate stalls its LED requests while still
  obeying them; this one acknowledges them.
* **Ring** (`main/encoder.c`): not a quadrature encoder. Each click pulls
  GPIO8 (clockwise) or GPIO7 (counter-clockwise) low for 5–30 ms; both lines
  are sampled every millisecond and pulses under 2 ms are bounce.
* **Button** (`main/touch.c`): CST816D touch controller, read every 10 ms
  while a finger is down. Between touches its interrupt line (GPIO9) wakes
  the reading; `PM_TOUCH_FALLBACK_POLL_MS` adds a slow poll as a fallback
  (off: the interrupt hasn't been seen to miss a touch).
* **Haptics** (`main/haptic.c`): DRV2605L driving an LRA (about 165 Hz) in
  closed loop, auto-calibrated on first boot. Its enable pin is GPIO38, which
  Waveshare's schematic labels as a UART line; with it left floating the clicks
  are barely there.
* **Screen** (`main/display.c`, `main/ui.c`): ST77916 over QSPI with LVGL.
  The glow ring and the gain arc are filled by hand into one image, in
  separate bands, so an LED pulse never re-renders the numbers (about
  25 frames/s while pulsing). Colors, the font (Anybody, the open font
  closest to the RFHS lettering) and the logo follow the RF Hackers
  Sanctuary artwork; `main/fonts/` and `main/images/` have the scripts that
  generate them.

Board quirks that cost time to find, and the pin map, are in `main/board.h`.
GPIO38 and the knob's switch lines were worked out by the
[teetotum-rs](https://github.com/teetotum-rs/firmware) project.

## Power

Made to run off the knob's battery later on, without changing how it behaves:

* The CPU idles at 80 MHz and only runs at 240 MHz while drawing the screen
  (the peripheral clocks stay put, so nothing else notices).
* The ring and the touch screen wake their handling with interrupts instead
  of being polled: nothing runs while nobody touches the knob.
* The breathing is done by the backlight hardware (a chain of hardware
  fades, one wake-up every 330 ms), so the screen isn't redrawn while the
  knob is idle.
* The haptic driver sits in standby between clicks.
* The USB task sleeps until there is something to send, waking every
  50 ms to notice a lost host or a reboot request.
* The second chip, whose factory Bluetooth firmware the emulator doesn't
  need, can be put into deep sleep: see [`secondary/`](secondary/README.md).

What still costs power while idle is the backlight itself, and the touch
controller, which is kept awake so it never misses a touch.

## Restoring the factory firmware

Before flashing a new knob for the first time, save its factory firmware
(16 MB, a few minutes):

```sh
mkdir -p ~/esp/waveshare-knob-factory-backup
esptool.py --chip esp32s3 -p /dev/ttyACM0 -b 460800 read_flash 0 0x1000000 ~/esp/waveshare-knob-factory-backup/factory_flash_16MB.bin
```

The first knob's dump is
`~/esp/waveshare-knob-factory-backup/factory_flash_16MB.bin` (sha256
`931bf2af…31d`). To put it back:

```sh
tools/pmknob.py reboot --bootloader      # confirm by holding the screen
esptool.py --chip esp32s3 -p /dev/ttyACM0 --before no_reset --after watchdog_reset write_flash 0x0 factory_flash_16MB.bin
```

## Licenses

Fonts in `main/fonts/` are generated from Anybody and Noto Sans, both SIL
Open Font License 1.1 (`main/fonts/OFL-Anybody.txt`,
`main/fonts/OFL-NotoSans.txt`). The RF Hackers Sanctuary logo in
`main/images/` belongs to Radio Frequency Hackers Sanctuary.
