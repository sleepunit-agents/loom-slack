"""What the runtime asks of a Slack place before a body starts there (`send --describe`, t-1745): who is in it (their
slugs; the runtime takes their tiers, restrictions and cards from its own gates), and the notes the agent's policy maps
to its own brief files, the old bridge's ext `dm_extra` (a tier-2 person's project and field scopes) and its group
briefs (the lowest tier present governs; the owner present). It fails closed: anyone the gates don't admit at tier 2
and up, or the gates or Slack not answering, is no body."""
import copy
import contextlib
import io
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from loomvenue import common
from test.fixtures import GATES, FakeSlack, gates_argv
from test.test_send import ENV, FakeSecrets, cli

ROOM = copy.deepcopy(GATES)
ROOM["people"].update({
    "lee": {"name": "Lee", "tier": 2, "slack": {"T1": ["U7"]}, "projects": ["pond", "Bad Name!"],
            "field": {"pond": ["et-livonia", "boyle"], "toad": []}},
    "ash": {"name": "Ash", "tier": 2, "slack": {"T1": ["U8"]}},
    "max": {"name": "Max", "tier": 3, "slack": {"T1": ["U10"]}},
    "far": {"name": "Far", "tier": 2, "slack": {"T2": ["U11"]}},         # another workspace's id: not this bot's
    "gus": {"name": "Gus", "tier": 2, "slack": {"T1": ["U6"]}},          # a Slack guest account (users.info)
})
# "Bad Name!" isn't a note name the runtime takes (policy.mjs NOTE): it's left out, not passed on.
ROOM["people"]["robin"]["projects"] = ["pond"]          # tier 3: scopes are for tier 2 only


class Describe(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="loom-slack-d-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.api = FakeSlack(members={"G1": ["B0", "U7", "U8"], "G2": ["B0", "U1", "U7"], "G3": ["B0", "U7", "U3"],
                                      "G4": ["B0", "U7"], "G5": ["B0", "U1", "U10"], "G6": ["B0", "U7", "U6"]})

    def describe(self, place, gates=ROOM, secrets=None):
        cfg = cli.config({"SLACK_MODE": "shadow", "SLACK_BOTS": '{"kit": "kit-slack.env"}', "SLACK_DATA": str(self.dir),
                          "SLACK_GATES_COMMAND": gates_argv(gates) if gates is not None else '["false"]'})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.send(cfg, ["--place", place, "--agent", "kit", "--describe"], secrets=secrets or FakeSecrets(ENV),
                     api_for=lambda t: self.api)
        return json.loads(out.getvalue())

    def test_a_tier_2_dm_names_its_person_and_their_project_and_field_notes(self):
        d = self.describe("dm:U7")
        self.assertEqual(d, {"ok": True, "guest": False, "peer": False, "venue": "U7", "readers": [], "members": ["lee"],
                             "notes": ["field-pond", "project-pond"]})
        self.assertEqual([m for m, _ in self.api.calls], ["users.info"], "Slack's own check of the person, as routing's")

    def test_the_owner_and_an_unscoped_colleague_get_no_notes(self):
        self.assertEqual(self.describe("dm:U1")["notes"], [], "tier 3 has no scope notes, whatever its gates say")
        self.assertEqual(self.describe("dm:U8")["notes"], [])
        self.assertEqual(self.describe("dm:U8")["members"], ["ash"])

    def test_a_dm_with_anyone_the_gates_dont_admit_is_no_body(self):
        # a guest (tier 1), a stranger, an agent's bot, a person under another workspace's id, a Slack guest account
        for uid in ("U3", "U4", "U9", "U11", "U6"):
            with self.subTest(uid=uid):
                self.assertFalse(self.describe(f"dm:{uid}")["ok"])

    def test_a_group_dm_names_everyone_in_it_and_the_lowest_tier_governs(self):
        d = self.describe("group:G1")
        self.assertEqual(d, {"ok": True, "guest": False, "peer": False, "venue": "G1", "readers": [],
                             "members": ["ash", "lee"], "notes": ["group-tier-2"]},
                         "the whole answer; members sorted by name, not the bot; no scope notes in a group")
        d = self.describe("group:G2")
        self.assertEqual(d["members"], ["lee", "robin"])
        self.assertEqual(d["notes"], ["group-owner-present", "group-tier-2"])
        d = self.describe("group:G5")
        self.assertEqual(d["members"], ["max", "robin"])
        self.assertEqual(d["notes"], ["group-tier-3"], "all at the owner's tier: no owner-present note")

    def test_a_group_with_a_guest_or_one_person_or_slack_not_answering_is_no_body(self):
        self.assertFalse(self.describe("group:G3")["ok"], "a guest in it")
        self.assertFalse(self.describe("group:G6")["ok"], "a Slack guest account in it, though the gates have them")
        self.assertFalse(self.describe("group:G4")["ok"], "one person isn't a group")
        self.api.fail.add("conversations.members")
        self.assertFalse(self.describe("group:G1")["ok"])

    def test_a_group_reads_its_members_from_the_intakes_cache_while_fresh(self):
        common.save_json(self.dir / "state.json", {"cache": {"kit": {"members": {"G9": {"at": time.time(), "uids": ["B0", "U7", "U1"]}}}}})
        self.assertEqual(self.describe("group:G9")["members"], ["lee", "robin"])
        self.assertNotIn("conversations.members", [m for m, _ in self.api.calls], "the fresh cache answers who's in it")
        common.save_json(self.dir / "state.json", {"cache": {"kit": {"members": {"G1": {"at": 0, "uids": ["B0", "U7", "U1"]}}}}})
        self.assertEqual(self.describe("group:G1")["members"], ["ash", "lee"], "stale: Slack is asked")
        self.assertIn("conversations.members", [m for m, _ in self.api.calls])

    def test_a_dm_or_group_whose_person_the_runtime_couldnt_name_or_slack_wont_say_is_no_body(self):
        odd = copy.deepcopy(ROOM)
        odd["people"]["Not_A_Slug"] = {"name": "Odd", "tier": 2, "slack": {"T1": ["U12"]}}
        self.assertFalse(self.describe("dm:U12", gates=odd)["ok"], "a gates slug the runtime can't name")
        self.api.members["G7"] = ["B0", "U7", "U12"]
        self.assertFalse(self.describe("group:G7", gates=odd)["ok"])
        self.api.fail.add("users.info")
        self.assertFalse(self.describe("dm:U8")["ok"], "Slack not answering users.info")

    def test_fail_closed_without_the_gates_or_the_bot(self):
        self.assertEqual(self.describe("dm:U7", gates=None), {"ok": False, "reason": "kit's gates can't be read"})
        self.assertFalse(self.describe("dm:U7", secrets=FakeSecrets(ok=False))["ok"])


if __name__ == "__main__":
    unittest.main()
