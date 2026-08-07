---
name: room-cleanup
description: Interrogate the operator about the house rules a Git project should run under — what counts as a stale worktree, whether one actor per worktree is advisory or binding, how file overlap should land — and record the answers in the room so every agent picks them up. Use when the user says /room-cleanup, "clean up the worktrees", "what are our rules", "set the room rules", or asks what should happen about stale or shared worktrees. Also use when `chat-room rules` reports rules nobody has decided.
---

# Room cleanup

Establish the rules a project runs under, and settle worktrees against them.

This writes nothing to Git and interrupts nobody. Every answer lands in the local option
index, and each worker picks it up on its next hook injection.

## Observe before asking

Run these first. They are cheap and they decide which questions are worth the operator's
attention:

```sh
chat-room rules         # the catalog, the rung each sits at, and what nobody has decided
chat-room alerts        # what the current rules already surface
chat-room projects      # every project and its worktrees
```

**Ask only about rules where `decided` is false.** A rule already answered is settled;
re-asking it is the interruption this skill exists to avoid. If every rule is decided, say
so and go straight to disposition.

## Ask

One question per undecided rule, in the operator's terms, not the schema's. Offer the rungs
as plain choices and say what each would do to their actual room:

- **`stale-worktrees`** — "How long should a worktree sit with no active agent and no branch
  commit before I call it idle?" Then: should that advise, warn, or be treated as binding?
  The threshold is a separate value; ask for a number of days.
- **`one-actor-per-worktree`** — "Two agents are in `#release-train` right now. Is one actor
  per worktree a preference or a rule?" `advise` reports it, `warn` carries it into every
  session, `refuse` states it as binding.
- **`file-overlap`** — "Two worktrees are editing `app/ui.tsx`. Should that just be visible,
  or should it stop work until someone owns it?"

Use the room's real numbers in the question. "Two agents are in `#release-train`" earns an
answer; "how should shared worktrees be handled" does not.

## Record

```sh
chat-room option-set --namespace rules --key one-actor-per-worktree --value refuse
chat-room option-set --namespace notification_policy --key stale-worktree-days --value 14
```

Then read it back with `chat-room rules` and show the operator what changed.

## Settle worktrees

Only after the rules are recorded, and only against them:

```sh
chat-room ready         # which branches still merge into the integration branch
chat-room spend         # what each worktree cost and what it produced
```

Report a disposition per stale worktree — unique unmerged work, merged and disposable, or
unclear. **Never delete a worktree or branch.** The room is advisory; the operator disposes.
Open a `human-loop` thread for anything you cannot settle from observation:

```sh
chat-room thread-open --audience human-loop --title "..." --reason "coordination" --participant @human
```

## Boundaries

- Rules are declared intent. Git stays authoritative — re-observe before acting on one.
- `refuse` binds agents that read the room. It does not block a write, and you must not tell
  the operator it does.
- Never post credentials or command output that carries values into the room.
