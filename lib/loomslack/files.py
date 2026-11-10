"""Files people send (t-1961): fetched when their message is delivered, kept for the conversation's bodies.

A Slack file opens only with the bot's token, which a body never holds (it runs in a house). So the intake fetches
each file of a message it delivers live (files.slack.com only, no redirect followed, so the token goes nowhere else;
25 MB at most, 5 per message, as the old bridge did) into its own data dir, filed under the agent and the place the
message came in. The message says the file's id. A body asks the runtime for it (`loom runtime file <id>`, proving the
conversation it holds); the runtime asks this adapter for that file in that conversation's place (`send --place P
--agent A --file ID --out PATH`), and only a file filed under that agent and place is handed over. Kept 14 days.
"""
import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path

MAX_BYTES = 25 * 1024 * 1024
PER_MESSAGE = 5
KEEP_S = 14 * 86400
HOSTS = ("files.slack.com",)
ID = re.compile(r"^F[A-Z0-9]{2,30}$")
AGENT = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The bot's token goes to Slack's file host and nowhere else, so no redirect is followed."""

    def redirect_request(self, *a, **k):
        return None


def _open(req, timeout):
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def name_of(f):
    """A file's name as a body may save it: letters, digits, `._-`, no leading dot."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(f.get("name") or "file"))[:80].lstrip("._") or "file"


class Files:
    def __init__(self, data_dir, opener=_open, now=time.time):
        self.root, self.opener, self.now = Path(data_dir) / "files", opener, now

    def _dir(self, agent, place):
        if not AGENT.match(agent or ""):
            raise ValueError("not an agent name")
        return self.root / agent / hashlib.sha256(str(place).encode()).hexdigest()[:32]

    def fetch(self, agent, place, f, token, message=None):
        """(id, None) once the file is kept under (agent, place), or (None, why not). Fetched once; each message it
        came in is noted (`message`, the delivery's id), so the runtime hands it only to the conversation that message
        went to."""
        fid = str(f.get("id") or "")
        if not ID.match(fid):
            return None, "no file id"
        size = int(f.get("size") or 0)
        if size > MAX_BYTES:
            return None, f"too big to fetch ({size // 1048576} MB, the most is 25)"
        folder = self._dir(agent, place)
        meta_file = folder / f"{fid}.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                if message and message not in meta.get("messages", []):
                    meta["messages"] = (meta.get("messages", []) + [message])[-20:]
                    _write(meta_file, json.dumps(meta).encode())
                return fid, None
            except (OSError, ValueError):
                pass
        url = f.get("url_private_download") or f.get("url_private") or ""
        u = urllib.parse.urlparse(url)
        if u.scheme != "https" or u.hostname not in HOSTS:
            return None, "no link to fetch"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "loom-slack/0.1"})
        if req.host not in HOSTS:                 # what urllib will connect to, not only what urlparse reads
            return None, "no link to fetch"
        try:
            with self.opener(req, 30) as r:
                if "text/html" in (r.headers.get("Content-Type") or ""):
                    return None, "Slack sent a page, not the file (the bot may lack files:read)"
                data = r.read(MAX_BYTES + 1)
        except Exception as e:  # noqa: BLE001  a file that can't be fetched is said, never fatal
            return None, f"couldn't fetch it ({type(e).__name__})"
        if len(data) > MAX_BYTES:
            return None, "too big to fetch (over 25 MB)"
        for d in (self.root, self.root / agent, folder):
            d.mkdir(exist_ok=True, mode=0o700)
        _write(folder / f"{fid}.bin", data)
        _write(meta_file, json.dumps({"name": name_of(f), "size": len(data), "at": self.now(), "place": place,
                                      "sha256": hashlib.sha256(data).hexdigest(),
                                      "messages": [message] if message else []}).encode())
        self.prune()
        return fid, None

    def take(self, agent, place, fid, out):
        """{ok, name, size, sha256, messages}, the file copied to `out` (a new file), or {ok: false, reason}: only a
        file kept under this agent and this place. `messages` are the deliveries it came in, for the runtime to check."""
        if not ID.match(fid or ""):
            return {"ok": False, "reason": "not a Slack file id", "permanent": True}
        try:
            folder = self._dir(agent, place)
        except ValueError as e:
            return {"ok": False, "reason": str(e), "permanent": True}
        try:
            meta = json.loads((folder / f"{fid}.json").read_text())
        except (OSError, ValueError):
            return {"ok": False, "reason": f"no file {fid} was sent in this conversation (or it's older than 14 days)",
                    "permanent": True}
        if meta.get("place") != place:
            return {"ok": False, "reason": f"no file {fid} was sent in this conversation", "permanent": True}
        try:
            src = open(folder / f"{fid}.bin", "rb")
        except OSError:
            return {"ok": False, "reason": f"no file {fid} was sent in this conversation (or it's older than 14 days)",
                    "permanent": True}
        with src:
            try:
                fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except OSError as e:
                return {"ok": False, "reason": f"can't write where the runtime asked ({type(e).__name__})"}
            with os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return {"ok": True, "name": meta["name"], "size": meta["size"], "sha256": meta["sha256"],
                "messages": meta.get("messages", [])}

    def prune(self):
        """Files older than KEEP_S go: a conversation's files, not an archive."""
        cut = self.now() - KEEP_S
        for meta in self.root.glob("*/*/*.json") if self.root.exists() else []:
            try:
                if json.loads(meta.read_text()).get("at", 0) < cut:
                    meta.with_suffix(".bin").unlink(missing_ok=True)
                    meta.unlink()
            except (OSError, ValueError):
                pass


def _write(path, data):
    tmp = path.with_name(path.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    os.replace(tmp, path)
