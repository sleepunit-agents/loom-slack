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

    def test_a_message_the_platform_no_longer_has_is_gone_and_counts_neither_way(self):
        b = self.rows(22)
        asked = []

        def gone(mid, pairs):
            asked.append((mid, pairs))
            return {"20": True, "21": None}.get(mid, False)
        r = self.run_cmp(self.rows(20), b, gone=gone)
        self.assertEqual((r["verdict"], r["gone"], r["only_bridge"], r["messages"]), ("disagree", ["20"], ["21"], 21))
        self.assertEqual(asked, [("20", [("kit", "dm:U1")]), ("21", [("kit", "dm:U1")])], "only the bridge's are asked")
        r = self.run_cmp(self.rows(20), self.rows(21), gone=lambda m, p: True)
        self.assertEqual((r["verdict"], r["gone"], r["same"]), ("agree", ["20"], 20))
        r = self.run_cmp(self.rows(20), self.rows(21))
        self.assertEqual((r["verdict"], r["gone"]), ("disagree", []), "without asking, nothing is gone")


class StillThere(unittest.TestCase):
    """loom-slack compare --check-gone: Slack's word on a message only the bridge routed, by ts alone."""

    def setUp(self):
        import importlib.machinery
        import importlib.util
        from test.fixtures import FakeSlack, ev
        binp = Path(__file__).resolve().parent.parent / "bin" / "loom-slack"
        loader = importlib.machinery.SourceFileLoader("loom_slack_bin_cmp", str(binp))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.cli = importlib.util.module_from_spec(spec)
        loader.exec_module(self.cli)
        parent = ev(T0 + 1, "U1", "p")
        self.api = FakeSlack(history={"D1": [parent, ev(T0 + 3, "U1", "top")]},
                             replies={("D1", parent["ts"]): [ev(T0 + 2 + i / 100, "U1", "r") for i in range(30)]})

    def test_the_timeline_a_thread_or_gone(self):
        self.assertIs(self.cli.still_there(self.api, f"D1:{T0 + 3:.6f}"), True)
        self.assertIs(self.cli.still_there(self.api, f"D1:{T0 + 2 + 29 / 100:.6f}"), True, "a thread's last page")
        self.assertIs(self.cli.still_there(self.api, f"D1:{T0 + 9:.6f}"), False)
        self.api.fail = {"conversations.history"}
        self.assertIsNone(self.cli.still_there(self.api, f"D1:{T0 + 3:.6f}"), "Slack down: can't tell")
        self.assertIsNone(self.cli.still_there(self.api, "not an id"))
        for m, p in self.api.calls:
            self.assertIn(m, ("conversations.history", "conversations.replies"))

    def test_the_checker_asks_as_the_agents_own_bot_and_waits_out_a_rate_limit(self):
        from loomslack import routing
        from loomvenue import common as c
        cfg = {"bots": {"kit": "kit-slack.env"}, "may": [], "get": []}

        class Secrets:
            def get(self, agent, name):
                return "SLACK_BOT_TOKEN=xb\nSLACK_TEAM_ID=T1\nSLACK_BOT_USER_ID=B0\n"
        tokens, slept, n = [], [], {"v": 0}
        api = self.api
        orig = api.call

        def call(method, **p):
            n["v"] += 1
            if n["v"] == 1:
                raise routing.RateLimited(5)
            return orig(method, **p)
        api.call = call
        gone = self.cli.gone_checker(cfg, Secrets(), api_for=lambda t: tokens.append(t) or api, sleep=slept.append)()
        self.assertIs(gone(f"D1:{T0 + 9:.6f}", [("kit", "dm:U1")]), True)
        self.assertIs(gone(f"D1:{T0 + 3:.6f}", [("kit", "dm:U1")]), False)
        self.assertIsNone(gone(f"D1:{T0 + 3:.6f}", [("bolt", "dm:U1")]), "no bot for that agent")
        self.assertEqual((tokens, slept), (["xb"], [5]))
        self.assertIsInstance(c.SecretRefused("x"), Exception)


if __name__ == "__main__":
    unittest.main()
