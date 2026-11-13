#!/usr/bin/env python3
"""Ask each plugged-in SDR for its tuning and gain range, the way its flowgraph opens it.

    tests/sdr_ranges.py                       every supported SDR that is plugged in
    tests/sdr_ranges.py --sdr uhd|bladerf2|hackrf|rtlsdr
    tests/sdr_ranges.py --repeat 5            open each SDR 5 times and compare the answers
    tests/sdr_ranges.py --args "serial=..."   device args instead of the flowgraph's
    tests/sdr_ranges.py --samp-rate 2.4e6     also the knob's range for a capture this wide
    tests/sdr_ranges.py --gain-across-band    also tune it to each preset and near both ends,
                                              and read the gain range there

The SDR is opened with the same source block and device args as the
flowgraph fosphor_knob.sh starts for it (read from the .grc), and asked for
its frequency and gain range on channel 0. It never streams, and it is
only tuned with --gain-across-band. The knob's centre frequency has to stay half the flowgraph's
sample rate inside the ends of the range, so the whole capture fits. Stop
fosphor_knob.sh first: an SDR in use can't be opened twice.

Exit status: 0 when every SDR answered with the same, sane ranges each time,
1 when one didn't or couldn't be opened, 2 when none was found.
Needs GNU Radio with gr-uhd and gr-osmosdr, and PyYAML.
"""

import argparse
import ast
import gc
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import run  # noqa: E402
import sdr_limits  # noqa: E402
from sdr_limits import span, sub_ranges, tuning_range  # noqa: E402

SDRS = {
    "uhd": {"name": "USRP B2xx", "usb": [("2500", "0022")], "block": "uhd_usrp_source",
            "flowgraphs": ["fosphor_knob_uhd", "fosphor_knob_uhd_sponsors"]},
    "bladerf2": {"name": "bladeRF 2.0", "usb": [("2cf0", "5250")], "block": "osmosdr_source",
                 "flowgraphs": ["fosphor_knob_bladerf2", "fosphor_knob_bladerf2_sponsors",
                                "fosphor_knob_bladerf2_wide", "fosphor_knob_bladerf2_wide_sponsors"]},
    # The flowgraphs drive the LNA and VGA as one ladder; the hackrf_sweep
    # ones run hackrf_sweep, not a source block, and aren't tuned.
    "hackrf": {"name": "HackRF", "usb": [("1d50", "6089")], "block": "osmosdr_source",
               "flowgraphs": ["fosphor_knob_hackrf", "fosphor_knob_hackrf_sponsors"],
               "gain_values": sdr_limits.HACKRF_GAINS},
    "rtlsdr": {"name": "RTL-SDR", "usb": [("0bda", "2838"), ("0bda", "2832")], "block": "osmosdr_source",
               "flowgraphs": ["fosphor_knob_rtlsdr", "fosphor_knob_rtlsdr_sponsors"]},
}


def plugged_in(sdr):
    return any(run.usbDevicePresent(*usb) for usb in sdr["usb"])


def usb_ids(sdr):
    return " or ".join("%s:%s" % usb for usb in sdr["usb"])


def load(flowgraph):
    import yaml
    with open(os.path.join(REPO, flowgraph + ".grc")) as f:
        return yaml.safe_load(f)


def _string_param(value):
    """A GRC string parameter: a Python string expression, or the text itself."""
    try:
        evaluated = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value
    return evaluated if isinstance(evaluated, str) else value


def source_args(flowgraph):
    """(block id, device args) of the flowgraph's SDR source."""
    for block in load(flowgraph)["blocks"]:
        p = block["parameters"]
        if block["id"] == "osmosdr_source":
            return block["id"], _string_param(p["args"])
        if block["id"] == "uhd_usrp_source":
            # GRC joins them the same way
            return block["id"], ",".join(filter(None, (_string_param(p["dev_addr"]),
                                                       _string_param(p["dev_args"]))))
    raise ValueError("%s has no SDR source block" % flowgraph)


def variable(flowgraph, name):
    for block in load(flowgraph)["blocks"]:
        if block["id"] == "variable" and block["name"] == name:
            return float(block["parameters"]["value"])
    return None


def open_source(block, args):
    if block == "uhd_usrp_source":
        from gnuradio import uhd
        return uhd.usrp_source(uhd.device_addr_t(args), uhd.stream_args(cpu_format="fc32", channels=[0]))
    import osmosdr
    return osmosdr.source(args)


def gains(src):
    """The overall gain range and each stage's."""
    return span(src.get_gain_range(0)), {name: span(src.get_gain_range(name, 0)) for name in src.get_gain_names(0)}


def query(block, args, across_band=False):
    """Open the SDR once and read what it reports; across_band also tunes it
    to each preset it covers and to near both ends, and reads the gains there."""
    started = time.time()
    src = open_source(block, args)
    opened = time.time() - started
    try:
        freq = src.get_freq_range(0)
        gain = src.get_gain_range(0)
        answer = {
            "freq": span(freq),
            "freq_pieces": sub_ranges(freq),
            "gain": span(gain),
            "gain_pieces": sub_ranges(gain),
            "gain_now": src.get_gain(0),
            "stages": gains(src)[1],
        }
        if across_band and answer["freq"]:
            low, high = answer["freq"][:2]
            points = {low + 0.01 * (high - low), high - 0.01 * (high - low)}
            points |= {p * 1e6 for p in run.presets if low <= p * 1e6 <= high}
            answer["by_freq"] = []
            for f in sorted(points):
                src.set_center_freq(f, 0)
                answer["by_freq"].append((f,) + gains(src))
    finally:
        del src
        gc.collect()
    return answer, opened


def ladder_problems(answer):
    """The HackRF ladder's LNA and VGA gains must be ones the stages report."""
    stages = answer["stages"]
    found = []
    for stage, top in (("IF", sdr_limits.HACKRF_LNA_MAX), ("BB", sdr_limits.HACKRF_VGA_MAX)):
        r = stages.get(stage)
        if r is None or r[0] > 0 or r[1] < top:
            found.append("the gain ladder needs %s 0 to %d dB, the SDR reports %s" % (
                stage, top, "%g to %g" % r[:2] if r else "no such stage"))
    return found


def discrete(answer):
    points = answer["gain_pieces"] or []
    return len(points) > 1 and all(a == b for a, b in points)


def problems(sdr, answer):
    found = ladder_problems(answer) if sdr.get("gain_values") else []
    for what in ("freq", "gain"):
        r = answer[what]
        if r is None:
            found.append("%s range is empty" % what)
        elif r[0] >= r[1]:
            found.append("%s range is inverted or a single value (%g to %g)" % (what, r[0], r[1]))
    if answer["freq"] and answer["freq"][1] <= 0:
        found.append("frequency range ends at or below 0 Hz")
    if answer["freq"]:
        for flowgraph in sdr["flowgraphs"]:
            samp_rate = variable(flowgraph, "samp_rate")
            if tuning_range(answer["freq"], samp_rate) is None:
                found.append("%s's %s capture is wider than the whole range" % (flowgraph, mhz(samp_rate)))
    return found


def mhz(hz):
    return "%.3f MHz" % (hz / 1e6)


def describe(sdr, answer, samp_rates=()):
    freq, gain = answer["freq"], answer["gain"]
    if freq:
        step = " in steps of %g Hz" % freq[2] if freq[2] else ""
        print("  frequency   %s to %s%s" % (mhz(freq[0]), mhz(freq[1]), step))
        pieces = answer["freq_pieces"] or []
        gaps = [(a[1], b[0]) for a, b in zip(pieces, pieces[1:]) if b[0] > a[1]]
        if len(pieces) > 1:
            print("              in %d pieces: %s" % (len(pieces), ", ".join(
                "%s-%s" % (mhz(a), mhz(b)) for a, b in pieces)))
        for a, b in gaps:
            print("              GAP: nothing between %s and %s" % (mhz(a), mhz(b)))
    if gain:
        step = "in steps of %g dB" % gain[2] if gain[2] else "continuous"
        print("  gain        %g to %g dB, %s; %g dB right after opening" % (gain[0], gain[1], step, answer["gain_now"]))
        points = answer["gain_pieces"] or []
        if len(points) > 1 and all(a == b for a, b in points):
            values = [a for a, _ in points]
            widest = max(b - a for a, b in zip(values, values[1:]))
            print("              only %d values: %s (up to %g dB apart)" % (
                len(values), " ".join("%g" % v for v in values), widest))
            if widest > 1:
                print("              some steps are coarser than the knob's 1 dB per click")
        elif gain[2] > 1 and not sdr.get("gain_values"):
            print("              steps are coarser than the knob's 1 dB per click")
    for name, r in answer["stages"].items():
        print("    stage %-6s %s" % (name, "%g to %g dB, step %g" % r if r else "empty"))

    print("  the knob")
    if freq and not sdr["flowgraphs"]:
        print("    no flowgraph tunes it")
    captures = [(fg, variable(fg, "samp_rate")) for fg in sdr["flowgraphs"]]
    captures += [("--samp-rate", rate) for rate in samp_rates]
    for flowgraph, samp_rate in captures if freq else []:
        tuning = tuning_range(freq, samp_rate)
        if tuning is None:
            print("    %s: its %s capture doesn't fit" % (flowgraph, mhz(samp_rate)))
            continue
        outside = [p for p in run.presets if not tuning[0] <= p * 1e6 <= tuning[1]]
        print("    %s, %s wide: centre from %s to %s; presets %s" % (
              flowgraph, mhz(samp_rate), mhz(tuning[0]), mhz(tuning[1]),
              "outside it, to be skipped: %s MHz" % ", ".join(map(str, outside)) if outside else "all inside"))
    ladder = sdr.get("gain_values")
    if ladder:
        print("    gain %g to %g dB on the flowgraph's own ladder of %d values over the stages" % (
            ladder[0], ladder[-1], len(ladder)))
        knob_gain = (ladder[0], ladder[-1])
    elif gain:
        print("    gain %g to %g dB%s" % (gain[0], gain[1], ", on the SDR's own values" if
                                         discrete(answer) else ""))
        knob_gain = gain[:2]
    else:
        knob_gain = None
    for flowgraph in sdr["flowgraphs"] if knob_gain else []:
        start = variable(flowgraph, "gain")
        if start is not None:
            inside = knob_gain[0] <= start <= knob_gain[1]
            print("    %s starts at %g dB: %s" % (flowgraph, start, "inside" if inside else "OUTSIDE the range"))

    if "by_freq" in answer:
        print("  gain across the band (tuned to each)")
        for f, overall, stages in answer["by_freq"]:
            print("    %s  %s; %s" % (mhz(f).rjust(14), "%g to %g dB" % overall[:2] if overall else "empty", ", ".join(
                "%s %s" % (name, "%g to %g" % r[:2] if r else "empty") for name, r in stages.items())))
        if len({o for _, o, _ in answer["by_freq"]}) > 1:
            print("    the overall range changes with the frequency (run.py asks again after tuning)")
        elif len({tuple(st.items()) for _, _, st in answer["by_freq"]}) > 1:
            print("    the overall range, the one the knob uses, stays the same; only the stages change")
        else:
            print("    the same everywhere")


def check_sdr(key, sdr, args, repeat, samp_rates=(), across_band=False):
    """Returns True when every run answered the same, sane ranges."""
    if args is None:
        block, args = source_args(sdr["flowgraphs"][0])
        where = "from %s.grc" % sdr["flowgraphs"][0]
    else:
        block, where = sdr["block"], "from --args"
    print("=== %s (%s), %s %r %s" % (sdr["name"], usb_ids(sdr), block, args, where))

    answers = []
    for n in range(1, repeat + 1):
        if n > 1:
            time.sleep(1)       # let the driver let go of the device
        try:
            answer, opened = query(block, args, across_band)
        except (RuntimeError, ValueError) as e:
            print("  run %d: could not open it: %s" % (n, e))
            print("  (is fosphor_knob.sh or another program using it?)")
            return False
        print("  run %d: opened in %.1f s" % (n, opened))
        answers.append(answer)

    first = answers[0]
    describe(sdr, first, samp_rates)
    ok = True
    for problem in problems(sdr, first):
        print("  PROBLEM: %s" % problem)
        ok = False
    differing = [n for n, a in enumerate(answers, 1) if a != first]
    if differing:
        ok = False
        print("  PROBLEM: runs %s answered differently from run 1:" % ", ".join(map(str, differing)))
        for n in differing:
            print("    run %d: %s" % (n, answers[n - 1]))
    else:
        print("  all %d runs answered the same" % repeat)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sdr", choices=sorted(SDRS), help="only this SDR (default: every one plugged in)")
    ap.add_argument("--repeat", type=int, default=3, help="open each SDR this many times (default 3)")
    ap.add_argument("--args", help="device args instead of the flowgraph's (needs --sdr)")
    ap.add_argument("--gain-across-band", action="store_true",
                    help="also tune to each preset it covers and near both ends, and read the gain there")
    ap.add_argument("--samp-rate", type=float, action="append", default=[], metavar="HZ",
                    help="also show the knob's range for a capture this wide (can be repeated)")
    args = ap.parse_args()
    if args.args is not None and not args.sdr:
        ap.error("--args needs --sdr")
    if args.repeat < 1:
        ap.error("--repeat must be at least 1")

    keys = [args.sdr] if args.sdr else [k for k, s in SDRS.items() if plugged_in(s)]
    if args.sdr and not plugged_in(SDRS[args.sdr]):
        print("no %s (%s) is plugged in" % (SDRS[args.sdr]["name"], usb_ids(SDRS[args.sdr])))
        return 2
    if not keys:
        print("no supported SDR plugged in (%s)" % ", ".join(
            "%s %s" % (s["name"], usb_ids(s)) for s in SDRS.values()))
        return 2

    results = {key: check_sdr(key, SDRS[key], args.args, args.repeat, args.samp_rate, args.gain_across_band) for key in keys}
    print("\n=== summary")
    for key, ok in results.items():
        print("  %-12s %s" % (SDRS[key]["name"], "ok" if ok else "PROBLEMS"))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
