"""The gate (bridges.md "Routing", rule 1; shuttle's spawn_gate and group_gate): a DM is the bot's agent's, and
only someone Slack and the gates both let through reaches it."""
import unittest

from loomslack import routing
from test.fixtures import GATES, FakeSlack, ev


def gate(api=None, gates=GATES):
    return routing.Gate("kit", api or FakeSlack(), "T1", "B0", gates, {})


class Gate(unittest.TestCase):
    def test_a_dm_from_tier_two_and_up_is_delivered_from_their_card(self):
        self.assertEqual(gate().admit("D1", False, "U1"), ("ok", {"kind": "person", "slug": "robin"}))
        self.assertEqual(gate().admit("D2", False, "U2"), ("ok", {"kind": "person", "slug": "sam"}))

    def test_a_guest_a_stranger_another_workspace_or_a_guest_account_is_left_on_read(self):
        for uid in ("U3", "U4", "U5", "U6", "U9", None):
            self.assertEqual(gate().admit("D1", False, uid)[0], "drop", uid)

    def test_slack_not_answering_is_wait_not_drop(self):
        self.assertEqual(gate(FakeSlack(fail={"users.info"})).admit("D1", False, "U1")[0], "wait")
        self.assertEqual(gate(FakeSlack(fail={"conversations.members"})).admit("G1", True, "U1")[0], "wait")

    def test_a_group_needs_every_member_through_and_two_of_them(self):
        api = FakeSlack(members={"G1": ["B0", "U1", "U2"], "G2": ["B0", "U1", "U4"], "G3": ["B0", "U1"]})
        self.assertEqual(gate(api).admit("G1", True, "U2"), ("ok", {"kind": "person", "slug": "sam"}))
        self.assertEqual(gate(api).admit("G2", True, "U1")[0], "drop")
        self.assertEqual(gate(api).admit("G3", True, "U1")[0], "drop")

    def test_a_non_d_conversation_is_gated_as_a_room_even_unflagged(self):
        api = FakeSlack(members={"C1": ["B0", "U1"]})
        self.assertEqual(gate(api).admit("C1", False, "U1")[0], "drop")

    def test_slacks_answers_are_cached(self):
        api = FakeSlack()
        g = gate(api)
        g.admit("D1", False, "U1")
        g.admit("D1", False, "U1")
        self.assertEqual([c[0] for c in api.calls], ["users.info"])

    def test_an_id_two_people_claim_is_nobodys(self):
        """Independent read (t-1745): a slip that puts Sam's id under the owner's card too must never make Sam's message
        the owner's: the id is nobody's, and its messages are left on read."""
        import copy
        g = copy.deepcopy(GATES)
        g["people"]["robin"]["slack"] = {"T1": ["U1", "U2"]}     # robin (tier 3) also claims sam's U2
        self.assertEqual(gate(gates=g).admit("D2", False, "U2")[0], "drop")
        self.assertEqual(gate(gates=g).admit("D1", False, "U1"), ("ok", {"kind": "person", "slug": "robin"}))

    def test_no_gates_no_one(self):
        self.assertEqual(gate(gates=None).admit("D1", False, "U1")[0], "drop")

    def test_routable_skips_its_own_bots_edits_and_joins(self):
        self.assertTrue(routing.routable(ev(1, "U1"), "B0"))
        self.assertTrue(routing.routable(ev(1, "U1", subtype="file_share"), "B0"))
        for e in (ev(1, "B0"), ev(1, "U1", bot_id="X"), ev(1, "U1", subtype="message_changed"),
                  ev(1, "U1", subtype="channel_join"), {"type": "message", "ts": "1.0"}):
            self.assertFalse(routing.routable(e, "B0"))

    def test_places_ids_and_text(self):
        self.assertEqual(routing.place_of("D1", "U1", False), "dm:U1")
        self.assertEqual(routing.place_of("G1", "U1", True), "group:G1")
        self.assertEqual(routing.msg_id("D1", "1.000002"), "D1:1.000002")
        people = gate().people
        self.assertEqual(routing.clean_text("hi <@U2> and <@U7>, see <https://x.y|this>", people),
                         "hi @Sam and @someone, see this (https://x.y)")


if __name__ == "__main__":
    unittest.main()
