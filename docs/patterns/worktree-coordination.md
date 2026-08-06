# Pattern: sessions and worktrees are different things

An agent is ephemeral. A worktree is a durable Git lane. Treating them as the same identity makes coordination ambiguous whenever a session exits or several agents inspect one lane.

Chat Room routes `@project-manager` to one active session and `#release-train` to the active owner or owners of a worktree. Every linked worktree remains addressable even when unassigned, which lets a project manager discuss disposition before destructive cleanup.
