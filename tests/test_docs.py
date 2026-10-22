#!/usr/bin/env python3
"""The READMEs keep up with the code; run by CI.

    python3 -m unittest discover -s tests -p test_docs.py -v
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
READMES = ("README.md", os.path.join("firmware", "README.md"))


def read(*path):
    with open(os.path.join(REPO, *path)) as f:
        return f.read()


class Docs(unittest.TestCase):
    def test_every_build_option_is_documented(self):
        kconfig = set(re.findall(r"^\s*config (PM_\w+)", read("firmware", "main", "Kconfig.projbuild"), re.M))
        table = set(re.findall(r"^\| `(PM_\w+)` \|", read("firmware", "README.md"), re.M))
        self.assertEqual(kconfig - table, set(), "missing from the build options table in firmware/README.md")
        self.assertEqual(table - kconfig, set(), "documented but not in Kconfig.projbuild")

    def test_mentioned_scripts_exist(self):
        for readme in READMES:
            base = os.path.dirname(os.path.join(REPO, readme))     # the paths are relative to the README
            text = read(readme)
            for path in set(re.findall(r"((?:\.\./)?(?:tests|tools|firmware/tools|udev)/[\w.-]+\.(?:py|rules))", text)):
                self.assertTrue(os.path.exists(os.path.join(base, path)), "%s mentions %s" % (readme, path))

    def test_mentioned_pmknob_commands_exist(self):
        commands = set(re.findall(r'add_parser\("([\w-]+)"\)', read("firmware", "tools", "pmknob.py")))
        for readme in READMES:
            for cmd in re.findall(r"pmknob\.py ([a-z][\w-]*)", read(readme)):
                self.assertIn(cmd, commands, "%s mentions pmknob.py %s" % (readme, cmd))


if __name__ == "__main__":
    unittest.main()
