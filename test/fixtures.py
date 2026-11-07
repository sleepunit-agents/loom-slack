"""A made-up workspace T1: the bot B0 (agent kit), robin (tier 3), sam (tier 2), kim (a guest, tier 1), a stranger."""
GATES = {"people": {"robin": {"name": "Robin", "tier": 3, "slack": {"T1": ["U1"]}},
                    "sam": {"name": "Sam", "tier": 2, "slack": {"T1": ["U2"]}},
                    "kim": {"name": "Kim", "tier": 1, "slack": {"T1": ["U3"]}}},
         "agents": {"bolt": {"name": "Bolt", "slack": {"T1": ["U9"]}}}}
USERS = {"U1": {"team_id": "T1"}, "U2": {"team_id": "T1"}, "U3": {"team_id": "T1"},
         "U4": {"team_id": "T1"}, "U5": {"team_id": "T2"}, "U6": {"team_id": "T1", "is_restricted": True},
         "U9": {"team_id": "T1", "is_bot": True}}


class FakeSlack:
    """The Web API as one bot: users, members, conversations, their history and their threads. Records every call.

    `history` is each conversation's timeline (no replies, as Slack's); `replies` is {(conversation, parent ts):
    [reply, …]}, and a parent in a history page carries `reply_count` and `latest_reply` from it, as Slack's does."""

    def __init__(self, convs=None, history=None, members=None, fail=(), replies=None):
        self.convs, self.history, self.members, self.fail = convs or [], history or {}, members or {}, set(fail)
        self.replies = replies or {}
        self.not_mpim = set()
        self.calls = []

    def _window(self, msgs, p):
        lo, hi, inc = float(p.get("oldest") or 0), p.get("latest"), p.get("inclusive") == "true"
        out = [m for m in msgs if (float(m["ts"]) >= lo if inc else float(m["ts"]) > lo)]
        if hi is not None:
            out = [m for m in out if (float(m["ts"]) <= float(hi) if inc else float(m["ts"]) < float(hi))]
        return out

    def _with_thread(self, cid, m):
        rs = self.replies.get((cid, m["ts"]))
        return dict(m, reply_count=len(rs), latest_reply=max(rs, key=lambda r: float(r["ts"]))["ts"],
                    thread_ts=m["ts"]) if rs else m

    @staticmethod
    def _page(msgs, p):
        lim = int(p["limit"])
        start = int(p.get("cursor") or 0)
        page = msgs[start:start + lim]
        more = start + lim < len(msgs)
        return {"messages": page, "has_more": more, "response_metadata": {"next_cursor": str(start + lim) if more else ""}}

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
            msgs = [self._with_thread(p["channel"], m) for m in self._window(self.history.get(p["channel"], []), p)]
            msgs.sort(key=lambda m: -float(m["ts"]))       # newest first, as Slack's
            return self._page(msgs, p)
        if method == "conversations.replies":
            cid, ts = p["channel"], p["ts"]
            parent = next((m for m in self.history.get(cid, []) if m["ts"] == ts), None)
            if parent is None:
                for (c, pts), rs in self.replies.items():
                    if c == cid and any(r["ts"] == ts for r in rs):
                        parent = next((m for m in self.history.get(cid, []) if m["ts"] == pts), None)
            if parent is None:
                raise routing.SlackError("conversations.replies: thread_not_found")
            rs = [dict(r, thread_ts=parent["ts"]) for r in self.replies.get((cid, parent["ts"]), [])]
            rs = sorted(self._window(rs, p), key=lambda m: float(m["ts"]))   # oldest first, as Slack's
            page = self._page(rs, p)
            page["messages"] = [self._with_thread(cid, parent)] + page["messages"]   # the parent heads every page
            return page
        if method == "conversations.info":
            return {"channel": {"id": p["channel"], "is_mpim": p["channel"] in self.members and p["channel"] not in self.not_mpim}}
        if method == "conversations.open":
            return {"channel": {"id": "D" + p["users"]}}
        if method == "chat.postMessage":
            return {"ts": f"9.{len(self.calls):06d}"}
        if method in ("reactions.add", "reactions.remove"):
            return {}
        raise AssertionError(method)


def ev(ts, user, text="", **kw):
    return dict({"type": "message", "user": user, "ts": f"{ts:.6f}", "text": text}, **kw)


def gates_argv(gates):
    """SLACK_GATES_COMMAND for a test: an argv that prints {"doc": gates}."""
    import json
    import sys
    return json.dumps([sys.executable, "-c", f"import json; print(json.dumps({{'doc': {gates!r}}}))"])
