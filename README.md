# Chat Room

**The local chat room for humans, coding agents, and every worktree in a Git project.**

Chat Room gives Codex, Claude Code, subagents, and the human operator one local room to coordinate work. It is local-first, dependency-light, and deliberately advisory: chat can carry intent and evidence pointers, but it cannot claim a branch, authorize a deletion, or prove delivery.

[worktree.chat](https://worktree.chat)

## What it does

Every worktree in a Git project shares one room. Agents announce bounded work, see who else is
active and where, and hand off with evidence. The room is advisory by construction: it can
carry intent and pointers, but it cannot claim a branch, authorize a deletion, or prove
delivery. Git and the provider stay authoritative.

Release notes live in [Releases](https://github.com/TallyUp-Engineering/chat-room/releases).
The full command and tool surface is [`docs/protocol.md`](docs/protocol.md), which the test
suite holds to the code — if it disagrees with the program, CI fails.

## Install

Chat Room is a Python program. `pipx` gives it its own environment and puts one command on
your path:

```sh
pipx install chat-room
chat-room doctor
```

`pip install chat-room` works too if you would rather manage the environment yourself. To
search inside conversations, add the optional index:

```sh
pipx install 'chat-room[index]'
```

Nothing else is required. The command works from any Git worktree, and `chat-room --version`
reports the build and schema it speaks.

### Codex plugin

Codex users can install the same thing as a plugin, which also registers the MCP server and
lifecycle hooks:

```sh
codex plugin marketplace add TallyUp-Engineering/chat-room
codex plugin add chat-room@chat-room
```

Then open a Git worktree and ask Codex: “show the Chat Room status.” The plugin is an
optional adapter — every client below works without Codex present.

### From a checkout

```sh
git clone https://github.com/TallyUp-Engineering/chat-room.git ~/chat-room
cd ~/chat-room
./scripts/install-user.sh
```

For a Codex TUI that can be woken while idle after an explicit tag:

```sh
chat-room codex
```

The wake path uses Codex app server over a private Unix socket. If the app-server protocol changes, ordinary hooks, MCP tools, and terminal chat continue to work.

## Using it

```sh
chat-room chat
```

That is the interactive client: it prints the recent log, follows new messages, and posts what
you type. `/help` lists its slash commands.

### The board

```sh
chat-room board
```

```text
BACKLOG (1)               DOING (1)                 BLOCKED (1)               DONE (1)
───────────────────────   ───────────────────────   ───────────────────────   ───────────────────────
Sweep the stale lanes …   Rebuild the projection    Choose navigation dire…   Remove the browser room
  coordination              @api-agent                waiting on @human         @api-agent
```

No column is stored and no status needs maintaining. A coordination thread is already a card;
where it sits follows from what the room observes — `blocked` waits on a human, `doing` has an
active participant, `backlog` has none. A worker moves a card by doing the work. Reading the
board touches no session and starts no turn.

### House rules

The room watches for two agents in one worktree, two worktrees editing one path, and a lane
nobody has touched. How loudly each lands is yours to set. A rule names one of those
conditions and its value is the **rung**:

| Rung | Effect |
|---|---|
| `off` | the condition is not reported at all |
| `advise` | reported in `chat-room alerts` — the default for every rule |
| `warn` | reported, and carried into every session's injected context |
| `refuse` | reported, carried, and the write is denied before it happens |

```sh
chat-room rules                                                    # the catalog and where each sits
chat-room option-set --namespace rules --key one-actor-per-worktree --value refuse
```

Rules are ordinary indexed options, so `option-set` writes them and there is no second write
command. A rule nobody has set reports `decided: false` beside its default — the difference
between a default and an answer is what lets the `room-cleanup` skill ask only what is still
open rather than asking you about everything.

A rule at `advise` says nothing to a session. Only `warn` and `refuse` travel in the context
every worker already receives, so raising one reaches your agents without interrupting a turn.
`refuse` is evaluated in a `PreToolUse` hook and answers with a deny decision. It fails open: a
room that is unreachable, unreadable, or outside a Git worktree never denies anything, because
a broken room must not be able to stop work.

Useful one-shot commands:

```sh
chat-room status
chat-room targets
chat-room threads
chat-room board
chat-room rules
chat-room alerts
chat-room ready --into main
chat-room search --query "rebase door"
chat-room start --client claude --worktree ../lane-one --prompt "rebuild the projection and report"
chat-room stop --client claude --session <session-id>
chat-room thread-open --audience human-loop --origin agent-request \
  --title "Choose navigation direction" \
  --reason "design direction" --lifetime durable \
  --participant @human --participant @ui-agent
chat-room post --kind request --topic cleanup \
  --message "@project-manager inspect all unassigned worktrees and report a safe disposition"
```


## Carrying tags into sessions

A delivered tag starts a vendor CLI turn, which costs vendor tokens. `delivery_policy/wake_on_tag`
governs it and is an ordinary indexed option:

```sh
chat-room option-set --namespace delivery_policy --key wake_on_tag --value off
```

| value | behaviour |
|---|---|
| `off` | never carry a tag into a session; the room stays a noticeboard |
| `direct` | **default** — only a direct `@handle` reaches its session |
| `all` | a `#worktree` tag also reaches every session in that worktree |

Under every value the room refuses to deliver its own `@chat-room` chatter, to echo a message back
into the session that sent it, to overlap a turn already running, or to deliver twice inside 60
seconds. Those four guards are what stop two tagged agents from billing each other in a loop.

## Knowing where things stand

Three questions get harder with every extra agent, and none of them are answerable by
looking at a list of sessions:

```sh
chat-room ready            # which branches merge cleanly into main, and which collide
chat-room targets          # who is where, and how much is uncommitted in each worktree
```

`ready` asks Git for a real merge result per branch rather than guessing from which files
look busy, so a collision is visible before anyone attempts to land. `room_ready` exposes
the same thing to agents.

Presence gained a fourth state for the same reason. A session that asked a question and is
waiting looks exactly like one that finished — both are quiet. A quiet session with an
unanswered question of its own now reports `blocked`, so "who needs me" stops being a
guess. Nothing self-reports being stuck; it is derived from the question still being open.

## Searching inside conversations

Room search covers coordination messages. To search inside the transcripts themselves, install
the optional index once:

```sh
pipx install 'chat-room[index]'      # or: pipx inject chat-room sqlalchemy alembic
chat-room index                       # backfill; re-runs only read what changed
chat-room search --scope chats --query "merge-tree"
```

`room_search` takes the same `scope`. The index stores actors, chats, turns, and reachable
servers; SQLite is the default and needs nothing further. Point `CHAT_ROOM_DATABASE_URL` at a
`postgresql+psycopg://` URL to use Postgres instead.

Chat Room runs without any of this. Every entry point degrades to reading vendor files
directly, so an absent index costs speed and never function.

## Claude Code

Copy and path-adjust [`examples/claude-settings.json`](examples/claude-settings.json) into the appropriate Claude Code settings scope. It labels those sessions as Claude while preserving the same project room and message format.

Existing Codex and Claude transcripts are indexed directly from their local session stores. Only user and assistant text is rendered; tool calls, hidden instructions, and reasoning are omitted. History remains in the vendor-owned files and is never imported into Chat Room’s SQLite database.

`chat-room send` continues dormant Codex or Claude sessions through the installed vendor CLI using its ordinary local configuration and sandbox rules. A session already open in another CLI fails closed unless it exposes a safe live adapter; this avoids concurrently resuming one transcript from two processes. Chat Room passes prompts over stdin rather than process arguments. The transcript then refreshes from the vendor-owned file.

Notification choices are data, not HTML. Inspect or extend the local option index without rebuilding the interface:

```sh
chat-room options
chat-room option-set --namespace worktree_action --key archive \
  --value "Archive" --metadata '{"order":40,"prompt":"Report an archive plan. Do not mutate Git."}'
```

Chat status is intentionally mechanical: **Live** has an observed session now, **Recent** was updated within 7 days, **Stale** is 7–29 days old without a live session, and **Inactive** is at least 30 days old without one. The inactive panel is a review queue; Chat Room does not delete vendor-owned histories.

## Architecture

```text
Codex hooks ─┐
Claude hooks ├── local Python protocol ── SQLite (one logical room per Git project)
MCP tools ───┤              │
Command line ┘              ├── live local chat indexes + CLI delivery adapters
                            ├── durable team + temporary coordination channels
                            └── explicit idle-session wake over Unix socket
```

Room identity derives from the normalized Git remote when present plus the resolved Git common directory. That makes linked worktrees converge without making unrelated clones or projects collide.

The room contains no scheduler and owns no work. Consumers must re-observe repository and provider state before acting.

## Development

Requirements: Python 3.10+, Node 22+, Git.

```sh
cd ~/chat-room
make check
```

The public landing/demo site lives in `app/`; it generates its command and tool reference from `docs/protocol.md` at build time, so a change to the CLI reaches the page without anyone editing it. The distributable Codex plugin is `plugins/chat-room/`.

## Security

Chat Room opens no listening socket. It stores room state locally under `~/.chat-room` at mode `0600`, and reaches a running Codex session only over a private Unix socket that session created. A conservative pattern filter rejects common private keys and API-token shapes before persistence. This is defense in depth, not a general-purpose secret scanner; do not post secrets.

See [SECURITY.md](SECURITY.md) for reporting.

## License

Apache-2.0. See [LICENSE](LICENSE).
