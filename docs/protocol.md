# Chat Room protocol

Chat Room is a local coordination plane with five durable concepts.

1. **Project room** — a stable hash of normalized repository identity and the resolved Git common directory.
2. **Session** — an observed Codex, Claude Code, subagent, or human client with presence and an optional semantic handle.
3. **Worktree target** — a `#name` derived from every linked worktree, whether or not an agent is active there.
4. **Message** — a chronological, value-free coordination record with kind, topic, status, recipients, and Git context.
5. **Handoff** — a message that carries source revision, paths, proof summary, blocker, and next owner.

## Invariants

- A room is advisory and never grants authority.
- `@handle` resolves only to an observed active or idle session, and is settled when presence is written so a target never migrates between sessions.
- A session is `online`, `idle`, `blocked`, or `offline`. `blocked` is derived, not declared: a quiet session that opened a question still waiting on a human. Nothing self-reports being stuck.
- Merge safety is reported before a merge is attempted, never inferred from whether files look similar.
- `#worktree` resolves independently of agent presence.
- An explicit mention can carry a message into a supported idle session; it cannot interrupt an active turn.
- Unknown targets and credential-shaped messages fail closed.
- Git common-directory identity makes linked worktrees converge.
- Repository/provider truth is re-observed before any consequential action.
- Shared tables are never widened in place, and every write names its columns, because several versions of the program may share one database on a machine.
- A copy whose schema is behind the database refuses to write and says so; reads stay available.

## MCP tools

Exposed over stdio MCP. Inputs and outputs are JSON; the message schema is `chat-room.message.v1`.

| Tool | Purpose |
|---|---|
| `room_status` | Room and repository identity. |
| `room_read` | Chronological room messages. |
| `room_members` | Observed agent sessions and presence. |
| `room_targets` | Active `@agent` and `#worktree` targets. |
| `room_options` | Indexed notification and delivery options. |
| `room_option_set` | Add or update one machine-local indexed option. |
| `room_threads` | Open manual and merge-conflict coordination threads. |
| `room_thread_open` | Open agent chatter or a human-in-the-loop question. |
| `room_thread_close` | Mark a thread resolved without changing Git state. |
| `room_identify` | Assign an active session a semantic `@handle`. |
| `room_post` | Post one value-free coordination message. |
| `room_session_start` | Open new agent work in a worktree of this project. |
| `room_session_stop` | Interrupt a local turn this room started. |
| `room_ready` | Which worktree branches merge cleanly into the integration branch, and which collide. |
| `room_projects` | Every project with a room on this machine, worktrees grouped under it. |
| `room_spend` | Token spend per worktree beside the commits it produced. Needs the optional index. |
| `room_search` | Search room messages, or indexed transcripts with `scope: chats`. |
| `room_handoff` | Post a structured handoff. |

`room_session_start` runs a real vendor CLI session and bills vendor tokens.

## Command line

The `chat-room` command is the whole human surface. Every subcommand prints JSON except
`board`, which renders columns, `chat` and `codex`, which are interactive, and `hook`/`mcp`,
which speak to a host program.
Each accepts `--cwd` to name the worktree it acts in.

| Read | Returns |
|---|---|
| `chat-room status` | Room and repository identity. |
| `chat-room read` | Chronological room messages from `--after-id`. |
| `chat-room search` | Room messages, or transcripts with `--scope chats`. |
| `chat-room members` | Observed sessions and presence. |
| `chat-room targets` | Active `@agent` and `#worktree` targets. |
| `chat-room threads` | Open manual and merge-conflict coordination threads. |
| `chat-room alerts` | Shared worktrees, overlaps, decisions, and stale lanes, at whatever height the rules put them. |
| `chat-room rules` | Every house rule, the rung it sits at, and whether anyone has decided it. |
| `chat-room chats` | Local CLI conversations discovered for this project. |
| `chat-room board` | Coordination work as columns; the only subcommand that renders rather than prints JSON. |
| `chat-room ready` | Which worktree branches merge cleanly into `--into`, and which collide. |
| `chat-room projects` | Every project with a room on this machine, worktrees grouped under it. |
| `chat-room spend` | Token spend per worktree beside the commits it produced. Needs the optional index. |
| `chat-room options` | Indexed notification and delivery options. |
| `chat-room doctor` | Why the room is broken, with `--repair`. |

| Write | Effect |
|---|---|
| `chat-room post` | Post one value-free message as `--sender`. |
| `chat-room chat` | Interactive terminal session in the room. |
| `chat-room thread-open` | Open agent chatter or a human-in-the-loop question. |
| `chat-room thread-close` | Resolve or archive a thread. |
| `chat-room rename` | Machine-local rename overlay for a room, channel, or chat. |
| `chat-room identify` | Assign an active session a semantic `@handle`. |
| `chat-room option-set` | Add or update one machine-local indexed option. |
| `chat-room start` | Open new agent work in a worktree of this project. |
| `chat-room send` | Continue a stored conversation through its vendor CLI. |
| `chat-room stop` | Interrupt a local turn this room started. |
| `chat-room index` | Backfill the optional transcript index. |
| `chat-room warm` | Fill the merge memo so `ready` is fast. One per room at a time. |
| `chat-room codex` | Run Codex with a wake endpoint this room can reach. |
| `chat-room hook` | Emit coordination context to a host CLI. |
| `chat-room mcp` | Serve the MCP tools above over stdio. |

`chat-room start` and `chat-room send` run real vendor CLI turns and bill vendor tokens.
`--image` on `send` references files already on this machine; they are never copied.

## House rules

A rule names a condition the room can already observe. Its value is the **rung**, and
nothing else in the room decides how loudly the condition lands.

| Rung | Effect |
|---|---|
| `off` | the condition is not reported at all |
| `advise` | reported in `chat-room alerts`; the default for every rule |
| `warn` | reported, and carried into every session's injected context |
| `refuse` | reported, carried, and stated as binding |

Rules live in the `rules` option namespace, so `chat-room option-set --namespace rules`
sets one and no separate write command exists. A rule nobody has set reports its default
and `decided: false`; the difference between a default and an answer is what lets an
interrogation ask only what is still open.

`refuse` is the only rung that can interrupt a turn. A rule at that height is evaluated in a
`PreToolUse` hook and answers with a `deny` decision, so the write does not happen. Only rules
answerable from presence are checked there, because it runs before every write; anything that
shells out to Git belongs in `chat-room ready`.

The hook fails open. A room that is unreachable, unreadable, or outside a Git worktree returns
`{"continue": true}` and never a denial — a broken room must not be able to stop work. The
room still holds no authority over Git: it refuses a write through the host CLI's own
permission decision, and cannot undo one that already happened.

## The board

`chat-room board` groups coordination threads into `backlog`, `doing`, `blocked`, and `done`.
No column is stored. A thread is already a card, and where it sits follows from what the room
observes: `done` is a resolved or archived thread, `blocked` is one waiting on a human, `doing`
has an active participant, and `backlog` has none. A worker moves a card by doing the work.

Reading the board touches no session and starts no turn.

Every rule is evaluated from cheap, cached observations, because they run on the hook path.
The expensive question — does this branch still merge into the integration branch — stays in
`chat-room ready`, where a human asked for it and can wait.

## Warming

`chat-room ready` asks Git about every branch pair that shares a changed file. Whether two
commits conflict is a fixed fact, so each answer is memoised against the pair of commits —
a key that changes the moment either branch moves, which is what lets the memo live without
an expiry.

The first sweep of a large project is slow and every one after it is not. `chat-room warm`
does that sweep on purpose and reports progress. The first human-facing read of a room whose
memo is empty also starts one in the background, says so on stderr, and never does it again:

```sh
chat-room option-set --namespace warm --key in-background --value off
```

Only one warmer runs per room. It claims the lock itself rather than being handed one, and
its output goes to `warm.log` beside the room, because a detached process that fails
silently is indistinguishable from one that never started.

## Carrying tags into sessions

`delivery_policy/wake-on-tag` is an indexed option: `off`, `direct` (default), or `all`.
Under every value the room refuses to deliver its own `@chat-room` chatter, to echo a message
into the session that sent it, to overlap a turn already running, or to deliver twice inside
sixty seconds.

## Optional transcript index

With the `index` extra installed, local transcripts are indexed into
**actor**, **chat**, **turn**, and **server** tables and `scope: chats` searches inside them.
SQLite is the default; `CHAT_ROOM_DATABASE_URL` selects Postgres. Chat Room runs without any
of it — every caller degrades to reading vendor files directly, with one exception:
`chat-room spend` has no token figures without the index and says so rather than guessing.
