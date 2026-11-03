# loom-slack

loom's Slack venue adapter (loom's `docs/specs/agent-runtime.md`, "The sources"; `conversations.md`, "Venues";
`bridges.md`). A thin piece: it holds each agent's own Slack app (one bot per agent per platform), routes its DMs and
group DMs to the agent, and hands each to loom's `deliver`. The runtime gets the conversation a body; when the agent
answers, the runtime runs this piece's `send`, which posts as that agent's app. It never starts a body and never
names a harness (agent-runtime.md, test 1: `test/test_contract.py` fails if it does). Removing it leaves every
conversation in loom.

```
loom pieces install --from github:sleepunit-agents/loom-slack#<commit>
loom pieces set slack 'SLACK_BOTS={"kit": "kit-slack.env"}'
loom slack status
```

## Routing

DMs and group DMs only (the app's bot scopes are `im:*` and `mpim:*`). A DM, or a group DM the bot is in, is that
bot's agent's (bridges.md, rule 1). Who may reach the agent (`lib/loomslack/routing.py`, shuttle's gate, ported):

- **a 1:1 DM:** Slack says the sender is a member of this workspace (`users.info`: not a Slack Connect stranger, not
  another team, not a guest account, not a bot or deleted) and the gates have them at tier 2 or up;
- **a group DM:** every other member passes that, and there are at least two of them;
- **Slack not answering** (`users.info` or `conversations.members` failing) is a wait, tried again for an hour;
  anyone else is left on read. The gates are the runtime's (`loom runtime gates people --agent a --json`), and
  unreadable gates fail closed.

## The delivery

```json
{"id": "<agent>:<conversation>:<ts>", "agent": "kit", "venue": "slack", "place": "dm:<user id>" | "group:<conversation id>",
 "label": "DM with Robin" | "group DM", "from": {"kind": "person", "slug": "robin"} | {"kind": "unknown"},
 "kind": "message", "at": "<the message's time>", "text": "<its text, mentions as @name, links as text, file names>"}
```

The text goes only to `deliver`, which puts it in loom's audit log; the turn in loom is metadata only. This piece
logs ids, counts and reasons, never text or a token. Files aren't fetched: a file's name is in the text.

## Two intakes

- **Live** (`SLACK_MODE=live`): Socket Mode, as the bridge it replaces. Each app's socket is dialled out (nothing
  inbound), each envelope acked at once. A message loom doesn't take is kept and tried again for an hour. Needs
  `websocket-client` (`python3-websocket`).
- **Shadow** (the default): no socket. With two Socket Mode connections open, Slack sends each event to only one of
  them, so a shadow socket beside the live bridge would take messages from it. The shadow reads the Web API instead,
  slowly: one `conversations.history` call per agent per tick (`SLACK_POLL_S`, 65 s), in turn over the bot's
  conversations, paging until each is read. It delivers nothing and posts nothing (`send` answers `{ok: false}`), and
  writes one line per message it would deliver to `<SLACK_DATA>/shadow-routes.jsonl`: `{at, platform, id, agent,
  place}`, the same line shuttle's bridges write. Then:

```
loom-slack compare --start 2026-11-03T00:00:00Z --adapter '<data>/shadow-routes.jsonl' --bridge '<bridge state>/shadow-routes.jsonl'
```

matches the two by message id: every message first seen in the window (less 10 minutes at each end) must have the
same agents and places on both sides, and there must be at least 20. Exit 0 agree, 1 disagree, 3 too few.

## Send

`loom-slack send --place <place> --agent <agent>`, the text on stdin: a DM's conversation is opened by the user id, a
group posts to its own id, split under 3900 characters, answering `{ok, id?}` or `{ok: false, reason, permanent?}`.
`permanent: true` is a refusal no retry will change, which the runtime records as refused and doesn't retry or keep
for a body: shadow mode, no app for that agent, not a Slack place, nothing to send, settings that don't parse, and a
post Slack refuses with an HTTP 4xx or an error code about the post (`channel_not_found`, `not_in_channel`,
`is_archived`, `missing_scope`, `invalid_auth`, …: any code that isn't Slack's own trouble). A rate limit, `fatal_error`/`internal_error`/
`service_unavailable`/`request_timeout`, a 5xx, the network, and a secret the policy doesn't allow (yet) say no
`permanent`. Run by
anything but loom's piece dispatch (the runtime runs it by its bin name), it goes through `loom slack send …` so
the piece's own settings apply.

## App credentials

`SLACK_BOTS` names, for each agent, the secret holding its app's env file: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`
(live), `SLACK_TEAM_ID`, `SLACK_BOT_USER_ID`. The secrets piece decides: only when `loom secrets may <agent> <name>`
answers `allowed` does this piece read it (host-side, `get --reveal`). The values stay in the process.

## Settings

| Name | What |
|---|---|
| `SLACK_MODE` | `shadow` (default) or `live` |
| `SLACK_BOTS` | `{"agent": "secret name"}` |
| `SLACK_DATA` | cursors, Slack's cached answers and the shadow log (default `${LOOM_PIECE_HOME}/data`) |
| `SLACK_POLL_S` | seconds between ticks (65 shadow, 5 live) |
| `SLACK_TOKEN_FILE` | live: a piece token of this piece's, to deliver with (`loom token create --piece slack/<name>`) |
| `SLACK_LOOM_URL` | loom's address (`http://127.0.0.1:8787`) |
| `SLACK_GATES_COMMAND` | a JSON argv printing `{"doc": …}`, `{agent}` filled in |

## What the runtime asks of a place, and a body at work

- `send --describe` answers `{ok, guest: false, peer: false, venue, readers: [], notes: []}`, because Slack routes
  only tier 2 and up. A tier's extra brief is the agent's policy's (`briefs.tiers`), in the runtime.
- `send --status working --message <delivery id>` (and `done`) is the runtime's word that a body is at work. Live,
  the intake puts 👀 (`eyes`) on the message the body took, and takes it off once the agent's `send` posts there or the
  body is done.
- **To one person:** `send --place dm:<user id>` opens that person's DM and posts there. The runtime's `send --person
  <slug> --via slack` runs it with the person's id from the gates.

## Not here (yet)

The local transcript a body read as context, fetching files, and a group DM's roster and restricts. `render` isn't
declared: loom's generic render shows these conversations.

## Tests

    python3 -m unittest discover -s test -t .

`lib/loomvenue/common.py` is the same file in loom-discord.

AGPL-3.0-or-later.
