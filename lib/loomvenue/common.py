"""What loom's venue adapters share (loom docs/specs/agent-runtime.md, "The sources"; conversations.md; bridges.md).

The same file ships in loom-discord and loom-slack: each is its own piece, so neither imports the other.

A venue adapter receives platform messages, routes each to an agent, and hands it to loom's `deliver`. It never
starts a body and never names a harness: the runtime does that (agent-runtime.md, test 1). In **shadow** mode it
receives and routes, records nothing and posts nothing, and logs `(platform message id, agent, place)` for each
message it would deliver, so `compare` can match it against the old bridge's log by message id, with what a local
check of loom's deliver contract said of the body it would have sent (`contract`).

Here:
  - settings: JSON-valued environment settings (loom sets a managed piece's settings as its environment);
  - Secrets: an agent's bot token from the secrets piece, only when its policy allows that agent (`may`);
  - Gates: who a platform id is, from the runtime (`loom runtime gates people --agent a --json`), failing closed;
  - from_of: a delivery's `from`, a person card's slug or {kind: unknown};
  - ShadowLog: the routing log (the same line shuttle's bridges write, shuttle/shadowlog.py);
  - Deliverer: POST /api/loom/deliver with the piece's own token; `iso`, the time it takes;
  - contract_problems: a deliver body checked against loom's own deliver contract, locally (the shadow's check);
  - verdict: what a deliver answer means for the message (done, refused for good, held, or tried again);
  - Alerter: the owner's alert command, once per condition;
  - compare: the two logs, matched by message id;
  - Presence: what the runtime says of a body at work in a place (`send --status`), and the stop word (t-1756).

Nothing here logs a message's text or a token. Text goes only into `deliver`, which puts it in loom's audit log
(conversations.md, rule 4); a turn in core is metadata only.
"""
import datetime
import decimal
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
AGENT = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
MAX_TEXT = 40_000
MAX_LABEL = 80
MAX_ID = 200


def log(msg):
    """One line to the journal. Callers pass ids, counts and reasons, never a message's text or a token."""
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- settings

def setting(env, name, default=None):
    v = env.get(name)
    return v if v not in (None, "") else default


def json_setting(env, name, default=None):
    """A setting holding JSON (an argv, a map). A malformed one is an error, never a silent default."""
    v = env.get(name)
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except ValueError:
        raise SystemExit(f"{name} isn't JSON") from None


def argv_setting(env, name, default):
    v = json_setting(env, name, default)
    if not (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
        raise SystemExit(f"{name} is a JSON list of strings (an argv)")
    return v


def fill(argv, **vals):
    """{agent}, {name} … filled in each argument; nothing else is templated."""
    out = []
    for a in argv:
        for k, v in vals.items():
            a = a.replace("{" + k + "}", str(v))
        out.append(a)
    return out


def bots_setting(env, name):
    """{agent: secret name}: which agents have a bot on this platform, and the secret its token comes from."""
    v = json_setting(env, name, {})
    if not isinstance(v, dict) or not all(AGENT.match(str(a)) and isinstance(s, str) and s for a, s in v.items()):
        raise SystemExit(f'{name} is a JSON map of agent to secret name, like {{"kit": "kit-bot-token"}}')
    return v


def mode_setting(env, name):
    m = setting(env, name, "shadow")
    if m not in ("shadow", "live"):
        raise SystemExit(f"{name} is shadow or live")
    return m


# ---------------------------------------------------------------- secrets

class SecretRefused(Exception):
    pass


class Secrets:
    """An agent's bot token, from the secrets piece. The policy decides (`loom secrets may <agent> <name>`); only
    an `allowed` answer reads the value, host-side (`get --reveal`, the secrets piece's README: the flag that still
    opens it for host-side tools). The value stays in this process: never logged, never in argv, never on disk."""

    def __init__(self, may_argv, get_argv, run=subprocess.run, env=None):
        self.may_argv, self.get_argv, self.run = may_argv, get_argv, run
        self.env = os.environ if env is None else env
        self._cache = {}

    def get(self, agent, name):
        key = (agent, name)
        if key in self._cache:
            return self._cache[key]
        r = self.run(fill(self.may_argv, agent=agent, name=name), capture_output=True, text=True, timeout=30)
        try:
            verdict = json.loads(r.stdout or "{}").get("may")
        except ValueError:
            verdict = None
        if verdict != "allowed":
            raise SecretRefused(f"the secrets policy says {verdict or 'nothing'} for {agent}'s {name}")
        env = {k: v for k, v in self.env.items() if k != "LOOM_STORE_GRANT"}
        env["LOOM_SECRETS_ALLOW_GET"] = "1"
        r = self.run(fill(self.get_argv, agent=agent, name=name), capture_output=True, text=True, timeout=30, env=env)
        if r.returncode != 0 or not r.stdout.strip():
            raise SecretRefused(f"{name} couldn't be read (exit {r.returncode})")
        self._cache[key] = r.stdout.strip()
        return self._cache[key]

    def forget(self, agent=None):
        for k in [k for k in self._cache if agent is None or k[0] == agent]:
            self._cache.pop(k)


# ---------------------------------------------------------------- gates and routes

class Doc:
    """A JSON document an argv prints as {"doc": …}, asked at most every `refresh_s`. Fails closed: when the
    command fails, `doc()` answers None (nobody routes) until it answers again, as shuttle's bridges do once the
    runtime holds the gates."""

    def __init__(self, argv, refresh_s=60, run=subprocess.run, now=time.time, what="a document"):
        self.argv, self.refresh_s, self.run, self.now, self.what = argv, refresh_s, run, now, what
        self._doc, self._at, self._failing = None, float("-inf"), False

    def doc(self):
        if self.now() - self._at < self.refresh_s:
            return self._doc
        self._at = self.now()
        try:
            env = {k: v for k, v in os.environ.items() if k != "LOOM_STORE_GRANT"}
            r = self.run(self.argv, capture_output=True, text=True, timeout=30, env=env)
            got = json.loads(r.stdout) if r.returncode == 0 else None
            doc = got.get("doc") if isinstance(got, dict) else None
            why = None if isinstance(doc, dict) else (f"exit {r.returncode}" if r.returncode else "no document")
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            doc, why = None, type(e).__name__
        if why:
            if not self._failing:
                log(f"FAIL CLOSED: {self.what} unreadable ({why}); nothing routes until it answers")
            self._failing, self._doc = True, None
            return None
        if self._failing:
            log(f"{self.what} readable again")
        self._failing, self._doc = False, doc
        return doc


def index(doc, platform, team=None, sections=("agents", "people")):
    """{platform user id: (slug, record)} from a gates document (shuttle's selvedge.py, ported). Slack ids are per
    workspace: pass the team id. A person's entry wins over an agent's for the same id. Never raises: an entry of
    the wrong shape is skipped, so a bad edit locks out that entry, not the adapter."""
    out = {}
    if not isinstance(doc, dict):
        return out
    for section in sections:
        entries = doc.get(section)
        if not isinstance(entries, dict):
            continue
        for slug, rec in entries.items():
            if not isinstance(slug, str) or slug.startswith("_") or not isinstance(rec, dict):
                continue
            ids = rec.get(platform)
            if isinstance(ids, dict):
                ids = ids.get(team)
            if isinstance(ids, (str, int)):
                ids = [ids]
            if not isinstance(ids, list):
                continue
            for uid in ids:
                if isinstance(uid, (str, int)) and not isinstance(uid, bool):
                    out[str(uid)] = (slug, dict(rec, agent=section == "agents"))
    return out


def may_wake(rec, venue=None):
    """(ok, why) for a record from index(), or None (shuttle's selvedge.may_wake): tier 2 and up anywhere; a guest
    (tier 1) only inside its `venues`; a listed agent (a peer bot) anywhere; anyone else nowhere."""
    if not rec:
        return False, "not registered"
    if rec.get("agent"):
        return True, None
    tier = rec.get("tier")
    if not isinstance(tier, int) or isinstance(tier, bool):
        return False, "no tier"
    if tier >= 2:
        return True, None
    if tier == 1 and venue is not None and venue in (rec.get("venues") or []):
        return True, None
    return False, f"tier {tier}" + (" outside its venues" if tier == 1 else "")


def from_of(hit):
    """A delivery's `from` (agent-runtime.md, "Who may deliver what"): the person card's slug when the gates
    resolve the platform id to a person, else {kind: unknown} (a peer bot, a webhook, anyone the gates don't name)."""
    if hit:
        slug, rec = hit
        if not rec.get("agent") and isinstance(slug, str) and SLUG.match(slug) and len(slug) <= 80:
            return {"kind": "person", "slug": slug}
    return {"kind": "unknown"}


# ---------------------------------------------------------------- the shadow log

class ShadowLog:
    """What the adapter would deliver: `{at, platform, id, agent, place, contract?}`, one line each, in
    `shadow-routes.jsonl`, the same line shuttle's bridges write (shuttle/shadowlog.py) plus `contract` (readers
    ignore keys they don't know). Metadata only. Capped like theirs."""

    NAME = "shadow-routes.jsonl"
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, data_dir, platform, now=time.time):
        self.path = Path(data_dir) / self.NAME
        self.platform, self.now = platform, now

    def routed(self, msg_id, agent, place, contract=None):
        """`contract`, when given, is what contract_problems said of the delivery it would have made: [] is "ok".
        Problems are words about the body's shape, never its text."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.path.stat().st_size > self.MAX_BYTES:
                    lines = self.path.read_text().splitlines(keepends=True)
                    self.path.write_text("".join(lines[len(lines) // 2:]))
            except FileNotFoundError:
                pass
            with self.path.open("a") as f:
                line = {"at": round(self.now(), 3), "platform": self.platform, "id": str(msg_id), "agent": agent,
                        "place": place}
                if contract is not None:
                    line["contract"] = list(contract) or "ok"
                f.write(json.dumps(line, separators=(",", ":")) + "\n")
            return True
        except OSError as e:
            log(f"shadow log: {e!r}")
            return False


# ---------------------------------------------------------------- deliver

class Deliverer:
    """POST /api/loom/deliver as this piece (agent-runtime.md, "The deliver contract"). Answers (status, body):
    status 0 is no answer (loom down, a timeout, no token). The same (piece, id) within 24 h is loom's to replay."""

    def __init__(self, url, token_file, opener=urllib.request.urlopen):
        self.url, self.token_file, self.opener = (url or "").rstrip("/").removesuffix("/mcp"), token_file, opener

    def deliver(self, body):
        try:
            token = Path(self.token_file).read_text().strip() if self.token_file else ""
        except OSError:
            token = ""
        if not (self.url and token):
            return 0, {"error": "no loom address or no token to deliver with"}
        req = urllib.request.Request(f"{self.url}/api/loom/deliver", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
        try:
            with self.opener(req, timeout=130) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {"error": f"HTTP {e.code}"}
        except (OSError, ValueError) as e:
            return 0, {"error": type(e).__name__}


def len16(s):
    """A string's length as loom counts it: JavaScript's `.length`, UTF-16 code units (an emoji is 2)."""
    return len(s.encode("utf-16-le")) // 2


def cap16(s, n):
    """`s` cut to at most `n` UTF-16 code units, never through a character."""
    if len16(s) <= n:
        return s
    out, used = [], 0
    for ch in s:
        w = 2 if ord(ch) > 0xFFFF else 1
        if used + w > n:
            break
        out.append(ch)
        used += w
    return "".join(out)


def delivery(msg_id, agent, venue, place, label, frm, at, text):
    """A deliver body, checked against the contract's caps. `id` is the agent's own copy of the message
    (`<agent>:<platform id>`): one platform message addressed to two agents is two deliveries."""
    body = {"id": cap16(f"{agent}:{msg_id}", MAX_ID), "agent": agent, "venue": venue, "place": place,
            "from": frm, "kind": "message", "at": at, "text": cap16(text or "(no text)", MAX_TEXT)}
    if label:
        body["label"] = cap16(label, MAX_LABEL)
    return body


_EPOCH = re.compile(r"\d{1,12}(\.\d+)?")
_ISO_IN = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})")
_UTC = datetime.timezone.utc


def iso(t):
    """A time as loom's deliver takes it (ISO_UTC): UTC, milliseconds, `Z`, like `2026-11-06T16:34:58.881Z`.

    `t` is epoch seconds (a number, or a string such as Slack's ts `1793828004.242749`) or an ISO-8601 time with its
    offset (Discord's `2026-11-06T16:34:58.881000+00:00`). The fraction is truncated to the millisecond, never
    rounded, so a time never moves into the next second (or day). Anything else is a ValueError."""
    if isinstance(t, bool) or t is None:
        raise ValueError("not a time")
    s = t.strip() if isinstance(t, str) else None
    if s is not None and not _EPOCH.fullmatch(s):
        m = _ISO_IN.fullmatch(s)
        if not m:
            raise ValueError(f"not a time: {s[:40]!r}")
        frac = (m.group(3) or "")[:3].ljust(3, "0")
        off = "+00:00" if m.group(4) == "Z" else m.group(4)
        try:
            dt = datetime.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}.{frac}{off}").astimezone(_UTC)
        except OverflowError:
            raise ValueError("not a time: out of range") from None
    else:
        try:
            d = decimal.Decimal(s if s is not None else repr(float(t)))
        except (decimal.InvalidOperation, TypeError, ValueError, OverflowError):
            raise ValueError("not a time") from None
        if not d.is_finite():
            raise ValueError("not a time")
        ms = int((d * 1000).to_integral_value(rounding=decimal.ROUND_FLOOR))
        try:
            dt = datetime.datetime(1970, 1, 1, tzinfo=_UTC) + datetime.timedelta(milliseconds=ms)
        except OverflowError:
            raise ValueError("not a time: out of range") from None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------- loom's deliver contract, checked here

# Copied from loom (github.com/sleepunit-agents/loom, main 720ead5, 2026-11-06), where core checks a delivery
# before anything runs and answers 400 `bad-input: …` for what's wrong: src/delivery.ts `parseDeliver` (its keys,
# id, text, kind, local) and src/backends/conversations.ts `parseTurn` (venue, place, at, from, label), with their
# constants (DELIVER_KEYS, DELIVER_KINDS, MAX_DELIVERY_ID 200, MAX_DELIVERY_TEXT 40,000, MAX_PLACE 200, MAX_LABEL
# 80; lengths are JavaScript's, UTF-16 units: len16). Two rules are the runtime's own parse, which core forwards to
# and whose 400 it passes back (loom-box src/delivery.mjs `parseDelivery`): agent is an agent id, and a local wake
# is from a schedule. The words are loom's, so a shadow mismatch can be looked up in loom's source.
# test_deliver_contract's LoomSource test checks these against a loom checkout (LOOM_SRC). ISO_UTC is
# conversations.ts's /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$/; JavaScript's `$` is the end of the string,
# so here it's used with fullmatch (Python's `$` would also take a trailing newline).
ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z")
DELIVER_KEYS = ("id", "agent", "venue", "place", "label", "from", "kind", "at", "text", "local")
DELIVER_KINDS = ("message", "wake")
VENUE_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
PRINTABLE = re.compile(r"[^\x00-\x1f\x7f]+")
MAX_PLACE = 200


def contract_problems(body):
    """What loom's deliver would refuse with a 400 in this body, in loom's words; [] when its shape is right.

    A local check, nothing sent: the shadow runs it on every delivery it would have made, so a body loom can't take
    shows up in a shadow window instead of on the first live message. Who may deliver what (a 403: the venue, the
    grant) is core's to say and isn't checked here. Never quotes the text."""
    if not isinstance(body, dict):
        return ["a delivery is a JSON object"]
    out = []
    bad = [k for k in body if k not in DELIVER_KEYS]
    if bad:
        out.append(f"a delivery has no key {json.dumps(bad[0])} (it has {', '.join(DELIVER_KEYS)})")

    def string(k, lo, hi):
        v = body.get(k)
        return isinstance(v, str) and lo <= len16(v) <= hi
    if not string("id", 1, MAX_ID):
        out.append(f"id is required: the source's own id for the message, 1-{MAX_ID} characters")
    if not (isinstance(body.get("agent"), str) and AGENT.fullmatch(body["agent"])):
        out.append("agent is an agent id")
    if not string("text", 1, MAX_TEXT):
        out.append(f"text is required: the message, 1-{MAX_TEXT} characters")
    if body.get("kind") == "note":
        out.append("kind is message or wake: a note is the agent's own turn, which a source doesn't write")
    elif body.get("kind") not in DELIVER_KINDS:
        out.append(f"kind is one of {', '.join(DELIVER_KINDS)}")
    if "local" in body and body["local"] is not True:
        out.append("local is true, or left out")
    if body.get("local") is True and (body.get("venue") != "wake" or body.get("kind") != "wake"):
        out.append("local is only for a wake, on venue wake")
    elif body.get("local") is True and (body.get("from") or {}).get("kind") != "schedule":
        out.append("local is only for a wake from a schedule, on venue wake")
    if not (isinstance(body.get("venue"), str) and VENUE_NAME.fullmatch(body["venue"])):
        out.append("venue is a venue name: lowercase letters, digits and dashes, at most 32")
    if not string("place", 1, MAX_PLACE):
        out.append(f"place is 1-{MAX_PLACE} characters")
    at = body.get("at")
    if not (isinstance(at, str) and ISO_UTC.fullmatch(at) and _parses(at)):
        out.append("at is an ISO-8601 UTC time (…Z)")
    if not _from_ok(body.get("from")):
        out.append("from is {kind: person, slug}, {kind: agent, id}, {kind: schedule} or {kind: unknown}, "
                   "with no other keys")
    if "label" in body and not (string("label", 1, MAX_LABEL) and PRINTABLE.fullmatch(body["label"])):
        out.append(f"label is 1-{MAX_LABEL} printable characters")
    return out


def _parses(at):
    try:
        datetime.datetime.fromisoformat(at[:-1] + "+00:00")
        return True
    except ValueError:
        return False


def _from_ok(v):
    if not isinstance(v, dict):
        return False
    keys = sorted(v)
    if v.get("kind") == "person" and keys == ["kind", "slug"]:
        return isinstance(v["slug"], str) and bool(SLUG.fullmatch(v["slug"])) and len(v["slug"]) <= 80
    if v.get("kind") == "agent" and keys == ["id", "kind"]:
        return isinstance(v["id"], str) and bool(AGENT.fullmatch(v["id"]))
    return v.get("kind") in ("schedule", "unknown") and keys == ["kind"]


# ---------------------------------------------------------------- what a deliver answer means

DONE, FINAL, HOLD, RETRY = "done", "final", "hold", "retry"


def verdict(status, ans):
    """What loom's answer to a delivery means for the message (loom src/delivery.ts, `deliver`, and loom-box's
    delivery.mjs, whose own refusals core passes through):

      - DONE: 200, loom took it.
      - FINAL: loom refused this message for good, and no retry or update changes that: 403 (who may deliver what:
        a venue the piece doesn't declare, an agent that isn't the household's or isn't granted, a person's message
        from a piece that isn't the venue's adapter, a wake's body in the wrong place), 409 (the same id came with
        another body) and 413 (over the route's 256 KB).
      - HOLD: loom refused how this adapter wrote the request: every 400 (core's `bad-input: …` from parseDeliver,
        `bad-input: malformed JSON` from the route, src/transport/http-server.ts `deliverApi`, or the runtime's own
        parse) and 415 (the route's `unsupported-media-type`, http-server.ts). None of them is about who sent the
        message or where: each is a field this adapter wrote (its time, id, keys, a length), and `delivery` keeps
        every field within loom's limits, so a 400 means this code is wrong. The same body is refused every time;
        a fixed adapter would be taken. So the message is held, not marked done, the owner is alerted, and it's
        tried again once the adapter restarts (an update does). Not by loom's wording: a reworded refusal holds.
      - RETRY: anything else (no answer, 401, 429, 5xx): the next tick tries again."""
    if status == 200:
        return DONE
    if status in (400, 415):
        return HOLD
    if status in (403, 409, 413):
        return FINAL
    return RETRY


# ---------------------------------------------------------------- the owner's alert command

def alert_setting(env, name):
    """The owner's alert command, a JSON argv, or None (unset: the journal line is all)."""
    return None if json_setting(env, name, None) is None else argv_setting(env, name, None)


ALERT_SOURCE = re.compile(r"[a-z][a-z0-9-]{0,39}")
ALERT_FACT = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class Alerter:
    """The owner's alert command (loom docs/specs/keeping-watch.md, "The alert path"): `argv` run detached, never
    waited on, as `<source> --severity bad --summary <one line> [--fact k=v]...`. Once per `key` in a process:
    a condition (deliveries held, a shadow body loom wouldn't take) is one alert, not one per message. Each alert is
    also a journal line. Facts are ids, codes and loom's own words, never a message's text."""

    def __init__(self, argv=None, popen=subprocess.Popen):
        self.argv, self.popen, self.said = argv, popen, set()

    def alert(self, key, source, severity, summary, **facts):
        if key in self.said:
            return False
        self.said.add(key)
        summary = " ".join(str(summary).split())[:200]
        log(f"ALERT {source} ({severity}): {summary}")
        if not self.argv or not ALERT_SOURCE.fullmatch(source):
            return False
        argv = [*self.argv, source, "--severity", severity, "--summary", summary]
        for k, v in facts.items():
            if ALERT_FACT.fullmatch(k):
                argv += ["--fact", f"{k}={' '.join(str(v).split())[:300]}"]
        env = {k: v for k, v in os.environ.items() if k != "LOOM_STORE_GRANT"}
        try:
            self.popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       start_new_session=True, env=env)
            return True
        except OSError as e:
            log(f"the alert command didn't run ({type(e).__name__})")
            return False


# ---------------------------------------------------------------- state

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save_json(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# ---------------------------------------------------------------- the comparison

def read_logs(patterns, platform):
    """{message id: {(agent, place), …}} and {message id: first `at`}, from every file the patterns match."""
    pairs, first = {}, {}
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            try:
                text = Path(path).read_text()
            except OSError:
                continue
            for line in text.splitlines():
                try:
                    x = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(x, dict) or x.get("platform") != platform or not x.get("id"):
                    continue
                mid = str(x["id"])
                pairs.setdefault(mid, set()).add((str(x.get("agent")), str(x.get("place"))))
                at = x.get("at")
                if isinstance(at, (int, float)):
                    first[mid] = min(first.get(mid, at), at)
    return pairs, first


def compare(adapter_logs, bridge_logs, platform, start, end, grace_s=600, min_messages=20, gone=None):
    """Every message first seen (on either side) inside [start + grace, end - grace] must have the same set of
    (agent, place) on both sides. The grace keeps a message seen a moment before the window on one side from
    counting as missing. Answers a report: verdict `agree` (and at least `min_messages`), `too-few`, or `disagree`.

    `gone(id, pairs)`, when given, is asked of each message only the bridge routed: True when the platform no
    longer has it (deleted before a shadow that reads late could see it; the live socket gets it as it's sent).
    Those are listed as `gone` and count neither way; False or None (couldn't tell) leaves it `only_bridge`."""
    a, af = read_logs(adapter_logs, platform)
    b, bf = read_logs(bridge_logs, platform)
    lo, hi = start + grace_s, end - grace_s
    ids = sorted(m for m in set(a) | set(b) if lo <= min(af.get(m, float("inf")), bf.get(m, float("inf"))) <= hi)
    same, differ, only_a, only_b, gone_ids = [], [], [], [], []
    for m in ids:
        if m not in b:
            only_a.append(m)
        elif m not in a:
            (gone_ids if gone is not None and gone(m, sorted(b[m])) is True else only_b).append(m)
        elif a[m] == b[m]:
            same.append(m)
        else:
            differ.append({"id": m, "adapter": sorted(a[m]), "bridge": sorted(b[m])})
    agree = not (differ or only_a or only_b)
    verdict = "disagree" if not agree else ("agree" if len(same) >= min_messages else "too-few")
    return {"platform": platform, "window": [iso(start), iso(end)], "grace_s": grace_s,
            "messages": len(ids) - len(gone_ids), "same": len(same), "differ": differ, "only_adapter": only_a,
            "only_bridge": only_b, "gone": gone_ids, "min_messages": min_messages, "verdict": verdict}


def compare_main(argv, platform, gone_checker=None):
    """`loom-<venue> compare --start <iso|epoch> [--hours 24] --adapter <glob>... --bridge <glob>... [--json]`.
    Exit 0 when the window agrees with at least 20 messages, 1 when it disagrees, 3 when there are too few.
    `--check-gone` (a venue that passes `gone_checker`, a function making compare's `gone`) asks the platform
    about each message only the bridge routed: metadata only, never its text."""
    import argparse
    p = argparse.ArgumentParser(prog=f"loom-{platform} compare")
    p.add_argument("--start", required=True, help="the window's start: ISO-8601 or epoch seconds")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--adapter", action="append", required=True, help="the adapter's shadow-routes.jsonl (a glob)")
    p.add_argument("--bridge", action="append", required=True, help="the old bridge's shadow-routes.jsonl (a glob)")
    p.add_argument("--grace", type=float, default=600.0, help="seconds left out at each end")
    p.add_argument("--min", type=int, default=20)
    p.add_argument("--json", action="store_true")
    if gone_checker is not None:
        p.add_argument("--check-gone", action="store_true",
                       help="ask the platform whether each message only the bridge routed still exists")
    a = p.parse_args(argv)
    try:
        start = float(a.start)
    except ValueError:
        start = datetime.datetime.fromisoformat(a.start.replace("Z", "+00:00")).timestamp()
    gone = gone_checker() if gone_checker is not None and a.check_gone else None
    r = compare(a.adapter, a.bridge, platform, start, start + a.hours * 3600, a.grace, a.min, gone=gone)
    if a.json:
        print(json.dumps(r, indent=1))
    else:
        print(f"{platform} {r['window'][0]} .. {r['window'][1]}: {r['messages']} messages, {r['same']} the same, "
              f"{len(r['differ'])} different, {len(r['only_adapter'])} only the adapter's, "
              f"{len(r['only_bridge'])} only the bridge's"
              + (f", {len(r['gone'])} gone from {platform} (not counted)" if r["gone"] else "") + f": {r['verdict']}")
        for d in r["differ"][:20]:
            print(f"  differ {d['id']}: adapter {d['adapter']} bridge {d['bridge']}")
        for m in r["only_adapter"][:20]:
            print(f"  only the adapter: {m}")
        for m in r["only_bridge"][:20]:
            print(f"  only the bridge: {m}")
        for m in r["gone"][:20]:
            print(f"  gone from {platform}: {m}")
    return {"agree": 0, "disagree": 1, "too-few": 3}[r["verdict"]]


# ---------------------------------------------------------------- presence and the stop word (t-1756)

PLACE = re.compile(r"^[a-z]{2,10}:[A-Za-z0-9]{1,40}$")
STATUS_FRESH_S = 90      # a working body the runtime hasn't said again of in this long: shown no more
STOP_RE = re.compile(r"^(stop|cancel)[.!]*$", re.IGNORECASE)


def is_stop_text(text):
    """A message that's only the stop word ("stop" or "cancel", a trailing ! or . allowed), its mentions aside."""
    return bool(STOP_RE.match((text or "").strip()))


class Presence:
    """What the runtime says of a body at work in a place, for the run loop to show (loom-box delivery.mjs, presence).

    The runtime runs the venue's declared send argv with `--place P --agent A --status working [--message ID]...`
    when a message reaches a body, again every 15 s while it works, and `--status done` once its turn is over. That
    short process only writes here; the run loop, which holds the bots, shows it (typing, 👀) and takes it down. The
    run loop writes the other two: a stop (a person said the stop word in the place) and the agent's last post.

        <data>/presence/<agent>/<kind>-<id>.json   {place, working, at, since, acks: [{id, at}]}
        <data>/stops/<agent>/<kind>-<id>.json      {at, msg}
        <data>/posted/<agent>/<kind>-<id>.json     {at}

    Ids and times only: never a message's text."""

    def __init__(self, data_dir, now=time.time):
        self.dir, self.now = Path(data_dir), now

    def _file(self, kind, agent, place):
        if not AGENT.match(agent or "") or not PLACE.match(place or ""):
            raise ValueError("not an agent and a place")
        k, _, ident = place.partition(":")
        return self.dir / kind / agent / f"{k}-{ident}.json"

    def status(self, agent, place, state, messages=()):
        """The runtime's word: {ok, stop?}. A stop said since this body started working there answers stop: true."""
        if state not in ("working", "done"):
            return {"ok": False, "reason": "--status is working or done"}
        try:
            f = self._file("presence", agent, place)
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
        rec = load_json(f, {}) or {}
        now = self.now()
        if state == "done":
            if rec:
                rec.update(working=False, at=now)
                save_json(f, rec)
            self._file("stops", agent, place).unlink(missing_ok=True)
            return {"ok": True}
        if not rec.get("working"):
            rec = {"place": place, "working": True, "since": now, "acks": [a for a in rec.get("acks") or [] if isinstance(a, dict)]}
        rec["at"] = now
        have = {a.get("id") for a in rec["acks"]}
        for m in messages:
            if isinstance(m, str) and 0 < len(m) <= MAX_ID and m not in have:
                rec["acks"].append({"id": m, "at": now})
                have.add(m)
        rec["acks"] = rec["acks"][-20:]
        save_json(f, rec)
        stop = load_json(self._file("stops", agent, place), None)
        return {"ok": True, **({"stop": True} if isinstance(stop, dict) and stop.get("at", 0) >= rec.get("since", now) else {})}

    def working(self, agent, place):
        """Whether the runtime says a body of `agent` works in `place` now."""
        try:
            rec = load_json(self._file("presence", agent, place), {}) or {}
        except ValueError:
            return False
        return bool(rec.get("working")) and self.now() - rec.get("at", 0) < STATUS_FRESH_S

    def stop(self, agent, place, msg_id):
        save_json(self._file("stops", agent, place), {"at": self.now(), "msg": str(msg_id)[:MAX_ID]})

    def posted(self, agent, place):
        try:
            save_json(self._file("posted", agent, place), {"at": self.now()})
        except (ValueError, OSError) as e:
            log(f"presence: couldn't note a post ({type(e).__name__})")

    def last_post(self, agent, place):
        try:
            return (load_json(self._file("posted", agent, place), {}) or {}).get("at", 0)
        except ValueError:
            return 0

    def all(self):
        """[(agent, place, record)] the runtime has said of."""
        out = []
        for f in sorted((self.dir / "presence").glob("*/*.json")):
            rec = load_json(f, None)
            if isinstance(rec, dict) and PLACE.match(str(rec.get("place") or "")) and AGENT.match(f.parent.name):
                out.append((f.parent.name, rec["place"], rec))
        return out

    def clear(self, agent, place):
        self._file("presence", agent, place).unlink(missing_ok=True)

    def show(self, state, apis, react, typing=None):
        """One pass of the run loop: for each place a body works in, 👀 on each message it took (`react(api, place,
        msg, add)`), off once the agent has posted after it or the body is done; typing (`typing(api, place)`) while
        it owes a reply. `state` is the run loop's own memory: {"<agent>|<place>": [msg ids reacted to]}."""
        now = self.now()
        seen = set()
        for agent, place, rec in self.all():
            api = apis.get(agent)
            if api is None:
                continue
            key = f"{agent}|{place}"
            seen.add(key)
            reacted = state.setdefault(key, [])
            live = bool(rec.get("working")) and now - rec.get("at", 0) < STATUS_FRESH_S
            posted = self.last_post(agent, place)
            owes = False
            for a in rec.get("acks") or []:
                mid = str(a.get("id") or "")
                mid = mid.split(":", 1)[1] if mid.startswith(f"{agent}:") else mid
                answered = posted >= a.get("at", now)
                if (not live or answered) and mid in reacted:
                    if react(api, place, mid, False):
                        reacted.remove(mid)
                elif live and not answered:
                    owes = True
                    if mid not in reacted and react(api, place, mid, True):
                        reacted.append(mid)
            if owes and typing is not None:
                typing(api, place)
            if not live and not reacted:
                self.clear(agent, place)
                state.pop(key, None)
        for key in [k for k in state if k not in seen]:
            state.pop(key, None)
