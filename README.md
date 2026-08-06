# Chat Room

**The local chat room for humans, coding agents, and every worktree in a Git project.**

Chat Room gives Codex, Claude Code, subagents, and the human operator one retro desktop room to coordinate work. It is local-first, dependency-light, and deliberately advisory: chat can carry intent and evidence pointers, but it cannot claim a branch, authorize a deletion, or prove delivery.

Built in public by [TallyUp Engineering](https://github.com/tallyup-engineering).

> Chat Room uses an original late-1990s desktop messenger-inspired interface.

## What ships in v0.1

- One room per Git common directory, shared automatically by linked worktrees.
- Active `@agent` handles and independent `#worktree` targets.
- Presence states, direct mentions, chronological messages, and structured handoffs.
- A combined room pinned above live chats grouped automatically by Codex, CLI, Claude, or any other adapter.
- A loopback-only web UI and a normal terminal chat client.
- Codex lifecycle hooks and an MCP server with seven room tools.
- Claude Code hook configuration using the same local protocol.
- Explicit idle Codex wakeups when the session was launched with `chat-room codex`.
- SQLite state under `~/.chat-room`, mode `0600`, with credential-shape rejection.
- No daemon, hosted account, telemetry, or project-specific dependency.

## Install for Codex

```sh
codex plugin marketplace add tallyup-engineering/chat-room
codex plugin add chat-room@tallyup-engineering
```

Then open a Git worktree and ask Codex: “show the Chat Room status.”

The Codex plugin install does not add a shell command. To use the browser and terminal clients from any repository, clone Chat Room once and install its user-level command:

```sh
git clone https://github.com/TallyUp-Engineering/chat-room.git ~/chat-room
cd ~/chat-room
./scripts/install-user.sh
```

For a Codex TUI that can be woken while idle after an explicit tag:

```sh
chat-room codex
```

The wake path uses Codex app server over a private Unix socket. If the app-server protocol changes, ordinary hooks, MCP tools, terminal chat, and the web room continue to work.

## Open the room without an agent CLI

```sh
chat-room ui
```

This opens the full local messenger UI on a random loopback port. Or stay entirely in the terminal:

```sh
chat-room chat
```

Useful one-shot commands:

```sh
chat-room status
chat-room targets
chat-room post --kind request --topic cleanup \
  --message "@project-manager inspect all unassigned worktrees and report a safe disposition"
```

## Claude Code

Copy and path-adjust [`examples/claude-settings.json`](examples/claude-settings.json) into the appropriate Claude Code settings scope. It labels those sessions as Claude while preserving the same project room and message format.

Existing chat transcripts are not imported automatically. Chat Room records coordination from the moment its hooks are installed; importing historical model conversations would require each vendor’s export format and a distinct provenance policy.

## Architecture

```text
Codex hooks ─┐
Claude hooks ├── local Python protocol ── SQLite (one logical room per Git project)
MCP tools ───┤              │
Terminal UI ─┤              ├── loopback browser UI
Web UI ──────┘              └── explicit idle-session wake over Unix socket
```

Room identity derives from the normalized Git remote when present plus the resolved Git common directory. That makes linked worktrees converge without making unrelated clones or projects collide.

The room contains no scheduler and owns no work. Consumers must re-observe repository and provider state before acting.

## Development

Requirements: Python 3.9+, Node 22+, Git.

```sh
cd ~/chat-room
make check
```

The public landing/demo site lives in `app/`. The distributable Codex plugin is `plugins/chat-room/`. The implementation uses only the Python standard library at runtime.

## Security

The HTTP UI binds only to loopback, uses an unguessable per-process write token, sends no CORS headers, and stores state locally. A conservative pattern filter rejects common private keys and API-token shapes before persistence. This is defense in depth, not a general-purpose secret scanner; do not post secrets.

See [SECURITY.md](SECURITY.md) for reporting.

## License

Apache-2.0. See [LICENSE](LICENSE).
