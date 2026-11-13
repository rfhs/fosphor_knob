#!/usr/bin/env python3
"""A stand-in for the fosphor GNU Radio flowgraph's XML-RPC control port.

run.py only talks to the flowgraph through get_freq/set_freq/get_gain/set_gain
and get_knob_limits on 127.0.0.1:8080, so this is enough to drive run.py with
either knob and no SDR attached:

    tests/fake_flowgraph.py &                 # no limits, like an older flowgraph
    tests/fake_flowgraph.py --like rtlsdr &   # the limits an RTL-SDR reports
    tests/fake_flowgraph.py --like fake &     # made-up limits that are quick to reach
    ./run.py                     # or ./run.py /dev/input/eventN

--like answers get_knob_limits with sdr_limits.describe() of an SDR that
reports what the real one did (tests/sdr_ranges.py), or, for "fake", a
made-up SDR whose edges are a few clicks from a preset and whose gains mix
1 dB steps with uneven jumps. Every set_freq/set_gain call is printed. The
run.py tests use it too.
"""

import argparse
import os
import sys
import threading
from xmlrpc.server import SimpleXMLRPCServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sdr_limits  # noqa: E402

START_FREQ = 915000000
START_GAIN = 30

R820T_GAINS = [0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28, 29.7,
               32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48, 49.6]
FAKE_GAINS = [-3, -2, -1, 0, 1, 2, 3, 4.6, 7.9, 12.3, 13.3, 14.3, 15.3, 16.3, 21.8, 22.8, 23.8,
              30, 31, 32, 33, 37.5, 44.1, 45.1, 46.1, 47.1, 55]
# What each SDR reported to tests/sdr_ranges.py: (freq range Hz, the
# flowgraph's sample rate, gain range (start, stop, step) or discrete values,
# a fixed ladder the flowgraph publishes instead).
SDRS = {
    "uhd": ((42e6, 6008e6), 56e6, (0, 76, 1), None),
    "bladerf2": ((70e6, 6000e6), 61.44e6, (-15, 60, 1), None),
    "rtlsdr": ((24e6, 1766e6), 2.4e6, R820T_GAINS, None),
    "hackrf": ((4e6, 7246e6), 20e6, (0, 14, 14), sdr_limits.HACKRF_GAINS),
    # Centre 130-2440 MHz: 16 clicks above the 2424 MHz preset, 10 below
    # 140 MHz, and 5160 MHz is skipped.
    "fake": ((120e6, 2450e6), 20e6, FAKE_GAINS, None),
}


class _Range:
    """Enough of an osmosdr meta_range_t for sdr_limits."""

    def __init__(self, pieces, step=0.0):
        self.pieces, self._step = pieces, step

    def start(self):
        return self.pieces[0][0]

    def stop(self):
        return self.pieces[-1][1]

    def step(self):
        return self._step

    def to_pp_string(self):
        return "".join("(%g, %g)\n" % p if p[0] != p[1] else "(%g)\n" % p[0] for p in self.pieces)


class FakeSource:
    """An osmosdr source block that answers like one of SDRS."""

    def __init__(self, like):
        self.freq, self.samp_rate, self.gain, _ = SDRS[like]

    def get_freq_range(self, chan=0):
        return _Range([self.freq])

    def get_samp_rate(self):
        return self.samp_rate

    def get_gain_range(self, chan=0):
        if isinstance(self.gain, list):
            return _Range([(g, g) for g in self.gain], 0.1)
        return _Range([self.gain[:2]], self.gain[2])


def knob_limits(like):
    return sdr_limits.describe(FakeSource(like), SDRS[like][1], gain_values=SDRS[like][3])


class FakeFlowgraph:
    def __init__(self, freq=START_FREQ, gain=START_GAIN, verbose=False, like=None, opening=0):
        """like: answer get_knob_limits like that SDR (None: no such method,
        as with an older flowgraph); opening: answer {} that many times
        first, as while the SDR is still being opened."""
        self.freq = freq
        self.gain = gain
        self.like = like
        self.opening = opening
        self.verbose = verbose
        self.calls = []
        self.lock = threading.Lock()

    def _record(self, kind, value):
        with self.lock:
            self.calls.append((kind, int(value)))
        if self.verbose:
            print("set_%s(%s)" % (kind, value), flush=True)

    def get_freq(self):
        return self.freq

    def set_freq(self, f):
        self.freq = f
        self._record("freq", f)
        return True

    def get_knob_limits(self):
        if self.like is None:
            raise AttributeError("get_knob_limits is not supported")
        if self.opening > 0:
            self.opening -= 1
            return {}
        return knob_limits(self.like)

    def get_gain(self):
        return self.gain

    def set_gain(self, g):
        self.gain = g
        self._record("gain", g)
        return True

    def take(self):
        """Return and forget the calls recorded so far."""
        with self.lock:
            calls, self.calls = self.calls, []
        return calls


def serve(flowgraph, host="127.0.0.1", port=8080):
    """Serve the flowgraph in a background thread; returns the server."""
    server = SimpleXMLRPCServer((host, port), logRequests=False, allow_none=True)
    server.register_instance(flowgraph)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freq", type=float, default=START_FREQ, help="initial frequency in Hz")
    ap.add_argument("--gain", type=float, default=START_GAIN, help="initial gain in dB")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--like", choices=sorted(SDRS) + ["none"], default="none",
                    help="publish the limits this SDR reported, or made-up ones (fake); default: none")
    args = ap.parse_args()
    like = None if args.like == "none" else args.like
    server = serve(FakeFlowgraph(args.freq, args.gain, verbose=True, like=like), port=args.port)
    print("fake flowgraph on 127.0.0.1:%d, Ctrl-C to stop" % args.port, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
