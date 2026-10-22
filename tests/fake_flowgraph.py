#!/usr/bin/env python3
"""A stand-in for the fosphor GNU Radio flowgraph's XML-RPC control port.

run.py only talks to the flowgraph through get_freq/set_freq/get_gain/set_gain
on 127.0.0.1:8080, so this is enough to drive run.py with either knob and no
SDR attached:

    tests/fake_flowgraph.py &
    ./run.py                     # or ./run.py /dev/input/eventN

Every set_freq/set_gain call is printed. The run.py tests use it too.
"""

import argparse
import threading
from xmlrpc.server import SimpleXMLRPCServer

START_FREQ = 915000000
START_GAIN = 30


class FakeFlowgraph:
    def __init__(self, freq=START_FREQ, gain=START_GAIN, verbose=False):
        self.freq = freq
        self.gain = gain
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
    args = ap.parse_args()
    server = serve(FakeFlowgraph(args.freq, args.gain, verbose=True), port=args.port)
    print("fake flowgraph on 127.0.0.1:%d, Ctrl-C to stop" % args.port, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
