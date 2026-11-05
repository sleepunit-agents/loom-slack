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

    def poller(self, lookback_s=None):
        kw = {} if lookback_s is None else {"lookback_s": lookback_s}
        return adapter.Poller(adapter.Router(self.dir, common.ShadowLog(self.dir, "slack", now=self.now), now=self.now),
                              self.agents, now=self.now, **kw)

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
        self.assertLessEqual(methods, {"conversations.list", "conversations.history", "conversations.replies", "users.info",
                                       "conversations.members"})
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

    def reads(self):
        return [c[0] for c in self.api.calls if c[0] in ("conversations.history", "conversations.replies")]

    def test_a_reply_in_a_dm_thread_is_routed_though_history_leaves_it_out(self):
        """t-1745 round 1: a reply under a message already read (Slack's history has only the parent) never
        reached the shadow log, while the bridge's socket routed it."""
        p = self.poller()
        parent = ev(T0 + 1, "U1", "first")
        self.api.history = {"D1": [parent]}
        p.tick()
        p.tick()                                         # D1 and G1 read; the parent routed
        self.t[0] = T0 + 300
        self.api.replies = {("D1", parent["ts"]): [ev(T0 + 171, "U1", "a follow-up in the thread")]}
        for _ in range(6):
            p.tick()
        got = sorted((x["id"], x["place"]) for x in self.log())
        self.assertEqual(got, [(f"D1:{T0 + 1:.6f}", "dm:U1"), (f"D1:{T0 + 171:.6f}", "dm:U1")])
        for _ in range(4):
            p.tick()
        self.assertEqual(len(self.log()), 2, "nothing twice")
        for f in self.dir.iterdir():
            self.assertNotIn("follow-up", f.read_text())

    def test_a_reply_under_the_bots_own_post_and_in_a_group_dm(self):
        p = self.poller()
        mine, g = ev(T0 + 1, "B0", "my answer"), ev(T0 + 2, "U2", "group")
        self.api.history = {"D1": [mine], "G1": [g]}
        self.api.replies = {("D1", mine["ts"]): [ev(T0 + 5, "U1", "thanks")],
                            ("G1", g["ts"]): [ev(T0 + 6, "U1", "in the group's thread"), ev(T0 + 7, "B0", "mine")]}
        for _ in range(8):
            p.tick()
        got = sorted((x["id"], x["place"]) for x in self.log())
        self.assertEqual(got, [(f"D1:{T0 + 5:.6f}", "dm:U1"), (f"G1:{T0 + 2:.6f}", "group:G1"),
                               (f"G1:{T0 + 6:.6f}", "group:G1")])

    def test_a_thread_under_an_older_parent_is_found_by_the_lookback(self):
        p = self.poller()
        old = ev(T0 + 1, "U1", "old")
        self.api.history = {"D1": [old] + [ev(T0 + 10 + i, "U1", "x") for i in range(3)]}
        for _ in range(4):
            p.tick()
        self.assertEqual(len(self.log()), 4)
        self.t[0] = T0 + 7200                           # the cursor is well past the parent, inside the lookback
        self.api.replies = {("D1", old["ts"]): [ev(T0 + 7000, "U1", "late reply")]}
        for _ in range(6):
            p.tick()
        self.assertIn(f"D1:{T0 + 7000:.6f}", {x["id"] for x in self.log()})
        self.assertEqual(len(self.log()), 5, "the lookback offers nothing it had read already")

    def test_a_thread_older_than_the_lookback_is_missed_but_a_wider_lookback_catches_it(self):
        """t-1745 round 2: a parent older than lookback_s when its conversation is first read is never seen, so
        a late reply on it is missed for good — unless lookback_s is wide enough to still cover that parent."""
        old = ev(T0 + 1, "U1", "old")
        self.api.history = {"D1": [old] + [ev(T0 + 10 + i, "U1", "x") for i in range(3)]}
        p = self.poller(lookback_s=100)                 # too narrow to ever see a parent this old again
        for _ in range(4):
            p.tick()
        self.t[0] = T0 + 7200
        self.api.replies = {("D1", old["ts"]): [ev(T0 + 7000, "U1", "late reply")]}
        for _ in range(6):
            p.tick()
        self.assertNotIn(f"D1:{T0 + 7000:.6f}", {x["id"] for x in self.log()}, "missed: the parent fell out of range")

        self.t[0] = T0
        self.api.history = {"D1": [old] + [ev(T0 + 10 + i, "U1", "x") for i in range(3)]}
        self.api.replies = {}
        p = self.poller(lookback_s=86400)                # wide enough to still reach this parent
        for _ in range(4):
            p.tick()
        self.t[0] = T0 + 7200
        self.api.replies = {("D1", old["ts"]): [ev(T0 + 7000, "U1", "late reply")]}
        for _ in range(6):
            p.tick()
        self.assertIn(f"D1:{T0 + 7000:.6f}", {x["id"] for x in self.log()}, "found: lookback_s covers the parent")

    def test_replies_from_before_the_start_arent_offered_and_a_long_thread_pages(self):
        parent = ev(T0 - 100, "U1", "before")
        self.api.history = {"D1": [parent]}
        rs = [ev(T0 - 50, "U1", "before too")] + [ev(T0 + i, "U1", "r") for i in range(1, 21)]
        self.api.replies = {("D1", parent["ts"]): rs}
        p = self.poller()
        for _ in range(12):
            p.tick()
        ids = sorted(x["id"] for x in self.log())
        self.assertEqual(ids, sorted(f"D1:{T0 + i:.6f}" for i in range(1, 21)))

    def test_one_read_a_tick_and_never_two_thread_reads_running(self):
        p = self.poller()
        ps = [ev(T0 + i, "U1", "p") for i in range(1, 6)]
        self.api.history = {"D1": ps}
        self.api.replies = {("D1", m["ts"]): [ev(float(m["ts"]) + 100, "U1", "r")] for m in ps}
        seq = []
        for _ in range(16):
            before = len(self.reads())
            p.tick()
            got = self.reads()[before:]
            self.assertLessEqual(len(got), 1, "one Slack read per agent a tick")
            seq += got
        self.assertNotIn(("conversations.replies", "conversations.replies"), list(zip(seq, seq[1:])))
        self.assertEqual(len(self.log()), 10)

    def test_a_thread_slack_no_longer_has_is_let_go(self):
        p = self.poller()
        parent = ev(T0 + 1, "U1", "p")
        self.api.history = {"D1": [parent]}
        self.api.replies = {("D1", parent["ts"]): [ev(T0 + 2, "U1", "r")]}
        p.tick()                                         # D1: the parent, and its thread due
        self.api.history = {"D1": []}                    # deleted before its thread was read
        for _ in range(4):
            p.tick()
        self.assertEqual(p.router.state["threads"]["kit"], {})

    def test_live_routes_a_thread_reply_like_any_message(self):
        """The socket gets a reply in a DM thread as message.im, thread_ts and all: no gap once live."""
        took = []
        router = adapter.Router(self.dir, common.ShadowLog(self.dir, "slack"),
                                sink=lambda a, b: took.append(b) or True, now=self.now)
        sock = adapter.Socket(router, self.agents, {"kit": "app"})
        sock.events.put(("kit", {"event": dict(ev(T0 + 2, "U1", "in a thread"), channel="D1", channel_type="im",
                                               thread_ts=f"{T0 + 1:.6f}", parent_user_id="U1")}))
        sock.tick()
        self.assertEqual([(b["id"], b["place"]) for b in took], [(f"kit:D1:{T0 + 2:.6f}", "dm:U1")])

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
