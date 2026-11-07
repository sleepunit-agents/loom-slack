"""Which agent a Slack message is for, and whether it may reach it (bridges.md, "Routing"; ported from shuttle's
adapters/slack.py).

Slack is DMs and group DMs (an app's bot scopes: im:*, mpim:*): a DM is for the agent whose bot received it
(rule 1), and so is a group DM its bot is in. Who may reach the agent is the gates':

  - a 1:1 DM: the sender passes Slack's own check (users.info: not a Slack Connect stranger, this workspace, not a
    guest account, not a bot or deleted) and the gates have them at tier 2 or up;
  - a group DM: every other member passes that, and there are at least two of them.

A Slack check that fails (users.info or conversations.members down) is "wait", not "drop": the message is tried
again for a while. Everyone else is left on read.
"""
import re
import time

from loomvenue import common

USER_CACHE_S = 3600
GROUP_MEMBERS_TTL_S = 3600
MENTION = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>")
LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")


class Slack:
    """Slack's Web API as one bot (always POST: Slack rejects a bare GET)."""

    def __init__(self, token, opener=None):
        import urllib.request
        self.token, self.opener = token, opener or urllib.request.urlopen

    def call(self, method, **params):
        import json
        import urllib.error
        import urllib.parse
        import urllib.request
        req = urllib.request.Request(f"https://slack.com/api/{method}", data=urllib.parse.urlencode(params).encode(),
                                     method="POST", headers={"Authorization": f"Bearer {self.token}",
                                                             "User-Agent": "loom-slack/0.1",
                                                             "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.opener(req, timeout=20) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited(float(e.headers.get("Retry-After") or 60)) from None
            raise
        if not d.get("ok"):
            if d.get("error") == "ratelimited":
                raise RateLimited(60.0)
            raise SlackError(f"{method}: {d.get('error')}", d.get("error"))
        return d


class SlackError(Exception):
    """Slack answered ok: false; `error` is its code (channel_not_found, not_in_channel, fatal_error, …)."""

    def __init__(self, message, error=None):
        super().__init__(message)
        self.error = error if error is not None else str(message).rpartition(": ")[2]


class RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__(f"rate limited for {retry_after:g}s")
        self.retry_after = retry_after


_CLAIMED_TWICE = set()


def people_of(gates, team, agent="?"):
    """{user id: (slug, record)} for this workspace, from the gates' people, without any id two people claim: who wrote
    can't be told then, and a message must never come from the wrong card (one of them the owner's). Each such id is
    logged once a process; its messages are left on read until the gates are fixed."""
    out = common.index(gates, "slack", team, sections=("people",))
    claims = {}
    for slug, rec in ((gates or {}).get("people") or {}).items():
        if not isinstance(slug, str) or slug.startswith("_") or not isinstance(rec, dict):
            continue
        ids = rec.get("slack")
        ids = ids.get(team) if isinstance(ids, dict) else ids
        for uid in [ids] if isinstance(ids, (str, int)) and not isinstance(ids, bool) else ids if isinstance(ids, list) else []:
            if isinstance(uid, (str, int)) and not isinstance(uid, bool):
                claims.setdefault(str(uid), set()).add(slug)
    for uid, slugs in claims.items():
        if len(slugs) > 1:
            out.pop(uid, None)
            if (agent, uid) not in _CLAIMED_TWICE:
                _CLAIMED_TWICE.add((agent, uid))
                common.log(f"{agent}: a Slack id is in the gates under {len(slugs)} people; it's nobody's until that's fixed")
    return out


class Gate:
    """One agent's gate on Slack: its bot, its workspace, its gates document, and Slack's own answers (cached)."""

    def __init__(self, agent, api, team, bot_user, gates, cache, now=time.time):
        self.agent, self.api, self.team, self.bot_user, self.now = agent, api, team, bot_user, now
        self.people = people_of(gates, team, agent) if isinstance(gates, dict) else {}
        self.cache = cache            # persisted: {"users": {uid: …}, "members": {cid: …}}
        self.cache.setdefault("users", {})
        self.cache.setdefault("members", {})

    def user_verdict(self, uid):
        """(ok, reason): Slack's own three-way check of a sender."""
        c = self.cache["users"].get(uid)
        if c and self.now() - c.get("at", 0) < USER_CACHE_S:
            return c["ok"], c["reason"]
        try:
            u = self.api.call("users.info", user=uid)["user"]
        except (SlackError, OSError, KeyError, ValueError) as e:
            return False, f"users.info failed: {type(e).__name__}"
        reason = None
        if u.get("is_stranger"):
            reason = "Slack Connect stranger"
        elif u.get("team_id") != self.team:
            reason = "another workspace"
        elif u.get("is_restricted") or u.get("is_ultra_restricted"):
            reason = "guest account"
        elif u.get("is_bot") or u.get("deleted"):
            reason = "bot or deleted"
        self.cache["users"][uid] = {"ok": reason is None, "reason": reason, "at": self.now()}
        return reason is None, reason

    def person_gate(self, uid):
        """("ok", (slug, rec)) | ("wait", why) | ("drop", why)."""
        if not uid:
            return "drop", "no sender"
        ok, reason = self.user_verdict(uid)
        if not ok:
            return ("wait" if str(reason).startswith("users.info failed") else "drop"), reason
        hit = self.people.get(uid)
        if not hit or not common.may_wake(hit[1])[0]:
            return "drop", "not in the gates at tier 2+"
        return "ok", hit

    def members(self, cid):
        hit = self.cache["members"].get(cid)
        if hit and self.now() - hit.get("at", 0) < GROUP_MEMBERS_TTL_S:
            return hit["uids"]
        try:
            uids = self.api.call("conversations.members", channel=cid).get("members") or []
        except (SlackError, OSError, ValueError):
            return None
        self.cache["members"][cid] = {"at": self.now(), "uids": uids}
        return uids

    def member_names(self, cid):
        """(names, total): a group DM's people but the bot, as the members this gate last read (`admit` reads or
        refreshes them just before), their gates names sorted, and how many there are, named or not."""
        uids = [u for u in ((self.cache["members"].get(cid) or {}).get("uids") or []) if u != self.bot_user]
        names = sorted(str(p[1]["name"]) for p in (self.people.get(u) for u in uids) if p and p[1].get("name"))
        return names, len(uids)

    def group_gate(self, cid):
        uids = self.members(cid)
        if uids is None:
            return "wait", "conversations.members failed"
        n = 0
        for uid in uids:
            if uid == self.bot_user:
                continue
            verdict, why = self.person_gate(uid)
            if verdict != "ok":
                return verdict, f"a member: {why if verdict == 'wait' else 'may not'}"
            if not isinstance(why[1].get("tier"), int):
                return "drop", "a member has no tier"
            n += 1
        if n < 2:
            return "drop", "not a group: fewer than two other members"
        return "ok", None

    def admit(self, cid, group, uid):
        """("ok", from) | ("wait", why) | ("drop", why) for a message in conversation `cid` from `uid`."""
        if is_group(cid, group):
            verdict, why = self.group_gate(cid)
            if verdict != "ok":
                return verdict, why
            return "ok", common.from_of(self.people.get(uid))
        verdict, hit = self.person_gate(uid)
        if verdict != "ok":
            return verdict, hit
        return "ok", common.from_of(hit)


def routable(ev, bot_user):
    """Whether a message event (or a history message) is a person's message to route at all: not an edit, a
    join or a delete; not this bot's own post or any bot's."""
    if ev.get("type", "message") != "message":
        return False
    if ev.get("subtype") not in (None, "file_share"):
        return False
    if ev.get("bot_id") or not ev.get("user") or ev.get("user") == bot_user:
        return False
    return bool(ev.get("ts"))


def place_of(cid, uid, group):
    """conversations.md's place: a 1:1 DM by the person's user id, a group DM by its own id."""
    return f"group:{cid}" if is_group(cid, group) else f"dm:{uid}"


def msg_id(cid, ts):
    """A Slack message's id: its ts is unique only within its conversation."""
    return f"{cid}:{ts}"


def is_group(cid, group):
    """A group DM, or anything that isn't a 1:1 DM (its id starts with D), even if the flag were lost: gated as a room."""
    return bool(group) or not str(cid).startswith("D")


def label_of(group, hit, names=None, total=None):
    """The conversation's label: a DM by its person's name; a group DM by its people's names (the old bridge's), as
    many as fit in loom's label, and how many more (`total` counts everyone in it but the bot, named or not)."""
    if not group:
        return f"DM with {hit[1].get('name')}" if hit and hit[1].get("name") else "DM"
    names = list(names or [])
    total = max(total or 0, len(names))
    for n in range(len(names), 0, -1):
        label = group_label(names[:n], total)
        if common.len16(label) <= common.MAX_LABEL:
            return label
    return f"group DM with {total} people" if total else "group DM"


def group_label(shown, total):
    more = total - len(shown)
    return f"group DM with {', '.join(shown)}" + (f" and {more} more" if more else "")


def clean_text(text, people, bot_user=None, agent=None):
    """Slack's markup made readable (the old bridge's clean_text): a known person's mention as @name, this bot's as
    @<agent>, any other as @someone; links as text; and Slack's three escapes (&lt; &gt; &amp;) as the characters."""
    def mention(mo):
        if bot_user and agent and mo.group(1) == bot_user:
            return f"@{agent}"
        hit = people.get(mo.group(1))
        return f"@{hit[1].get('name')}" if hit and hit[1].get("name") else "@someone"

    def link(mo):
        return f"{mo.group(2)} ({mo.group(1)})" if mo.group(2) else mo.group(1)
    text = MENTION.sub(mention, text or "")
    text = LINK.sub(link, text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def text_of(ev, people, bot_user=None, agent=None):
    text = clean_text(ev.get("text"), people, bot_user, agent)
    for f in ev.get("files", []) or []:
        text += f" [file: {f.get('name')}]"
    return text
