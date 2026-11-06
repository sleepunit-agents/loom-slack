"""loom-slack's intake: each agent's bot, its DMs and group DMs, routed (routing.py) and logged (shadow) or handed to
loom's `deliver` (live).

**Two intakes, one router.**
  - Live: Socket Mode, as shuttle's bridge: Slack pushes `message.im` / `message.mpim` over a websocket the
    adapter dials out; each envelope is acked at once.
  - Shadow: the Web API, read slowly. Socket Mode can't run beside the old bridge: with two connections open,
    Slack sends each event to only one of them, so a shadow socket would take messages from the live bridge. So
    the shadow polls: one read per agent per `poll_s` (default 65 s, under the 1-a-minute limit Slack puts on some
    apps). It's late, and for a comparison by message id that doesn't matter.

**Thread replies (t-1825).** `conversations.history` leaves a reply in a thread out of the timeline; only
`conversations.replies` on its thread has it, while the socket gets it like any other message. So a history read
looks back THREAD_LOOKBACK_S past its cursor too, for parents only: one whose `latest_reply` is newer than what
was read of its thread is due. A tick reads a due thread (`conversations.replies`, from where it was left) instead
of a history page, at most every other tick, so history still gets at least half the reads and a tick still makes
one. A reply is routed like any message; a thread is marked read only up to the replies it offered.

A parent older than THREAD_LOOKBACK_S when the shadow first reads its conversation is never seen at all (the
history read's `oldest` never goes back further), so a reply landing on a thread that's been dormant longer than
that is missed for good — this is a shadow-only gap (t-1745 round 2): live's socket gets the reply regardless of
the thread's age, the same as the bridge's. `SLACK_THREAD_LOOKBACK_S` widens the window a dormant thread can still
be caught in; the cost is one history call that must walk back that far once per conversation (cheap while a
conversation's messages over that span stay under HISTORY_LIMIT, which is the common case for a DM).

A message is handled once per agent (a message id is its conversation and ts). A Slack check that fails ("wait")
is tried again on later ticks for up to WAIT_TTL_S, then dropped, as the old bridge does.

**Held (t-1909).** A delivery loom refuses as malformed (common.verdict's HOLD: a 400 about how this adapter wrote
it) isn't handled and isn't dropped: it waits with its event, isn't sent again by this process, never times out,
and the next process (an update restarts it) tries it again.

**The shadow's contract check.** In shadow, each message it would deliver is also built into the body live would
send and checked against loom's deliver contract (common.contract_problems), nothing sent: the shadow log's line
carries `contract` ("ok", or what loom would refuse), and a mismatch is a journal line and one alert.
"""
import time
from pathlib import Path

from loomvenue import common
from loomslack import routing

WAIT_TTL_S = 3600
HANDLED_KEEP_S = 2 * 86400
LIST_EVERY_S = 600
HISTORY_LIMIT = 15
SOURCE = "loom-slack"          # the alert command's source
THREAD_LOOKBACK_S = 2592000    # how far back a history read looks for a thread with new replies (30 days; t-1745
                                # round 2 found a dormant thread's parent 8 days old go unseen at 1 day's worth)


class Agent:
    """One agent's bot: its API, its workspace and bot user ids, and how to read its gates."""

    def __init__(self, agent, api, team, bot_user, gates_doc):
        self.agent, self.api, self.team, self.bot_user, self.gates_doc = agent, api, team, bot_user, gates_doc


class Router:
    """Routes a message for one agent: shadow logs it, live hands it to `sink(agent, body) -> bool`."""

    def __init__(self, data_dir, shadow, sink=None, now=time.time, alerter=None):
        self.state_file = Path(data_dir) / "state.json"
        self.shadow, self.sink, self.now = shadow, sink, now
        self.alerter = alerter or common.Alerter()
        self.held_now = set()       # "<agent>|<id>" held by this process: not sent again until it restarts
        self.state = common.load_json(self.state_file, {})
        for k in ("handled", "waiting", "cache", "cursors", "convs", "threads"):
            self.state.setdefault(k, {})

    def save(self):
        common.save_json(self.state_file, self.state)

    def handled(self, agent, mid):
        return agent in self.state["handled"].get(mid, {}).get("agents", [])

    def note_handled(self, agent, mid):
        h = self.state["handled"].setdefault(mid, {"at": self.now(), "agents": []})
        if agent not in h["agents"]:
            h["agents"].append(agent)

    def prune(self):
        cut = self.now() - HANDLED_KEEP_S
        self.state["handled"] = {k: v for k, v in self.state["handled"].items() if v.get("at", 0) >= cut}

    def route(self, a, cid, group, ev):
        """'routed' | 'handled' | 'wait' | 'drop' | 'retry' (live: loom didn't take it) | 'held' (live: loom refused
        how this adapter wrote it) for one message."""
        if not routing.routable(ev, a.bot_user):
            return "drop"
        mid = routing.msg_id(cid, ev["ts"])
        if self.handled(a.agent, mid):
            return "handled"
        gates = a.gates_doc.doc()
        if gates is None:
            return "wait"                    # fail closed: tried again once the gates answer
        cache = self.state["cache"].setdefault(a.agent, {})
        gate = routing.Gate(a.agent, a.api, a.team, a.bot_user, gates, cache, now=self.now)
        verdict, frm = gate.admit(cid, group, ev.get("user"))
        if verdict != "ok":
            return verdict
        place = routing.place_of(cid, ev.get("user"), group)
        body = common.delivery(mid, a.agent, "slack", place, routing.label_of(group, gate.people.get(ev.get("user"))),
                               frm, common.iso(ev["ts"]), routing.text_of(ev, gate.people))
        if self.sink is None:
            self.shadow.routed(mid, a.agent, place, contract=self.check(a.agent, mid, body))
            self.note_handled(a.agent, mid)
            return "routed"
        key = f"{a.agent}|{mid}"
        if key in self.held_now:
            return "held"
        r = self.sink(a.agent, body)
        if r == common.HOLD:
            self.held_now.add(key)
            return "held"
        if not r:
            return "retry"
        self.note_handled(a.agent, mid)
        return "routed"

    def check(self, agent, mid, body):
        """Shadow: the body live would send, checked against loom's deliver contract; nothing is sent. Answers the
        problems ([] when loom would take its shape); a mismatch is said once a process."""
        problems = common.contract_problems(body)
        if problems:
            common.log(f"{agent}: CONTRACT MISMATCH {mid}: {'; '.join(problems)[:300]}")
            self.alerter.alert("contract", SOURCE, "bad",
                               "loom-slack shadow: a delivery it would make doesn't match loom's deliver contract",
                               message=mid, agent=agent, problems="; ".join(problems))
        return problems

    def offer(self, a, cid, group, ev):
        """Route one message now; a "wait" or a "retry" is kept for later (its id and conversation only, in
        shadow; in live its event too, since nothing else holds it)."""
        r = self.route(a, cid, group, ev)
        if r in ("wait", "retry", "held"):
            key = f"{a.agent}|{routing.msg_id(cid, ev['ts'])}"
            w = self.state["waiting"].setdefault(key, {"since": self.now()})
            w.update({"agent": a.agent, "cid": cid, "group": group, "ev": slim(ev, keep_text=self.sink is not None)})
            if r == "held":
                w["held"] = True
        return r

    def retry_waiting(self, agents):
        for key, w in list(self.state["waiting"].items()):
            if key in self.held_now:
                continue                      # held: tried again by the next process, never timed out
            a = agents.get(w.get("agent"))
            if a is None or (not w.get("held") and self.now() - w.get("since", 0) > WAIT_TTL_S):
                common.log(f"{w.get('agent')}: a message waited {WAIT_TTL_S}s for Slack or loom; dropped")
                self.state["waiting"].pop(key)
                continue
            r = self.route(a, w["cid"], w["group"], w["ev"])
            if r == "held":
                w["held"] = True
            elif r not in ("wait", "retry"):
                self.state["waiting"].pop(key)


def slim(ev, keep_text):
    """What's kept of a waiting message: who and when; its text only in live mode (it's that message's only copy
    until loom takes it), never in shadow."""
    out = {k: ev.get(k) for k in ("type", "subtype", "user", "ts", "bot_id") if ev.get(k) is not None}
    if keep_text:
        out["text"] = ev.get("text")
        out["files"] = [{"name": f.get("name")} for f in ev.get("files", []) or []]
    return out


class Poller:
    """The shadow intake: one read per agent per tick, in turn over its conversations (`conversations.history`), or
    a thread with replies not read yet (`conversations.replies`), every other tick at most."""

    def __init__(self, router, agents, now=time.time, lookback_s=THREAD_LOOKBACK_S):
        self.router, self.agents, self.now, self.lookback_s = router, agents, now, lookback_s
        self.started = f"{now():.6f}"
        self.last_replies = {}

    def conversations(self, a):
        convs = self.router.state["convs"].setdefault(a.agent, {"at": 0, "list": [], "next": 0})
        if self.now() - convs["at"] >= LIST_EVERY_S:
            out, cursor = [], None
            while True:
                params = {"types": "im,mpim", "exclude_archived": "true", "limit": "200"}
                if cursor:
                    params["cursor"] = cursor
                d = a.api.call("conversations.list", **params)
                out += [{"id": c["id"], "group": bool(c.get("is_mpim"))} for c in d.get("channels", [])]
                cursor = (d.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
            convs.update({"at": self.now(), "list": out})
        return convs

    def tick(self):
        self.router.prune()
        self.router.retry_waiting(self.agents)
        n = 0
        for name, a in sorted(self.agents.items()):
            try:
                n += self.agent_tick(a)
            except routing.RateLimited as e:
                common.log(f"{name}: {e}")
            except (routing.SlackError, OSError, ValueError, KeyError) as e:
                common.log(f"{name}: tick: {type(e).__name__}")
        self.router.save()
        return n

    def threads(self, a):
        """This agent's threads: {"<conversation>|<parent ts>": {cid, group, parent, seen, at, due, page?}};
        `seen` is the newest reply offered (or where reading starts), `due` that Slack has a newer one."""
        th = self.router.state["threads"].setdefault(a.agent, {})
        cut = self.now() - 2 * self.lookback_s
        for k in [k for k, t in th.items() if not t.get("due") and t.get("at", 0) < cut]:
            th.pop(k)
        return th

    def agent_tick(self, a):
        th = self.threads(a)
        due = sorted((t for t in th.values() if t.get("due")), key=lambda t: t.get("at", 0))
        if due and not self.last_replies.get(a.agent):
            self.last_replies[a.agent] = True
            return self.replies_tick(a, th, due[0])
        self.last_replies[a.agent] = False
        return self.history_tick(a, th)

    def history_tick(self, a, th):
        convs = self.conversations(a)
        if not convs["list"]:
            return 0
        cursors = self.router.state["cursors"].setdefault(a.agent, {})
        i = convs.get("next", 0) % len(convs["list"])
        c = convs["list"][i]
        cur = cursors.setdefault(c["id"], {"oldest": self.started})
        cur.setdefault("since", cur["oldest"])           # nothing from before this conversation was first read
        look = min(float(cur["oldest"]), self.now() - self.lookback_s)
        params = {"channel": c["id"], "oldest": f"{look:.6f}", "inclusive": "false", "limit": str(HISTORY_LIMIT)}
        if cur.get("page"):
            params["cursor"] = cur["page"]
        d = a.api.call("conversations.history", **params)
        msgs = sorted(d.get("messages", []), key=lambda m: float(m.get("ts", 0)))
        n = 0
        for m in msgs:
            self.note_thread(th, c, cur, m)
            if float(m["ts"]) <= float(cur["oldest"]):
                continue                      # read on an earlier lap: looked at for its thread only
            if self.router.offer(a, c["id"], c["group"], m) == "routed":
                n += 1
            cur["newest"] = max(float(cur.get("newest", 0)), float(m["ts"]))
        nxt = (d.get("response_metadata") or {}).get("next_cursor") if d.get("has_more") else None
        if nxt:
            cur["page"] = nxt                 # the same conversation next tick, further back
        else:
            cur.pop("page", None)
            if cur.get("newest"):
                cur["oldest"] = f"{cur.pop('newest'):.6f}"
            convs["next"] = i + 1
        return n

    def note_thread(self, th, c, cur, m):
        """A parent in a history page: due when Slack's newest reply is past what's been read of its thread."""
        latest = m.get("latest_reply")
        if not (m.get("reply_count") and latest and m.get("ts")):
            return
        key = f"{c['id']}|{m['ts']}"
        t = th.get(key)
        if t is None:
            start = max(float(m["ts"]), float(cur["since"]))
            t = th[key] = {"cid": c["id"], "group": c["group"], "parent": m["ts"], "seen": f"{start:.6f}",
                           "at": float(m["ts"]), "due": False}
        if float(latest) > float(t["seen"]):
            t["due"] = True

    def replies_tick(self, a, th, t):
        key = f"{t['cid']}|{t['parent']}"
        params = {"channel": t["cid"], "ts": t["parent"], "oldest": t["seen"], "inclusive": "false",
                  "limit": str(HISTORY_LIMIT)}
        if t.get("page"):
            params["cursor"] = t["page"]
        try:
            d = a.api.call("conversations.replies", **params)
        except routing.SlackError as e:
            if e.error in ("thread_not_found", "channel_not_found", "message_not_found"):
                th.pop(key, None)             # the thread is gone: nothing left in it to read
                return 0
            raise
        msgs = sorted(d.get("messages", []), key=lambda m: float(m.get("ts", 0)))
        n = 0
        for m in msgs:
            if m.get("ts") == t["parent"] or float(m.get("ts", 0)) <= float(t["seen"]):
                continue                      # the parent comes with every page; it's history's to offer
            if self.router.offer(a, t["cid"], t["group"], m) == "routed":
                n += 1
            t["newest"] = max(float(t.get("newest", 0)), float(m["ts"]))
        nxt = (d.get("response_metadata") or {}).get("next_cursor") if d.get("has_more") else None
        if nxt:
            t["page"] = nxt                   # this thread again, on its next turn
        else:
            t.pop("page", None)
            if t.get("newest"):
                t["seen"] = f"{t.pop('newest'):.6f}"
            t["due"] = False                  # a newer reply shows on the parent's next history read
        return n


class Socket:
    """The live intake: Socket Mode (shuttle's ws_loop and handle_frame, ported). Needs websocket-client."""

    def __init__(self, router, agents, app_tokens):
        import queue
        import threading
        self.router, self.agents, self.app_tokens = router, agents, app_tokens
        self.events = queue.Queue()
        self.accepting = True
        self.lock = threading.Lock()

    def handle_frame(self, agent, ws, raw):
        import json
        try:
            d = json.loads(raw)
        except ValueError:
            return
        t = d.get("type")
        if t == "hello":
            common.log(f"{agent}: socket mode: hello")
            return
        if t == "disconnect":
            ws.close()
            return
        with self.lock:
            if not self.accepting:
                return                        # unacked: Slack sends it again to the next connection
            if d.get("envelope_id"):
                ws.send(json.dumps({"envelope_id": d["envelope_id"]}))
            if t == "events_api":
                self.events.put((agent, d.get("payload", {})))

    def loop(self, agent):
        import websocket  # websocket-client: only the live socket needs it
        backoff = 5
        while True:
            try:
                url = routing.Slack(self.app_tokens[agent]).call("apps.connections.open")["url"]
            except Exception as e:  # noqa: BLE001
                common.log(f"{agent}: apps.connections.open failed ({type(e).__name__}); retry in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            app = websocket.WebSocketApp(url, on_message=lambda ws, raw: self.handle_frame(agent, ws, raw),
                                         on_error=lambda ws, err: common.log(f"{agent}: socket error {type(err).__name__}"))
            app.run_forever(ping_interval=30, ping_timeout=10)
            common.log(f"{agent}: socket closed; reconnecting")
            backoff = 5
            time.sleep(2)

    def start(self):
        import threading
        for agent in self.agents:
            threading.Thread(target=self.loop, args=(agent,), daemon=True).start()

    def drain(self):
        import queue
        n = 0
        while True:
            try:
                agent, payload = self.events.get_nowait()
            except queue.Empty:
                return n
            ev = payload.get("event", {})
            kind = ev.get("channel_type")
            a = self.agents.get(agent)
            if a is None or ev.get("type") != "message" or kind not in ("im", "mpim") or not ev.get("channel"):
                continue
            if self.router.offer(a, ev["channel"], kind == "mpim", ev) == "routed":
                n += 1

    def tick(self):
        self.router.prune()
        self.router.retry_waiting(self.agents)
        n = self.drain()
        self.router.save()
        return n

    def stop(self):
        with self.lock:
            self.accepting = False
        self.drain()
        self.router.save()
