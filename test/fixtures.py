"""A made-up workspace T1: the bot B0 (agent kit), robin (tier 3), sam (tier 2), kim (a guest, tier 1), a stranger."""
GATES = {"people": {"robin": {"name": "Robin", "tier": 3, "slack": {"T1": ["U1"]}},
                    "sam": {"name": "Sam", "tier": 2, "slack": {"T1": ["U2"]}},
                    "kim": {"name": "Kim", "tier": 1, "slack": {"T1": ["U3"]}}},
         "agents": {"bolt": {"name": "Bolt", "slack": {"T1": ["U9"]}}}}
USERS = {"U1": {"team_id": "T1"}, "U2": {"team_id": "T1"}, "U3": {"team_id": "T1"},
         "U4": {"team_id": "T1"}, "U5": {"team_id": "T2"}, "U6": {"team_id": "T1", "is_restricted": True},
         "U9": {"team_id": "T1", "is_bot": True}}


class FakeSlack:
    """The Web API as one bot: users, members, conversations and their history. Records every call."""

    def __init__(self, convs=None, history=None, members=None, fail=()):
        self.convs, self.history, self.members, self.fail = convs or [], history or {}, members or {}, set(fail)
        self.calls = []

    def call(self, method, **p):
        from loomslack import routing
        self.calls.append((method, p))
        if method in self.fail:
            raise routing.SlackError(f"{method}: fatal_error")
        if method == "users.info":
            return {"user": USERS.get(p["user"], {"team_id": "T1"})}
        if method == "conversations.members":
            return {"members": self.members.get(p["channel"], [])}
        if method == "conversations.list":
            return {"channels": self.convs}
        if method == "conversations.history":
            msgs = [m for m in self.history.get(p["channel"], []) if float(m["ts"]) > float(p["oldest"])]
            msgs.sort(key=lambda m: -float(m["ts"]))
            lim = int(p["limit"])
            start = int(p.get("cursor") or 0)
            page = msgs[start:start + lim]
            more = start + lim < len(msgs)
            return {"messages": page, "has_more": more, "response_metadata": {"next_cursor": str(start + lim) if more else ""}}
        if method == "conversations.open":
            return {"channel": {"id": "D" + p["users"]}}
        if method == "chat.postMessage":
            return {"ts": f"9.{len(self.calls):06d}"}
        if method in ("reactions.add", "reactions.remove"):
            return {}
        raise AssertionError(method)


def ev(ts, user, text="", **kw):
    return dict({"type": "message", "user": user, "ts": f"{ts:.6f}", "text": text}, **kw)
