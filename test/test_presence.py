"""What the runtime asks of a Slack place (`send --describe`), what it says of a body at work (`send --status`): 👀 on
the message a body took while it works, off once it answers or is done; and a body's message to one person is one
DM (t-1756)."""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from loomvenue import common
from test.fixtures import GATES, FakeSlack
from test.test_send import ENV, FakeSecrets, cli


def gates_argv(gates):
    """SLACK_GATES_COMMAND for a test: an argv that prints {"doc": gates}."""
    return json.dumps([sys.executable, "-c", f"import json; print(json.dumps({{'doc': {gates!r}}}))"])


class Presence(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="loom-slack-p-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = cli.config({"SLACK_MODE": "live", "SLACK_BOTS": '{"kit": "kit-slack.env"}', "SLACK_DATA": str(self.dir),
                               "SLACK_GATES_COMMAND": gates_argv(GATES)})
        self.api = FakeSlack()

    def send(self, argv, text=""):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.send(self.cfg, argv, stdin=io.StringIO(text), secrets=FakeSecrets(ENV), api_for=lambda t: self.api)
        return json.loads(out.getvalue())

    def reacts(self):
        return [(m, p["channel"], p["timestamp"]) for m, p in self.api.calls if m.startswith("reactions.")]

    def test_a_slack_place_is_never_a_guest_or_peer_venue(self):
        self.assertEqual(self.send(["--place", "dm:U1", "--agent", "kit", "--describe"]),
                         {"ok": True, "guest": False, "peer": False, "venue": "U1", "readers": [], "members": ["robin"],
                          "notes": []})
        self.assertFalse(self.send(["--place", "channel:1", "--agent", "kit", "--describe"])["ok"])
        self.assertFalse(self.send(["--place", "dm:U1", "--agent", "bolt", "--describe"])["ok"])

    def test_eyes_on_the_message_a_body_took_until_it_answers(self):
        p = common.Presence(self.dir)
        self.assertEqual(self.send(["--place", "dm:U1", "--agent", "kit", "--status", "working", "--message", "kit:DU1:1.000100"]), {"ok": True})
        state = {}
        p.show(state, {"kit": self.api}, cli.react)
        self.assertEqual(self.reacts(), [("reactions.add", "DU1", "1.000100")])
        p.show(state, {"kit": self.api}, cli.react)
        self.assertEqual(len(self.reacts()), 1, "once")
        # It answers (its send posts there): 👀 off.
        self.assertTrue(self.send(["--place", "dm:U1", "--agent", "kit"], "On it: Thursday at 3.")["ok"])
        p.show(state, {"kit": self.api}, cli.react)
        self.assertEqual(self.reacts()[-1], ("reactions.remove", "DU1", "1.000100"))
        self.send(["--place", "dm:U1", "--agent", "kit", "--status", "done"])
        p.show(state, {"kit": self.api}, cli.react)
        self.assertEqual(p.all(), [])
        self.assertEqual(len(self.reacts()), 2)

    def test_a_message_to_one_person_is_one_dm(self):
        a = self.send(["--place", "dm:U9", "--agent", "kit"], "The build is green.")
        self.assertTrue(a["ok"])
        self.assertEqual([m for m, _ in self.api.calls], ["conversations.open", "chat.postMessage"])
        self.assertEqual(self.api.calls[1][1], {"channel": "DU9", "text": "The build is green."})


if __name__ == "__main__":
    unittest.main()
