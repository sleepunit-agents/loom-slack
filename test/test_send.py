"""The venue's `send`: --place and --agent, the text on stdin, {ok, id?}; posted as that agent's own app;
nothing at all in shadow."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

from loomslack import routing
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
        self.assertEqual(ans, {"ok": False, "reason": "no Slack bot for bolt", "permanent": True})
        _, ans = self.run_send(["--place", "channel:30", "--agent", "kit"], "x")
        self.assertFalse(ans["ok"])
        _, ans = self.run_send(["--place", "dm:U1", "--agent", "kit"], "x", secrets=FakeSecrets("SLACK_TEAM_ID=T1\n"))
        self.assertIn("SLACK_BOT_TOKEN", ans["reason"])
        self.api.fail = {"chat.postMessage"}
        _, ans = self.run_send(["--place", "dm:U1", "--agent", "kit"], "x")
        self.assertFalse(ans["ok"])
        self.assertNotIn("xb", json.dumps(ans))


class Failing:
    """Slack answering every call with one failure."""
    def __init__(self, error):
        self.error = error

    def call(self, method, **p):
        raise self.error


def http(code):
    return urllib.error.HTTPError("https://slack.com/api/chat.postMessage", code, "x", {}, None)


class Permanent(unittest.TestCase):
    """`permanent: true` when no retry will ever post it (agent-runtime.md, "Outbound"): the runtime records it
    refused, doesn't retry it and keeps nothing for a body. A refusal for now says no `permanent`."""

    def setUp(self):
        self.api = FakeSlack()
        self.cfg = cli.config({"SLACK_MODE": "live", "SLACK_BOTS": '{"kit": "kit-slack.env"}'})

    def ans(self, argv, text="x", cfg=None, secrets=None, api=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.send(cfg or self.cfg, argv, stdin=io.StringIO(text), secrets=secrets or FakeSecrets(),
                            api_for=lambda tok: api or self.api)
        self.assertEqual(code, 1)
        return json.loads(out.getvalue())

    def test_shadow_no_bot_a_place_that_isnt_slacks_and_nothing_to_send_are_for_good(self):
        shadow = cli.config({"SLACK_BOTS": '{"kit": "kit-slack.env"}'})
        for argv, text, cfg in ((["--place", "dm:U1", "--agent", "kit"], "x", shadow),
                                (["--place", "dm:U1", "--agent", "bolt"], "x", None),
                                (["--place", "channel:30", "--agent", "kit"], "x", None),
                                (["--place", "dm:U-1", "--agent", "kit"], "x", None),
                                (["--agent", "kit"], "x", None),
                                (["--place", "dm:U1", "--agent", "kit"], "  \n", None)):
            with self.subTest(argv=argv, shadow=cfg is shadow):
                a = self.ans(argv, text, cfg)
                self.assertIs(a["permanent"], True)
                self.assertFalse(a["ok"])
        self.assertEqual(self.api.calls, [])

    def test_a_conversation_slack_wont_let_it_post_in_is_for_good(self):
        for code in ("channel_not_found", "not_in_channel", "is_archived", "missing_scope", "invalid_auth",
                     "cannot_dm_bot", "user_not_found"):
            with self.subTest(code=code):
                a = self.ans(["--place", "dm:U1", "--agent", "kit"],
                             api=Failing(routing.SlackError(f"chat.postMessage: {code}", code)))
                self.assertEqual(a, {"ok": False, "reason": f"Slack refused the post: chat.postMessage: {code}",
                                     "permanent": True})
        for code in (400, 403, 404):
            with self.subTest(http=code):
                self.assertIs(self.ans(["--place", "group:G1", "--agent", "kit"], api=Failing(http(code)))["permanent"], True)

    def test_a_rate_limit_slacks_own_trouble_a_5xx_the_network_or_an_unallowed_secret_is_for_now(self):
        for err in (routing.RateLimited(60), routing.SlackError("chat.postMessage: fatal_error"),
                    routing.SlackError("chat.postMessage: internal_error", "internal_error"),
                    routing.SlackError("chat.postMessage: service_unavailable", "service_unavailable"),
                    routing.SlackError("chat.postMessage: request_timeout", "request_timeout"),
                    http(408), http(500), http(503), urllib.error.URLError("down"), TimeoutError(), KeyError("channel")):
            with self.subTest(err=repr(err)):
                self.assertNotIn("permanent", self.ans(["--place", "dm:U1", "--agent", "kit"], api=Failing(err)))
        self.assertNotIn("permanent", self.ans(["--place", "dm:U1", "--agent", "kit"], secrets=FakeSecrets(ok=False)))

    def test_settings_that_dont_parse_are_for_good(self):
        out = io.StringIO()
        env = {"LOOM_PIECE_NAME": "slack", "SLACK_BOTS": "not json"}
        with unittest.mock.patch.dict("os.environ", env, clear=True), contextlib.redirect_stdout(out):
            code = cli.main(["send", "--place", "dm:U1", "--agent", "kit"])
        self.assertEqual(code, 1)
        self.assertIs(json.loads(out.getvalue())["permanent"], True)


if __name__ == "__main__":
    unittest.main()
