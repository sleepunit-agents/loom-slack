"""The intakes: shadow reads the Web API (never a socket, never a post), logs what it would deliver, and pages
through; live hands each message to the sink once, and keeps it while loom or Slack can't answer."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from loomslack import adapter
from loomvenue import common
from test.fixtures import GATES, FakeSlack, ev

T0 = 1_800_000_000.0


class StaticDoc:
    def __init__(self, doc):
        self.d = doc

    def doc(self):
        return self.d


class Intake(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="loom-slack-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.t = [T0]
        self.api = FakeSlack(convs=[{"id": "D1", "is_im": True}, {"id": "G1", "is_mpim": True}],
                             members={"G1": ["B0", "U1", "U2"]})
        self.agents = {"kit": adapter.Agent("kit", self.api, "T1", "B0", StaticDoc(GATES))}

    def now(self):
        return self.t[0]

    def log(self):
        p = self.dir / common.ShadowLog.NAME
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def poller(self):
        return adapter.Poller(adapter.Router(self.dir, common.ShadowLog(self.dir, "slack", now=self.now), now=self.now),
                              self.agents, now=self.now)

    def test_shadow_logs_what_it_would_deliver_reads_only_and_keeps_no_text(self):
        p = self.poller()
        self.api.history = {"D1": [ev(T0 - 5, "U1", "before the start"), ev(T0 + 1, "U1", "private words"),
                                   ev(T0 + 2, "B0", "my own reply"), ev(T0 + 3, "U1", "again")],
                            "G1": [ev(T0 + 4, "U2", "group")]}
        for _ in range(4):
            p.tick()
        got = [(x["id"], x["agent"], x["place"]) for x in self.log()]
        self.assertEqual(sorted(got), sorted([(f"D1:{T0 + 1:.6f}", "kit", "dm:U1"), (f"D1:{T0 + 3:.6f}", "kit", "dm:U1"),
                                              (f"G1:{T0 + 4:.6f}", "kit", "group:G1")]))
        methods = {c[0] for c in self.api.calls}
        self.assertLessEqual(methods, {"conversations.list", "conversations.history", "users.info", "conversations.members"})
        for f in self.dir.iterdir():
            self.assertNotIn("private words", f.read_text())

    def test_one_history_read_per_agent_a_tick_paging_through(self):
        p = self.poller()
        self.api.history = {"D1": [ev(T0 + i, "U1", "x") for i in range(1, 40)]}
        p.tick()
        self.assertEqual(sum(1 for c in self.api.calls if c[0] == "conversations.history"), 1)
        for _ in range(6):
            p.tick()
        self.assertEqual(len({x["id"] for x in self.log()}), 39)
        p.tick()
        p.tick()
        self.assertEqual(len(self.log()), 39, "nothing twice")

    def test_a_wait_is_tried_again_and_a_drop_isnt(self):
        p = self.poller()
        self.api.fail = {"users.info"}
        self.api.history = {"D1": [ev(T0 + 1, "U1", "hi")]}
        p.tick()
        self.assertEqual(self.log(), [])
        self.api.fail = set()
        p.tick()
        self.assertEqual(len(self.log()), 1)

    def test_live_hands_each_to_the_sink_once_and_retries_when_loom_doesnt_take_it(self):
        took, ok = [], {"v": False}

        def sink(agent, body):
            took.append(body)
            return ok["v"]
        router = adapter.Router(self.dir, common.ShadowLog(self.dir, "slack"), sink=sink, now=self.now)
        sock = adapter.Socket(router, self.agents, {"kit": "app"})
        sock.events.put(("kit", {"event": dict(ev(T0 + 1, "U1", "Thursday? <@U2>"), channel="D1", channel_type="im")}))
        sock.events.put(("kit", {"event": dict(ev(T0 + 2, "U1", "edited"), channel="D1", channel_type="im",
                                               subtype="message_changed")}))
        sock.tick()
        self.assertEqual(len(took), 1)
        b = took[0]
        self.assertEqual({k: b[k] for k in ("id", "agent", "venue", "place", "from", "kind", "label")},
                         {"id": f"kit:D1:{T0 + 1:.6f}", "agent": "kit", "venue": "slack", "place": "dm:U1",
                          "from": {"kind": "person", "slug": "robin"}, "kind": "message", "label": "DM with Robin"})
        self.assertEqual(b["text"], "Thursday? @Sam")
        sock.tick()
        self.assertEqual(len(took), 2)
        ok["v"] = True
        sock.tick()
        sock.tick()
        self.assertEqual(len(took), 3)
        self.assertEqual(self.log(), [], "live writes no shadow log")

    def test_a_frame_is_acked_and_queued_until_it_stops_accepting(self):
        router = adapter.Router(self.dir, common.ShadowLog(self.dir, "slack"), sink=lambda a, b: True)
        sock = adapter.Socket(router, self.agents, {"kit": "app"})

        class WS:
            sent = []

            def send(self, x):
                self.sent.append(json.loads(x))
        ws = WS()
        sock.handle_frame("kit", ws, json.dumps({"type": "events_api", "envelope_id": "e1", "payload": {"event": {}}}))
        sock.stop()
        sock.handle_frame("kit", ws, json.dumps({"type": "events_api", "envelope_id": "e2", "payload": {}}))
        self.assertEqual(ws.sent, [{"envelope_id": "e1"}])


if __name__ == "__main__":
    unittest.main()
