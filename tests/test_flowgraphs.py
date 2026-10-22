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
import tempfile
import unittest

try:
    import yaml        # .grc files are YAML; GNU Radio needs PyYAML anyway
except ImportError:
    yaml = None

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
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
