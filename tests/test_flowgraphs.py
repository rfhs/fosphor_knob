"""Checks on the GNU Radio flowgraphs (*.grc) that fosphor_knob.sh starts.

    python3 -m unittest discover -s tests -p test_flowgraphs.py -v

Every flowgraph must compile with grcc into the script fosphor_knob.sh runs
(grcc names it after the flowgraph's id), and that script must import, which
needs every block's Python module: gr-osmosdr, gr-fosphor, gr-paint and
gr-uhd. Those checks are skipped where GNU Radio isn't installed; CI runs
them in .github/workflows/flowgraphs.yml.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import yaml        # .grc files are YAML; GNU Radio needs PyYAML anyway
except ImportError:
    yaml = None

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
import sdr_limits  # noqa: E402
import sdr_ranges  # noqa: E402

FLOWGRAPHS = sorted(name[:-len(".grc")] for name in os.listdir(REPO) if name.endswith(".grc"))
WIDE_SAMPLE_RATE = "122.88e6"
# hackrf_sweep sweeps the whole range: there is nothing to tune, and
# fosphor_knob.sh doesn't start run.py for it.
UNTUNED = ("fosphor_knob_hackrf_sweep", "fosphor_knob_hackrf_sweep_sponsors")


def load(name):
    with open(os.path.join(REPO, name + ".grc")) as f:
        return yaml.safe_load(f)


def blocks(flowgraph, kind):
    return [block for block in flowgraph["blocks"] if block["id"] == kind]


def variables(flowgraph):
    return {block["name"]: block["parameters"]["value"] for block in blocks(flowgraph, "variable")}


@unittest.skipUnless(shutil.which("grcc"), "needs GNU Radio (grcc)")
class Flowgraphs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.TemporaryDirectory()
        cls.compiled = {}
        for name in FLOWGRAPHS:
            result = subprocess.run(["grcc", "-o", cls.out.name, os.path.join(REPO, name + ".grc")],
                                    capture_output=True, text=True)
            cls.compiled[name] = result

    @classmethod
    def tearDownClass(cls):
        cls.out.cleanup()

    def script(self, name):
        return os.path.join(self.out.name, name + ".py")

    def test_compile(self):
        for name in FLOWGRAPHS:
            with self.subTest(name):
                result = self.compiled[name]
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(os.path.exists(self.script(name)),
                                "grcc didn't write %s.py: the flowgraph's id must be its file name" % name)

    def test_scripts_import(self):
        for name in FLOWGRAPHS:
            with self.subTest(name):
                if not os.path.exists(self.script(name)):
                    self.skipTest("didn't compile")
                spec = importlib.util.spec_from_file_location(name, self.script(name))
                spec.loader.exec_module(importlib.util.module_from_spec(spec))

    def test_run_py_can_reach_them(self):
        """run.py tunes through XML-RPC on 127.0.0.1:8080 (its targetip),
        which mustn't be reachable from the network."""
        for name in FLOWGRAPHS:
            with self.subTest(name):
                flowgraph = load(name)
                servers = [(b["parameters"]["addr"], b["parameters"]["port"])
                           for b in blocks(flowgraph, "xmlrpc_server")]
                self.assertEqual(servers, [("127.0.0.1", "8080")])
                if name not in UNTUNED:
                    # run.py calls get_/set_freq and get_/set_gain
                    self.assertLessEqual({"freq", "gain"}, set(variables(flowgraph)))


class Launcher(unittest.TestCase):
    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_wide_flowgraphs_match_the_normal_ones(self):
        """A wide flowgraph is its normal one at 122.88 MHz."""
        wide = [name for name in FLOWGRAPHS if "_wide" in name]
        self.assertTrue(wide)
        for name in wide:
            with self.subTest(name):
                normal = load(name.replace("_wide", ""))
                flowgraph = load(name)
                self.assertEqual(flowgraph["options"]["parameters"]["id"], name)
                self.assertEqual(variables(flowgraph)["samp_rate"], WIDE_SAMPLE_RATE)
                for f in (normal, flowgraph):
                    del f["options"]["parameters"]["id"]
                    for block in blocks(f, "variable"):
                        if block["name"] == "samp_rate":
                            del block["parameters"]["value"]
                self.assertEqual(flowgraph, normal)

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_tuned_flowgraphs_publish_the_knob_limits(self):
        """run.py reads get_knob_limits() over XML-RPC; the snippet defining it must run
        before the xmlrpc_server block starts serving, and answer {} until the source exists."""
        for name in FLOWGRAPHS:
            with self.subTest(name):
                snippets = blocks(load(name), "snippet")
                if name in UNTUNED:
                    self.assertEqual(snippets, [])
                    continue
                self.assertEqual(len(snippets), 1)
                parameters = snippets[0]["parameters"]
                self.assertEqual(parameters["section"], "init_before_blocks")
                self.assertIn("sdr_limits.describe", parameters["code"])
                sources = [b["name"] for b in load(name)["blocks"] if b["id"] in ("osmosdr_source", "uhd_usrp_source")]
                self.assertEqual(len(sources), 1)
                self.assertIn("'%s'" % sources[0], parameters["code"])

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_hackrf_gain_ladder_matches_sdr_limits(self):
        """The HackRF flowgraphs split the knob's gain over the LNA and VGA in
        plain GRC variables (grcc can't import sdr_limits); they must give
        what sdr_limits.hackrf_split() says, for the ladder they publish."""
        for name in ("fosphor_knob_hackrf", "fosphor_knob_hackrf_sponsors"):
            flowgraph = load(name)
            values = variables(flowgraph)
            source = blocks(flowgraph, "osmosdr_source")[0]["parameters"]
            self.assertEqual(str(source["gain0"]), "0", "the RF amp stays off")
            self.assertIn("HACKRF_GAINS", blocks(flowgraph, "snippet")[0]["parameters"]["code"])
            for gain in sdr_limits.HACKRF_GAINS + [-5, 1, 3, 101, 103, 200]:
                with self.subTest(name, gain=gain):
                    scope = {"gain": gain}
                    for var in ("hackrf_ladder", "lna_gain", "vga_gain"):
                        scope[var] = eval(values[var], {}, scope)
                    got = (eval(str(source["if_gain0"]), {}, scope), eval(str(source["bb_gain0"]), {}, scope))
                    self.assertEqual(got, sdr_limits.hackrf_split(gain))

    def test_hackrf_sweep_is_only_started_by_its_own_name(self):
        """Autodetection starts the tuned HackRF flowgraphs; the sweep has no knob."""
        with open(os.path.join(REPO, "fosphor_knob.sh")) as f:
            script = f.read()
        dispatch = script[script.index("use_knob=0"):]
        condition = ""
        calls = 0
        for line in dispatch.splitlines():
            if re.match(r"(if|elif|else)\b", line):
                condition = line
            elif re.match(r"\s+exec_hackrf_sweep", line):
                calls += 1
                self.assertRegex(condition, r"fosphor_knob_hackrf_sweep(_sponsors)?\"")
        self.assertEqual(calls, 2)

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_sdr_ranges_opens_sdrs_like_the_flowgraphs(self):
        """tests/sdr_ranges.py knows every tuned flowgraph and reads its source's device args."""
        known = [fg for sdr in sdr_ranges.SDRS.values() for fg in sdr["flowgraphs"]]
        self.assertEqual(sorted(known), sorted(set(FLOWGRAPHS) - set(UNTUNED)))
        for sdr in sdr_ranges.SDRS.values():
            for name in sdr["flowgraphs"]:
                with self.subTest(name):
                    block, args = sdr_ranges.source_args(name)
                    self.assertEqual(block, sdr["block"])
                    self.assertNotIn('"', args)
                    self.assertIsNotNone(sdr_ranges.variable(name, "gain"))
                    self.assertGreater(sdr_ranges.variable(name, "samp_rate"), 0)

    def test_every_flowgraph_it_starts_exists(self):
        with open(os.path.join(REPO, "fosphor_knob.sh")) as f:
            script = f.read()
        started = set(re.findall(r"/usr/share/fosphor_knob/(fosphor_knob_\w+)\.py", script))
        for wide, normal in re.findall(r"start_bladerf2 (\w+) (\w+)", script):
            started |= {wide, normal}
        self.assertTrue(started)
        self.assertLessEqual(started, set(FLOWGRAPHS))


if __name__ == "__main__":
    unittest.main()
