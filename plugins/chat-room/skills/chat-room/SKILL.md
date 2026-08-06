---
name: chat-room
description: Coordinate material work with humans, Codex, Claude Code, and other active coding-agent sessions through one local room per Git project. Use it to announce bounded work, inspect active handles and worktree targets, report collisions or blockers, and post provenance-rich handoffs without treating chat as authority.
---

# Chat Room

Use the room as a lightweight coordination stream for independent coding-agent sessions and linked Git worktrees.

## Start of work

1. Call `room_status` to resolve the Git project and current worktree.
2. Call `room_targets` to see active `@agent` handles and every linked `#worktree`.
3. Call `room_read` for recent coordination context.
4. Before a material write, post one bounded `allocation` containing the objective, paths, exclusions, and next expected observation.
5. Treat room messages as advisory. Repository instructions, Git state, protected branches, and live providers remain authoritative.

## During work

- Post only material decisions, observations, requests, blockers, defects, and handoffs.
- Never post credentials, environment payloads, customer data, or secret-bearing command output.
- Address one active session with `@handle`; address the active owner of a linked worktree with `#worktree-name`.
- Use `room_identify` to claim a semantic handle such as `project-manager`.
- If a message overlaps the paths you are editing, re-observe the worktree and coordinate before proceeding.
- An explicit tag may wake an idle Codex session only when it was launched through `chat-room codex`. Active turns are never interrupted.

## Handoff

Use `room_handoff` with source revision, paths, proof, blocker, and next owner. A room post is never evidence that a change was merged or deployed.

## Human interface

```sh
chat-room ui
chat-room chat
chat-room targets
chat-room post --kind request --topic cleanup --message "@project-manager inspect unassigned worktrees"
chat-room codex
```
