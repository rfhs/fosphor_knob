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
