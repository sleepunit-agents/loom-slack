"""What loom's deliver takes (t-1909): the time, the body's shape, what a refusal means, and the shadow's check.

loom's deliver refuses an `at` that isn't ISO-8601 UTC with at most 3 fraction digits and a `Z`. The shared helper
wrote 6 (`iso("1793828004.242749")` was `…24.242749Z`), so live Slack would have had every message refused; and a 400
was taken as "refused for good", the message marked done. These run real Slack ts values through the adapter's own
body, against loom's own `at` rule, copied below from loom's source. (loom-discord has the same file's tests.)"""
import importlib.machinery
import importlib.util
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from loomslack import adapter
from loomvenue import common
from test.fixtures import GATES, FakeSlack, ev

# loom src/backends/conversations.ts (main 720ead5), `ISO_UTC`, the rule parseTurn applies to a delivery's `at`
# (`at is an ISO-8601 UTC time (…Z)`). Copied here on its own, not imported from common, so this test checks
# common's copy too.
LOOM_ISO_UTC = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$"


def loom_takes(at):
    return isinstance(at, str) and "\n" not in at and re.match(LOOM_ISO_UTC, at) is not None


# Real Discord message timestamps, as the API sends them (the first is the message t-1745's live switch dropped).
DISCORD = [
    ("2026-11-06T16:34:58.881000+00:00", "2026-11-06T16:34:58.881Z"),
    ("2026-11-06T16:34:58+00:00", "2026-11-06T16:34:58.000Z"),
    ("2026-11-06T23:59:59.999999+00:00", "2026-11-06T23:59:59.999Z"),     # truncated: never into the next day
    ("2026-11-06T18:34:58.881000+02:00", "2026-11-06T16:34:58.881Z"),
]
# Real Slack ts values (seconds and 6 digits), from t-1745's shadow windows.
SLACK = [
    ("1793828004.242749", "2026-11-04T21:33:24.242Z"),
    ("1793723214.782659", "2026-11-03T16:26:54.782Z"),
    ("1793762896.550436", "2026-11-04T03:28:16.550Z"),
    ("1793828004.999999", "2026-11-04T21:33:24.999Z"),                    # truncated: never into the next second
]

BIN = Path(__file__).resolve().parent.parent / "bin" / "loom-slack"
loader = importlib.machinery.SourceFileLoader("loom_slack_bin_contract", str(BIN))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)

TS = "1793828004.242749"           # a real Slack ts (t-1745's shadow round 2)


class StaticDoc:
    def __init__(self, doc):
        self.d = doc

    def doc(self):
        return self.d


class Time(unittest.TestCase):
    def test_the_copy_in_common_is_looms_rule(self):
        self.assertEqual(common.ISO_UTC.pattern, LOOM_ISO_UTC.strip("^$"))

    def test_loom_refuses_what_discord_and_the_old_helper_sent(self):
        self.assertFalse(loom_takes("2026-11-06T16:34:58.881000+00:00"))   # Discord's raw timestamp
        self.assertFalse(loom_takes("2026-11-06T16:34:58.881000Z"))        # the old iso(): 6 digits
        self.assertFalse(loom_takes("2026-11-06T16:34:58.881Z\n"))

    def test_a_discord_timestamp(self):
        for raw, want in DISCORD:
            with self.subTest(raw=raw):
                self.assertEqual(common.iso(raw), want)
                self.assertTrue(loom_takes(common.iso(raw)))

    def test_a_slack_ts(self):
        for raw, want in SLACK:
            with self.subTest(raw=raw):
                self.assertEqual(common.iso(raw), want)
                self.assertTrue(loom_takes(common.iso(raw)))

    def test_epoch_seconds_as_numbers(self):
        self.assertEqual(common.iso(1793982898.881), "2026-11-06T16:34:58.881Z")
        self.assertEqual(common.iso(1793982898), "2026-11-06T16:34:58.000Z")
        self.assertTrue(loom_takes(common.iso(1793982898.123456)))

    def test_what_isnt_a_time(self):
        for bad in ("", "soon", "2026-11-06T16:34:58", None, True, float("nan")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                common.iso(bad)


class Verdict(unittest.TestCase):
    """loom's deliver answers (src/delivery.ts; loom-box delivery.mjs's own, passed through by core)."""

    def test_a_malformed_request_is_held(self):
        for status, err in [(400, "bad-input: at is an ISO-8601 UTC time (…Z)"),
                            (400, "bad-input: id is required: the source's own id for the message, 1-200 characters"),
                            (400, 'bad-input: a delivery has no key "extra" (it has id, agent, …)'),
                            (400, "bad-input: malformed JSON"),
                            (400, "at is an ISO-8601 time"),                  # the runtime's own parse
                            (415, "unsupported-media-type: POST /api takes application/json")]:
            with self.subTest(err=err):
                self.assertEqual(common.verdict(status, {"error": err}), common.HOLD)

    def test_a_refusal_about_the_message_itself_is_final(self):
        for status, err in [(403, "forbidden: discord doesn't declare venue x (its loom block's venues)"),
                            (403, "forbidden: kit isn't one of this household's agents"),
                            (403, "forbidden: from {kind: person} comes only from a venue adapter on its own venue"),
                            (409, "conflict: kit:1 was already delivered with a different body"),
                            (413, "oversized: a delivery is 256 KB at most"),
                            (400, "bad-input: text is required: the message, 1-40000 characters"),
                            (400, "text is 1-40000 characters")]:
            with self.subTest(err=err):
                self.assertEqual(common.verdict(status, {"error": err}), common.FINAL)

    def test_the_rest(self):
        self.assertEqual(common.verdict(200, {"conversation": "cv-0", "seq": 1, "body": "fed"}), common.DONE)
        for status in (0, 401, 429, 500, 502, 503, 504):
            self.assertEqual(common.verdict(status, {"error": "x"}), common.RETRY)


class Alerts(unittest.TestCase):
    def test_once_per_condition_detached_and_no_text(self):
        runs = []
        a = common.Alerter(["/bin/alert"], popen=lambda argv, **kw: runs.append((argv, kw)))
        self.assertTrue(a.alert("hold", "loom-slack", "bad", "held", status=400, error="bad-input: at"))
        self.assertFalse(a.alert("hold", "loom-slack", "bad", "held again"))
        self.assertEqual(runs[0][0], ["/bin/alert", "loom-slack", "--severity", "bad", "--summary", "held",
                                      "--fact", "status=400", "--fact", "error=bad-input: at"])
        self.assertTrue(runs[0][1]["start_new_session"])

    def test_unset_is_the_journal_only(self):
        self.assertFalse(common.Alerter(None).alert("hold", "loom-slack", "bad", "held"))

class FakeDeliverer:
    def __init__(self, status, ans):
        self.answer, self.bodies = (status, ans), []

    def deliver(self, body):
        self.bodies.append(body)
        return self.answer


class FakeAlerter:
    def __init__(self):
        self.said = []

    def alert(self, key, source, severity, summary, **facts):
        self.said.append((key, source, severity, facts))
        return True


class Sink(unittest.TestCase):
    def sink(self, status, ans):
        d, al = FakeDeliverer(status, ans), FakeAlerter()
        return cli.make_sink({"loom_url": "", "token_file": None, "alert": None}, deliverer=d, alerter=al), d, al

    def body(self):
        return common.delivery(f"D1:{TS}", "kit", "slack", "dm:U1", "DM with Robin", {"kind": "person", "slug": "robin"},
                               common.iso(TS), "hi")

    def test_a_malformed_refusal_holds_and_alerts(self):
        s, _, al = self.sink(400, {"error": "bad-input: at is an ISO-8601 UTC time (…Z)"})
        self.assertEqual(s("kit", self.body()), common.HOLD)
        self.assertEqual([(k, src, sev) for k, src, sev, _ in al.said], [("hold", "loom-slack", "bad")])
        self.assertEqual(al.said[0][3]["message"], f"D1:{TS}")

    def test_a_refusal_about_the_message_is_done_with_no_alert(self):
        s, _, al = self.sink(409, {"error": "conflict: kit:D1:1 was already delivered with a different body"})
        self.assertIs(s("kit", self.body()), True)
        self.assertEqual(al.said, [])

    def test_taken_and_no_answer(self):
        self.assertIs(self.sink(200, {"conversation": "cv-000000000000", "seq": 1, "body": "fed"})[0]("kit", self.body()), True)
        self.assertIs(self.sink(0, {"error": "URLError"})[0]("kit", self.body()), False)


class Intake(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="loom-slack-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.t = [float(TS)]
        self.api = FakeSlack(convs=[{"id": "D1", "is_im": True}])
        self.agents = {"kit": adapter.Agent("kit", self.api, "T1", "B0", StaticDoc(GATES))}
        self.runs = []
        self.alerts = common.Alerter(["/bin/alert"], popen=lambda argv, **kw: self.runs.append(argv))

    def now(self):
        return self.t[0]

    def log(self):
        p = self.dir / common.ShadowLog.NAME
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def socket(self, sink):
        router = adapter.Router(self.dir, common.ShadowLog(self.dir, "slack"), sink=sink, now=self.now, alerter=self.alerts)
        return adapter.Socket(router, self.agents, {"kit": "app"})

    def put(self, sock, ts, text="hi"):
        sock.events.put(("kit", {"event": {"type": "message", "user": "U1", "ts": ts, "text": text, "channel": "D1",
                                           "channel_type": "im"}}))

    def test_a_real_slack_message_meets_the_contract(self):
        took = []
        sock = self.socket(lambda a, b: took.append(b) or True)
        self.put(sock, TS)
        sock.tick()
        self.assertEqual(took[0]["at"], "2026-11-04T21:33:24.242Z")
        self.assertTrue(loom_takes(took[0]["at"]))
        self.assertEqual(common.contract_problems(took[0]), [])

    def test_a_held_message_isnt_lost_isnt_resent_never_times_out_and_is_tried_again_after_a_restart(self):
        took, answer = [], {"v": common.HOLD}

        def sink(agent, body):
            took.append(body["id"])
            return answer["v"]
        sock = self.socket(sink)
        self.put(sock, TS)
        sock.tick()
        self.assertEqual(took, [f"kit:D1:{TS}"])
        self.t[0] += 2 * adapter.WAIT_TTL_S      # long past the wait's limit
        sock.tick()
        sock.tick()
        self.assertEqual(len(took), 1, "held: not sent again by the same process")
        self.assertIn(f"kit|D1:{TS}", sock.router.state["waiting"], "and not dropped")
        self.assertTrue(sock.router.state["waiting"][f"kit|D1:{TS}"]["held"])
        sock.router.save()
        answer["v"] = True                      # the adapter is fixed and restarted
        again = self.socket(sink)
        again.tick()
        self.assertEqual(took, [f"kit:D1:{TS}"] * 2)
        self.assertEqual(again.router.state["waiting"], {})
        self.assertTrue(again.router.handled("kit", f"D1:{TS}"))

    def poller(self):
        return adapter.Poller(adapter.Router(self.dir, common.ShadowLog(self.dir, "slack", now=self.now), now=self.now,
                                             alerter=self.alerts), self.agents, now=self.now)

    def test_the_shadow_checks_each_body_against_the_contract(self):
        p = self.poller()
        self.api.history = {"D1": [ev(float(TS) + 1, "U1", "private words")]}
        for _ in range(3):
            p.tick()
        self.assertEqual([x["contract"] for x in self.log()], ["ok"])
        self.assertEqual(self.runs, [])
        self.assertNotIn("private words", (self.dir / common.ShadowLog.NAME).read_text())

    def test_a_shadow_mismatch_is_logged_and_alerted_once(self):
        p = self.poller()
        self.api.history = {"D1": [ev(float(TS) + 1, "U1", "a"), ev(float(TS) + 2, "U1", "b")]}
        real = common.iso
        try:
            common.iso = lambda t: "2026-11-04T21:33:24.242749Z"     # the old helper's 6 digits
            for _ in range(3):
                p.tick()
        finally:
            common.iso = real
        self.assertEqual([x["contract"] for x in self.log()], [["at is an ISO-8601 UTC time (…Z)"]] * 2)
        self.assertEqual([r[1:4] for r in self.runs], [["loom-slack", "--severity", "bad"]])


if __name__ == "__main__":
    unittest.main()
