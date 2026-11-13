"""What the knob may do with the SDR a flowgraph has open.

The tuned flowgraphs publish describe() over XML-RPC as get_knob_limits(),
and run.py keeps the knob inside it. tests/sdr_ranges.py uses the same code
to report what an SDR says about itself.
"""

import re

# The HackRF's knob gain is one ladder over its LNA (IF, 8 dB steps) and VGA
# (BB, 2 dB steps), with the RF amp off.
HACKRF_LNA_MAX, HACKRF_LNA_STEP = 40, 8
HACKRF_VGA_MAX, HACKRF_VGA_STEP = 62, 2
HACKRF_GAINS = [float(g) for g in range(0, HACKRF_LNA_MAX + HACKRF_VGA_MAX + 1, HACKRF_VGA_STEP)]


def hackrf_split(gain):
    """(LNA, VGA) gains in dB for a gain on the HackRF ladder; the LNA takes
    as many whole steps as fit, the VGA the rest."""
    g = min(max(gain, 0), HACKRF_GAINS[-1])
    g = round(g / HACKRF_VGA_STEP) * HACKRF_VGA_STEP
    lna = min(HACKRF_LNA_MAX, g // HACKRF_LNA_STEP * HACKRF_LNA_STEP)
    return float(lna), float(g - lna)


def span(r):
    """(start, stop, step) of a meta_range_t, or None when it is empty."""
    try:
        return (r.start(), r.stop(), r.step())
    except RuntimeError:
        return None


def sub_ranges(r):
    """The pieces of an osmosdr meta_range_t as (start, stop), a single value
    as (v, v); None for gr-uhd's, which can't list them. osmosdr's values()
    has no Python binding, so its printed form is parsed."""
    if not hasattr(r, "to_pp_string"):
        return None
    pieces = []
    for line in r.to_pp_string().splitlines():
        numbers = [float(n) for n in re.findall(r"[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?", line)]
        if numbers:
            pieces.append((numbers[0], numbers[1] if len(numbers) > 1 else numbers[0]))
    return sorted(pieces)


def tuning_range(freq, samp_rate):
    """Where the centre can go so the whole samp_rate wide capture stays in
    the SDR's range, or None when it doesn't fit at all."""
    low, high = freq[0] + samp_rate / 2, freq[1] - samp_rate / 2
    return (low, high) if low <= high else None


def discrete_values(gain_range):
    """The gain values of a range made of single points (RTL-SDR), else []."""
    pieces = sub_ranges(gain_range) or []
    if len(pieces) > 1 and all(a == b for a, b in pieces):
        return [a for a, _ in pieces]
    return []


def describe(source, samp_rate, gain_values=None):
    """The knob's limits for an osmosdr or gr-uhd source block, as a dict of
    floats (XML-RPC ints are only 32 bits): {} while the source doesn't exist
    yet, {"error": ...} when the driver can't say."""
    if source is None:
        return {}
    try:
        freq = span(source.get_freq_range(0))
        if freq is None:
            return {"error": "the SDR reports no frequency range"}
        try:
            rate = float(source.get_samp_rate())
        except Exception:
            rate = 0.0
        rate = rate if rate > 0 else float(samp_rate)
        tuning = tuning_range(freq, rate)
        if tuning is None:
            return {"error": "a %.3f MHz capture is wider than the SDR's range" % (rate / 1e6)}
        if gain_values is None:
            gain_range = source.get_gain_range(0)
            gain = span(gain_range)
            if gain is None:
                return {"error": "the SDR reports no gain range"}
            gain_values = discrete_values(gain_range)
            gain_min, gain_max, gain_step = gain
        else:
            gain_values = sorted(float(g) for g in gain_values)
            gain_min, gain_max, gain_step = gain_values[0], gain_values[-1], 0.0
        return {
            "freq_min": float(tuning[0]),
            "freq_max": float(tuning[1]),
            "hw_freq_min": float(freq[0]),
            "hw_freq_max": float(freq[1]),
            "samp_rate": rate,
            "gain_min": float(gain_min),
            "gain_max": float(gain_max),
            "gain_step": float(gain_step),
            "gain_values": [float(g) for g in gain_values],
        }
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
