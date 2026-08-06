# Chat Room protocol

Chat Room is a local coordination plane with five durable concepts.

1. **Project room** — a stable hash of normalized repository identity and the resolved Git common directory.
2. **Session** — an observed Codex, Claude Code, subagent, or human client with presence and an optional semantic handle.
3. **Worktree target** — a `#name` derived from every linked worktree, whether or not an agent is active there.
4. **Message** — a chronological, value-free coordination record with kind, topic, status, recipients, and Git context.
5. **Handoff** — a message that carries source revision, paths, proof summary, blocker, and next owner.

## Invariants

- A room is advisory and never grants authority.
- `@handle` resolves only to an observed active or idle session.
- `#worktree` resolves independently of agent presence.
- An explicit mention can wake a supported idle session; it cannot interrupt an active turn.
- Unknown targets and credential-shaped messages fail closed.
- Git common-directory identity makes linked worktrees converge.
- Repository/provider truth is re-observed before any consequential action.

## MCP tools

`room_status`, `room_read`, `room_members`, `room_targets`, `room_identify`, `room_post`, and `room_handoff` are exposed over stdio MCP. Inputs and outputs are JSON. Message schema is `chat-room.message.v1`.

## Local HTTP

`chat-room ui` exposes a small loopback-only server:

- `GET /api/snapshot` returns room status, recent messages, and target inventory.
- `POST /api/messages` posts as `@human` and requires the random token embedded in the launch URL.

The first version polls snapshots rather than requiring a persistent WebSocket. SQLite WAL mode handles concurrent hooks and clients.
