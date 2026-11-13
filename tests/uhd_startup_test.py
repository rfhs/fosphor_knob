#!/usr/bin/env python3
"""run.py can talk to the UHD flowgraph while it is still opening the USRP.

The generated fosphor_knob_uhd.py starts its XML-RPC server and only then
opens the USRP, which can take over a minute while UHD loads the firmware
and FPGA image after the USRP is plugged in. This does the same, in that
order, and calls get_freq/set_freq from another process meanwhile, the way
run.py does. It passes when every call is answered (calls made during the
open wait for it, since gr-uhd holds the GIL while opening) and none fails:
a set_freq that reached the flowgraph before the USRP block existed would.

    tests/uhd_startup_test.py [--port 18080]

Needs a USRP plugged in, not in use, and GNU Radio with gr-uhd. Replug the
USRP first to see a cold start with the firmware load.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from xmlrpc.server import SimpleXMLRPCServer

CLIENT = r'''
import json, sys, time, xmlrpc.client
port, stop_file = sys.argv[1], sys.argv[2]
i = 0
while True:
    started = time.time()
    try:
        proxy = xmlrpc.client.ServerProxy("http://127.0.0.1:%s" % port)
        if i % 2:
            proxy.set_freq(916e6 + i)
        else:
            proxy.get_freq()
        result = "ok"
    except ConnectionRefusedError:
        result = "refused"
    except Exception as e:
        result = "FAILED: %s" % e
    print(json.dumps([started, time.time(), "set_freq" if i % 2 else "get_freq", result]), flush=True)
    if result == "ok":
        i += 1
    try:
        open(stop_file).close()
        break
    except OSError:
        time.sleep(0.25)
'''


class Flowgraph:
    """The parts of the generated flowgraph run.py talks to, in its order."""

    def __init__(self, port):
        self.freq = 915e6
        self.server = SimpleXMLRPCServer(("127.0.0.1", port), allow_none=True, logRequests=False)
        self.server.register_instance(self)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.opening = time.time()
        from gnuradio import uhd
        self.uhd_usrp_source_0 = uhd.usrp_source(",".join(("", "")),
                                                 uhd.stream_args(cpu_format="fc32", args="", channels=[0]))
        self.opened = time.time()
        self.uhd_usrp_source_0.set_center_freq(self.freq, 0)

    def get_freq(self):
        return self.freq

    def set_freq(self, freq):
        self.freq = freq
        self.uhd_usrp_source_0.set_center_freq(self.freq, 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=18080, help="not 8080, so it can't reach a running flowgraph")
    args = ap.parse_args()

    tmp = tempfile.TemporaryDirectory()
    stop_file = os.path.join(tmp.name, "stop")
    client = subprocess.Popen([sys.executable, "-c", CLIENT, str(args.port), stop_file],
                              stdout=subprocess.PIPE, text=True)
    time.sleep(0.5)     # the client starts before the flowgraph's server, as run.py does
    try:
        fg = Flowgraph(args.port)
    except RuntimeError as e:
        client.kill()
        sys.exit("could not open the USRP (plugged in? in use by fosphor_knob.sh?): %s" % e)
    time.sleep(1.5)     # a few calls once it is open
    open(stop_file, "w").close()
    out, _ = client.communicate(timeout=60)
    calls = [json.loads(line) for line in out.splitlines()]

    failures = 0

    def check(cond, what):
        nonlocal failures
        print("  %s  %s" % ("ok  " if cond else "FAIL", what))
        failures += 0 if cond else 1

    print("UHD flowgraph startup: USRP opened in %.1f s" % (fg.opened - fg.opening))
    answered = [c for c in calls if c[3] == "ok"]
    failed = [c for c in calls if c[3].startswith("FAILED")]
    during = [c for c in answered if c[0] < fg.opened and c[1] >= fg.opening]
    longest = max((end - start for start, end, _, _ in during), default=0.0)
    check(not failed, "no call failed%s" % ("" if not failed else ": %s" % failed[0][3]))
    check(any(c[2] == "set_freq" for c in answered if c[1] >= fg.opened), "set_freq works once the USRP is open")
    if fg.opened - fg.opening > 0.5:
        check(bool(during), "calls made while it opened were answered (the longest waited %.1f s)" % longest)
    else:
        print("  (it opened too quickly for a call to overlap; replug the USRP for a cold start)")
    print("\n%s" % ("all checks passed" if failures == 0 else "%d check(s) FAILED" % failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
