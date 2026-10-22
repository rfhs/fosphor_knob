# fosphor_knob

#powermate.py from https://github.com/bethebunny/powermate under apache 2 license

#initial code from dev_nulling, I think, it's  been a while

## Flowgraphs

`fosphor_knob.sh` starts the flowgraph for the SDR it finds: a USRP
(`fosphor_knob_uhd`), a bladeRF 2.0 (`fosphor_knob_bladerf2`) or a HackRF
(`fosphor_knob_hackrf_sweep`, which sweeps the whole range). Started as
`fosphor_knob_sponsors`, it runs the variants that also show the sponsors.
With a bladeRF 2.0 it starts the wide flowgraph
(`fosphor_knob_bladerf2_wide*.grc`, 122.88 MHz) when libbladeRF (2.6.0 or
later) and gr-osmosdr support oversampling and bit packing, and the normal
one (61.44 MHz) otherwise; it says which one it started. The flowgraphs take
their tuning over XML-RPC on 127.0.0.1:8080, from this machine only.

`tests/test_flowgraphs.py` compiles every flowgraph with grcc and imports the
generated scripts, which needs GNU Radio with gr-osmosdr, gr-fosphor and
gr-paint. CI runs it (`.github/workflows/flowgraphs.yml`):

```sh
python3 -m unittest discover -s tests -p test_flowgraphs.py -v
```

## Knobs

`run.py` works with a Griffin PowerMate or with a Waveshare ESP32-S3 knob
running the PowerMate emulator in [`firmware/`](firmware/README.md). With the
Waveshare knob it also shows the current frequency and gain on the knob's
screen (`knob_display.py`, installed next to `run.py`), the RF Hackers
Sanctuary logo when there is nothing to show, and, once it's been left
alone, takes turns with an invitation to try it. Either knob's LED
breathes after 1 second without use and holds steady while it is in use: the
Waveshare knob's firmware does that itself, for a Griffin `run.py` does it
(`idleSeconds` in `run.py`, or `FOSPHOR_KNOB_IDLE_SECONDS`).

| | Griffin PowerMate | Waveshare knob |
|---|---|---|
| tune by `step` | turn the knob | turn the ring |
| next preset | short press | short tap on the screen |
| gain up/down | push and turn | hold the screen and turn the ring |

`fosphor_knob.sh` checks for a usable knob (`run.py --check`) before starting
a mode that is tuned with it, and stops with the reason if there is none.
The hackrf_sweep modes sweep the whole range and can't be tuned, so they run
without the knob.

`run.py` waits for the knob if it isn't plugged in yet, and picks it up again
if it is unplugged and plugged back in. If more than one knob is plugged in,
pass the one to use: `run.py /dev/input/eventN` (no reconnecting then, as the
name can change on a replug). `tests/knobs.py` lists the knobs plugged in and
which event node is which.

## Setup

`run.py` needs only Python 3. The test scripts and
`firmware/tools/pmknob.py` also need pyusb (`pip install pyusb`, and
`pyserial` for `pmknob.py rescue`).

### Permissions

`run.py` reads and writes the knob's `/dev/input/event*` node, and the
Waveshare knob's readout goes through its USB device node. To use the knobs
without root, install the udev rule:

```sh
sudo mkdir -p /etc/udev/rules.d
sudo cp udev/99-fosphor-knob.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

then unplug and replug the knobs. It gives the `plugdev` group, and whoever
is logged in at the machine, access to both knobs; flashing the Waveshare
knob also needs the `dialout` group. `run.py --check` says what is missing.

## Settings

At the top of `run.py`:

| Setting | Default | |
|---|---|---|
| `presets` | 915, 1950, 2134, 2424, 5160, 140, 433 | MHz, in the order a short press steps through them |
| `step` | 1000000 | Hz per click |
| `minGain`, `maxGain` | 0, 76 | gain range for push and turn, in dB |
| `idleSeconds` | 1 | a Griffin's LED breathes after this many seconds unused; `FOSPHOR_KNOB_IDLE_SECONDS` overrides it |
| `targetip` | 127.0.0.1 | where the flowgraph's XML-RPC server listens (port 8080) |

The Waveshare knob's own settings (breathing, haptics, screen rotation, …)
are in the [firmware README](firmware/README.md#build-options).

## Testing

Stop `fosphor_knob.sh` (and any `run.py`) first: the `run.py` tests start
their own, against a fake flowgraph on port 8080. Both knobs can stay
plugged in.

### Everything automated, for both knobs

```sh
tests/run_all.py                    # unit tests + every knob that is plugged in
tests/run_all.py --knob waveshare   # unit tests + the Waveshare knob only
tests/run_all.py --knob griffin     # unit tests + the Griffin only
tests/run_all.py --no-hardware      # unit tests only, as CI runs them
```

It takes about 30 seconds with both knobs and ends with a summary; the exit
status is non-zero if anything failed. A knob that isn't plugged in is
skipped, unless it was asked for with `--knob`. What it runs:

| Script | Knob | Checks |
|---|---|---|
| `python3 -m unittest discover -s tests` | none | wire formats against the firmware, knob detection, `run.py`'s LED breathing, and the flowgraphs where GNU Radio is installed; CI runs these |
| `firmware/tools/selftest.py` | Waveshare | USB descriptors, LED requests, readout, raw HID reports, the no-USB hint, breathing, request hardening, kernel `powermate` events |
| `tests/runpy_test.py` | Waveshare | `run.py` end to end: turning tunes, a press steps through the presets, hold and turn sets the gain, the readout follows, the LED breathes when idle |
| `tests/reconnect_test.py` | Waveshare | `run.py` keeps going when the knob is unplugged and plugged back in |
| `tests/griffin_test.py` | Griffin | USB descriptors (the same as the emulator's), kernel driver and input capabilities, LED writes |

The Waveshare tests turn and press the knob with test requests, so no hands
are needed. A Griffin can't be turned that way: its turning, pressing and
LED are checked by hand, below.

### By hand, for both knobs

```sh
tests/hands_on.py --knob waveshare
tests/hands_on.py --knob griffin
```

It asks you to turn the knob both ways, press it, and hold it while turning,
watches the input events, then asks whether the LED (on the Waveshare knob:
the screen) breathes when idle and goes steady in use. On the Waveshare knob
it also asks about the readout, the logo, the invitation, the haptic clicks
and the no-USB message.
Each step waits up to 30 seconds and Enter skips it; questions take y, n or
s (skip). It ends with a summary and a non-zero exit status if a step failed.
With only one knob plugged in `--knob` can be left out.

### Other helpers

* `tests/knobs.py` — which knobs are plugged in, on which event node.
* `tests/fake_flowgraph.py` — stands in for the flowgraph's XML-RPC port, so
  `run.py` can be tried with either knob and no SDR.
* `firmware/tools/pmknob.py` — status and test requests for the Waveshare
  knob (`pmknob.py info`, `touches --watch`, …); see the firmware README.

CI (`.github/workflows/host.yml`) runs the unit tests, compiles every
script, runs shellcheck on `fosphor_knob.sh`, `nosleep.sh` and
`firmware/secondary/flash.sh` and checks the udev rules.
`.github/workflows/firmware.yml` builds the firmware, and
`.github/workflows/flowgraphs.yml` checks the flowgraphs (see
[Flowgraphs](#flowgraphs)).
