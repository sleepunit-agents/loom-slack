"""What every source must keep (loom docs/specs/agent-runtime.md, "What the tests must pin", test 1): a source only
calls `deliver`. This piece's code never names a harness binary or a session command. And it names no household:
no agent, person, server or workspace of a real instance (they come from settings, the runtime and the gates)."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = [p for p in [*ROOT.joinpath("bin").iterdir(), *ROOT.joinpath("lib").rglob("*.py")] if p.is_file()]

HARNESS = [r"\bclaude\b", r"\bcodex\b", r"--bg\b", r"--resume\b", r"\bbody start\b", r"\bbody resume\b",
           r"\bbody feed\b", r"\bbody-host\b", r"\bremote-control\b", r"\bsession_id\b", r"\bspawn\b"]
INSTANCE = [r"\bart\b", r"\bmark\b", r"\bjonathan\b", r"\bonterris\b", r"\bsleepunit\.com\b", r"\bfez\b",
            r"\bgabe\b", r"\bloren\b", r"\d{17,20}"]


def hits(patterns):
    out = []
    for p in CODE:
        for i, line in enumerate(p.read_text().splitlines(), 1):
            for pat in patterns:
                if re.search(pat, line, re.IGNORECASE) and "github.com/sleepunit-agents/" not in line:
                    out.append(f"{p.relative_to(ROOT)}:{i}: {pat}")
    return out


class Contract(unittest.TestCase):
    def test_no_harness_binary_or_session_command(self):
        self.assertEqual(hits(HARNESS), [], "a source only calls deliver")

    def test_no_instance_names(self):
        self.assertEqual(hits(INSTANCE), [])

    def test_the_test_would_catch_one(self):
        self.assertTrue(re.search(HARNESS[0], 'subprocess.run(["claude", "-p"])', re.IGNORECASE))

    def test_the_loom_block(self):
        pkg = json.loads((ROOT / "package.json").read_text())
        block, bins = pkg["loom"], set(pkg["bin"])
        self.assertEqual(pkg["license"], "AGPL-3.0-or-later")
        for name, v in block["venues"].items():
            self.assertEqual(set(v) - {"label", "kind", "render", "send"}, set())
            self.assertIn(v["send"][0], bins)
        for s in block["services"]:
            self.assertIn(s["argv"][0], bins)
        self.assertNotIn("delivery", block)
        self.assertNotIn("grants", block)

    def test_common_is_the_shared_copy(self):
        # loomvenue/common.py is the same file in loom-discord and loom-slack: the header says so.
        self.assertIn("ships in loom-discord and loom-slack", (ROOT / "lib/loomvenue/common.py").read_text())


if __name__ == "__main__":
    unittest.main()
