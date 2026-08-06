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
4. Call `room_threads` and join an existing path-overlap or decision thread when one applies.
5. Before a material write, post one bounded `allocation` containing the objective, paths, exclusions, and next expected observation.
6. Treat room messages as advisory. Repository instructions, Git state, protected branches, and live providers remain authoritative.

## During work

- Post only material decisions, observations, requests, blockers, defects, and handoffs.
- Never post credentials, environment payloads, customer data, or secret-bearing command output.
- Address one active session with `@handle`; address the active owner of a linked worktree with `#worktree-name`.
- Use `room_identify` to claim a semantic handle such as `project-manager`.
- If a message overlaps the paths you are editing, re-observe the worktree and coordinate before proceeding.
- Preemptive file-overlap rooms automatically tag `@human`, the involved worktree targets, and active workers in those worktrees. Reply with ownership and sequencing before another write.
- Use `room_thread_open` for a design decision, review, handoff, blocker, or proactive conflict. Include every involved `@actor`, `#worktree`, and path.
- Post with the returned `thread_id` when the central reference applies. Direct `@actor` and `#worktree` tags remain valid for ad hoc coordination.
- Use `room_thread_close` only when the coordination question is resolved; it never changes Git state.
- An explicit tag may wake an idle Codex session only when it was launched through `chat-room codex`. Active turns are never interrupted.

## Handoff

Use `room_handoff` with source revision, paths, proof, blocker, and next owner. A room post is never evidence that a change was merged or deployed.

## Human interface

```sh
chat-room ui
chat-room chat
chat-room targets
chat-room threads
chat-room options
chat-room thread-open --title "Choose navigation direction" --reason "design direction" --participant @human --participant @ui-agent
chat-room post --kind request --topic cleanup --message "@project-manager investigate unassigned worktrees and report unique unmerged work"
chat-room codex
chat-room service install --cwd .
```
