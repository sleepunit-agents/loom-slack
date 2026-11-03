"""The venue's `send`: --place and --agent, the text on stdin, {ok, id?}; posted as that agent's own app;
nothing at all in shadow."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import unittest
from pathlib import Path

from loomvenue import common
from test.fixtures import FakeSlack

BIN = Path(__file__).resolve().parent.parent / "bin" / "loom-slack"
loader = importlib.machinery.SourceFileLoader("loom_slack_bin", str(BIN))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)

ENV = "SLACK_BOT_TOKEN=xb\nSLACK_APP_TOKEN=xa\nSLACK_TEAM_ID=T1\nSLACK_BOT_USER_ID=B0\n"


class FakeSecrets:
    def __init__(self, value=ENV, ok=True):
        self.value, self.ok, self.asked = value, ok, []

    def get(self, agent, name):
        self.asked.append((agent, name))
        if not self.ok:
            raise common.SecretRefused("the secrets policy says ask")
        return self.value


class Send(unittest.TestCase):
    def setUp(self):
        self.api = FakeSlack()
        self.tokens = []
        self.cfg = cli.config({"SLACK_MODE": "live", "SLACK_BOTS": '{"kit": "kit-slack.env"}'})

    def run_send(self, argv, text, cfg=None, secrets=None):
        out = io.StringIO()

        def api_for(tok):
            self.tokens.append(tok)
            return self.api
        with contextlib.redirect_stdout(out):
            code = cli.send(cfg or self.cfg, argv, stdin=io.StringIO(text), secrets=secrets or FakeSecrets(), api_for=api_for)
        return code, json.loads(out.getvalue())

    def posts(self):
        return [c[1] for c in self.api.calls if c[0] == "chat.postMessage"]

    def test_shadow_posts_nothing(self):
        cfg = cli.config({"SLACK_BOTS": '{"kit": "kit-slack.env"}'})
        sec = FakeSecrets()
        code, ans = self.run_send(["--place", "dm:U1", "--agent", "kit"], "hi", cfg=cfg, secrets=sec)
        self.assertEqual((code, ans["ok"]), (1, False))
        self.assertIn("shadow", ans["reason"])
        self.assertEqual((self.api.calls, sec.asked), ([], []))

    def test_a_dm_opens_the_conversation_and_posts_as_the_agents_bot(self):
        code, ans = self.run_send(["--place", "dm:U1", "--agent", "kit"], "Yes, Thursday at 3.")
        self.assertEqual(code, 0)
        self.assertTrue(ans["ok"] and ans["id"].startswith("DU1:"))
        self.assertEqual(self.tokens, ["xb"])
        self.assertEqual(self.posts(), [{"channel": "DU1", "text": "Yes, Thursday at 3."}])

    def test_a_group_posts_to_its_own_id_and_long_text_is_split(self):
        code, _ = self.run_send(["--place", "group:G1", "--agent", "kit"], "word\n" * 2000)
        self.assertEqual(code, 0)
        self.assertGreater(len(self.posts()), 1)
        self.assertTrue(all(p["channel"] == "G1" and len(p["text"]) <= cli.MAX_POST for p in self.posts()))

    def test_refusals_say_why_and_never_carry_a_token(self):
        _, ans = self.run_send(["--place", "dm:U1", "--agent", "bolt"], "x")
        self.assertEqual(ans, {"ok": False, "reason": "no Slack bot for bolt"})
        _, ans = self.run_send(["--place", "channel:30", "--agent", "kit"], "x")
        self.assertFalse(ans["ok"])
        _, ans = self.run_send(["--place", "dm:U1", "--agent", "kit"], "x", secrets=FakeSecrets("SLACK_TEAM_ID=T1\n"))
        self.assertIn("SLACK_BOT_TOKEN", ans["reason"])
        self.api.fail = {"chat.postMessage"}
        _, ans = self.run_send(["--place", "dm:U1", "--agent", "kit"], "x")
        self.assertFalse(ans["ok"])
        self.assertNotIn("xb", json.dumps(ans))


if __name__ == "__main__":
    unittest.main()
