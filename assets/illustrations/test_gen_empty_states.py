"""The illustration guard must FAIL on a stale module, not only pass on a fresh one.

Same reasoning as assets/test_check_icons.py: a drift check only ever run
against correct output proves nothing. Standard library only; CI's Brand assets
job runs it.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "gen_empty_states.py")
MODULE = os.path.join(
    os.path.dirname(os.path.dirname(HERE)),
    "frontend", "src", "lib", "components", "ui", "emptyStateArt.generated.ts",
)


def check(module):
    return subprocess.run(
        [sys.executable, GEN, "--check", "--module", module], capture_output=True, text=True
    )


class CheckFailsOnDrift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.copy = os.path.join(self.tmp, "art.ts")
        shutil.copy(MODULE, self.copy)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_committed_module_passes(self):
        self.assertEqual(check(self.copy).returncode, 0)

    def test_edited_geometry_fails(self):
        with open(self.copy) as f:
            text = f.read()
        # One coordinate nudged by hand — the edit this guard exists to catch.
        with open(self.copy, "w") as f:
            f.write(text.replace('"cx": 116', '"cx": 117', 1))
        result = check(self.copy)
        self.assertEqual(result.returncode, 1)
        self.assertIn("stale", result.stderr)

    def test_missing_module_fails(self):
        os.remove(self.copy)
        self.assertEqual(check(self.copy).returncode, 1)


if __name__ == "__main__":
    unittest.main()
