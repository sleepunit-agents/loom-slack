"""Files people send (t-1961): fetched once a message is delivered live, with the bot's token and only from Slack's
file host; handed to the runtime only for the agent and place they came in; named in the message with the way a
body gets one."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from loomslack import adapter, files
from loomvenue import common
from test.fixtures import GATES, FakeSlack, ev
from test.test_adapter import T0, StaticDoc
from test.test_send import cli

URL = "https://files.slack.com/files-pri/T1-F0ABC/photo.jpg"


class Resp:
    def __init__(self, data, ctype="image/jpeg"):
        self.data, self.headers = data, {"Content-Type": ctype}

    def read(self, n=-1):
        return self.data if n < 0 else self.data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, data=b"JPEGDATA", ctype="image/jpeg"):
        self.data, self.ctype, self.reqs = data, ctype, []

    def __call__(self, req, timeout):
        self.reqs.append(req)
        return Resp(self.data, self.ctype)


def f(fid="F0ABC", url=URL, size=8, name="photo 1.jpg"):
    return {"id": fid, "name": name, "size": size, "url_private_download": url}


class Fetch(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="loom-slack-files-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.t = [T0]
        self.opener = Opener()
        self.files = files.Files(self.dir, opener=self.opener, now=lambda: self.t[0])

    def take(self, agent, place, fid):
        out = self.dir / f"out-{len(os.listdir(self.dir))}"
        return self.files.take(agent, place, fid, str(out)), out

    def test_fetched_with_the_token_from_slacks_file_host_and_handed_only_where_it_came_in(self):
        self.assertEqual(self.files.fetch("kit", "dm:U1", f(), "xb"), ("F0ABC", None))
        self.assertEqual(self.opener.reqs[0].get_header("Authorization"), "Bearer xb")
        got, out = self.take("kit", "dm:U1", "F0ABC")
        self.assertEqual((got["ok"], got["name"], got["size"]), (True, "photo_1.jpg", 8))
        self.assertEqual(out.read_bytes(), b"JPEGDATA")
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o600")
        for agent, place in (("kit", "dm:U2"), ("kit", "group:G1"), ("bolt", "dm:U1")):
            got, out = self.take(agent, place, "F0ABC")
            self.assertEqual(got["ok"], False, (agent, place))
            self.assertFalse(out.exists())
        self.assertEqual(self.files.fetch("kit", "dm:U1", f(), "xb", message="kit:D1:2.0"), ("F0ABC", None))
        self.assertEqual(len(self.opener.reqs), 1, "fetched once")
        self.assertEqual(self.take("kit", "dm:U1", "F0ABC")[0]["messages"], ["kit:D1:2.0"], "each message it came in")

    def test_never_sends_the_token_anywhere_but_slacks_file_host(self):
        for url in ("https://evil.example/x", "http://files.slack.com/x", "", "https://files.slack.com.evil.example/x",
                    "https://evil.example\\@files.slack.com/x"):
            self.assertEqual(self.files.fetch("kit", "dm:U1", f(url=url), "xb")[0], None, url)
        self.assertEqual(self.opener.reqs, [])

    def test_too_big_or_a_page_is_said_not_kept(self):
        self.assertIn("too big", self.files.fetch("kit", "dm:U1", f(size=files.MAX_BYTES + 1), "xb")[1])
        self.assertEqual(self.opener.reqs, [])
        self.opener.data = b"x" * (files.MAX_BYTES + 1)
        self.assertIn("too big", self.files.fetch("kit", "dm:U1", f(), "xb")[1])
        self.opener.data, self.opener.ctype = b"<html>", "text/html; charset=utf-8"
        self.assertIn("page", self.files.fetch("kit", "dm:U1", f(), "xb")[1])
        self.assertEqual(self.take("kit", "dm:U1", "F0ABC")[0]["ok"], False)

    def test_odd_ids_and_names_never_reach_the_disk(self):
        for fid in ("../x", "F", "f0abc", "F0ABC/../F1"):
            self.assertEqual(self.files.fetch("kit", "dm:U1", f(fid=fid), "xb")[0], None, fid)
            self.assertEqual(self.take("kit", "dm:U1", fid)[0]["ok"], False, fid)
        self.assertEqual(self.take("../kit", "dm:U1", "F0ABC")[0]["ok"], False)

    def test_kept_fourteen_days(self):
        self.files.fetch("kit", "dm:U1", f(), "xb")
        self.t[0] += files.KEEP_S + 1
        self.files.fetch("kit", "dm:U1", f(fid="F0DEF"), "xb")
        self.assertEqual(self.take("kit", "dm:U1", "F0ABC")[0]["ok"], False)
        self.assertEqual(self.take("kit", "dm:U1", "F0DEF")[0]["ok"], True)


class Route(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="loom-slack-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.api = FakeSlack()
        self.api.token = "xb"
        self.agents = {"kit": adapter.Agent("kit", self.api, "T1", "B0", StaticDoc(GATES))}
        self.opener = Opener()

    def deliver(self, sink, *fs, files_=True):
        router = adapter.Router(self.dir, common.ShadowLog(self.dir, "slack"), sink=sink, now=lambda: T0,
                                files=files.Files(self.dir, opener=self.opener, now=lambda: T0) if files_ else None)
        sock = adapter.Socket(router, self.agents, {"kit": "app"})
        sock.events.put(("kit", {"event": dict(ev(T0 + 1, "U1", "look"), channel="D1", channel_type="im", files=list(fs))}))
        sock.tick()

    def test_live_says_how_a_body_gets_each_file(self):
        took = []
        self.deliver(lambda a, b: took.append(b) or True, f(), f(fid="F0BAD", url="https://evil.example/x", name="b.txt"))
        self.assertEqual(took[0]["text"], "look [file: photo 1.jpg; to read it: loom runtime file F0ABC] [file: b.txt: no link to fetch]")
        out = io.StringIO()
        dest = self.dir / "got"
        with contextlib.redirect_stdout(out):
            code = cli.send(cli.config({"SLACK_MODE": "live", "SLACK_DATA": str(self.dir)}),
                            ["--place", "dm:U1", "--agent", "kit", "--file", "F0ABC", "--out", str(dest)])
        got = json.loads(out.getvalue())
        self.assertEqual((code, got["ok"], dest.read_bytes()), (0, True, b"JPEGDATA"))
        self.assertEqual(got["messages"], [took[0]["id"]], "the delivery it came in, for the runtime to check")
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.send(cli.config({"SLACK_MODE": "live", "SLACK_DATA": str(self.dir)}),
                            ["--place", "dm:U2", "--agent", "kit", "--file", "F0ABC", "--out", str(self.dir / "no")])
        self.assertEqual(code, 1)
        self.assertFalse((self.dir / "no").exists())

    def test_at_most_five_a_message(self):
        took = []
        self.deliver(lambda a, b: took.append(b) or True, *[f(fid=f"F0A{i}", name=f"{i}.jpg") for i in range(6)])
        self.assertEqual(took[0]["text"].count("loom runtime file"), 5)
        self.assertIn("[file: 5.jpg: not fetched, more than 5 in one message]", took[0]["text"])

    def test_shadow_fetches_nothing(self):
        self.deliver(None, f())
        self.assertEqual(self.opener.reqs, [])


if __name__ == "__main__":
    unittest.main()
