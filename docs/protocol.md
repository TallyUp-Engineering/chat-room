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
| `room_spend` | Token spend per worktree beside the commits it produced. |
| `room_search` | Search room messages, or indexed transcripts with `scope: chats`. |
| `room_handoff` | Post a structured handoff. |

`room_session_start` runs a real vendor CLI session and bills vendor tokens.

## Local HTTP

`chat-room ui` exposes a loopback-only server. It binds only to loopback, accepts only
`localhost` hostnames, sends no CORS headers, and validates the WebSocket origin. **Every
route below requires the unguessable per-process local token**, supplied as the
`chat_room_token` cookie set by `GET /` or an `X-Chat-Room-Token` header.

| Read | Returns |
|---|---|
| `/api/snapshot` | Room status, recent messages, targets, threads, alerts, options. |
| `/api/search` | Room messages matching `?q=`, across the whole history. |
| `/api/chats` | Indexed local CLI conversations for this project. |
| `/api/chat` | One conversation's visible transcript and delivery state. |

| Write | Effect |
|---|---|
| `/api/messages` | Post as `@human`, or into a thread with `thread_id`. |
| `/api/threads` | Open a chatter thread or a human-in-the-loop question. |
| `/api/thread-close` | Resolve or archive a thread. |
| `/api/chat-send` | Continue a local conversation through its vendor CLI. |
| `/api/rename` | Machine-local rename overlay for a room, channel, or chat. |
| `/api/session-start` | Start a new local agent session in a worktree. |
| `/api/session-stop` | Interrupt a turn this room started. |

`GET /` and the static assets it references are the only unauthenticated responses.

## Carrying tags into sessions

`delivery_policy/wake_on_tag` is an indexed option: `off`, `direct` (default), or `all`.
Under every value the room refuses to deliver its own `@chat-room` chatter, to echo a message
into the session that sent it, to overlap a turn already running, or to deliver twice inside
sixty seconds.

## Optional transcript index

With the `index` extra installed, local transcripts are indexed into
**actor**, **chat**, **turn**, and **server** tables and `scope: chats` searches inside them.
SQLite is the default; `CHAT_ROOM_DATABASE_URL` selects Postgres. Chat Room runs without any
of it — every caller degrades to reading vendor files directly.
