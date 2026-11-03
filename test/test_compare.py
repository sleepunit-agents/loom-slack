"""The shadow window's comparison: the two logs, matched by message id."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from loomvenue import common

T0 = 1_800_000_000.0


def write(path, rows):
    Path(path).write_text("".join(json.dumps(dict({"platform": "slack"}, **r)) + "\n" for r in rows))


class Compare(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="loom-compare-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def rows(self, n, start=T0 + 3600):
        return [{"at": start + i, "id": str(i), "agent": "kit", "place": "dm:U1"} for i in range(n)]

    def run_cmp(self, a, b, **kw):
        write(self.d / "a.jsonl", a)
        write(self.d / "b.jsonl", b)
        return common.compare([str(self.d / "a.jsonl")], [str(self.d / "b*.jsonl")], "slack", T0, T0 + 86400, **kw)

    def test_agree_needs_every_message_the_same_and_at_least_twenty(self):
        self.assertEqual(self.run_cmp(self.rows(20), self.rows(20))["verdict"], "agree")
        self.assertEqual(self.run_cmp(self.rows(19), self.rows(19))["verdict"], "too-few")

    def test_a_different_place_or_agent_or_a_missing_message_disagrees(self):
        b = self.rows(25)
        b[3] = dict(b[3], place="group:G1")
        r = self.run_cmp(self.rows(25), b)
        self.assertEqual((r["verdict"], [x["id"] for x in r["differ"]]), ("disagree", ["3"]))
        r = self.run_cmp(self.rows(25), self.rows(24))
        self.assertEqual((r["verdict"], r["only_adapter"]), ("disagree", ["24"]))
        r = self.run_cmp(self.rows(25) + [{"at": T0 + 9000, "id": "7", "agent": "bolt", "place": "dm:U1"}], self.rows(25))
        self.assertEqual([x["id"] for x in r["differ"]], ["7"])

    def test_the_edges_and_other_platforms_dont_count(self):
        edge = [{"at": T0 + 5, "id": "e", "agent": "kit", "place": "dm:U1"}]
        r = self.run_cmp(self.rows(20) + edge, self.rows(20))
        self.assertEqual(r["verdict"], "agree")
        write(self.d / "b2.jsonl", [{"at": T0 + 4000, "id": "s", "agent": "kit", "place": "dm:U1", "platform": "discord"}])
        self.assertEqual(self.run_cmp(self.rows(20), self.rows(20))["verdict"], "agree")

    def test_repeated_lines_are_one_message(self):
        self.assertEqual(self.run_cmp(self.rows(20) * 2, self.rows(20))["verdict"], "agree")


if __name__ == "__main__":
    unittest.main()
