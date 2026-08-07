# Chat Room architecture

The decisions this program is not free to change quietly. Each one names the test that holds
it, because a constraint nobody checks is a preference, and preferences erode.

`docs/protocol.md` describes the surface. This describes the shape underneath it.

## Constraints

| # | Constraint | Enforced by |
|---|---|---|
| 1 | The program runs on the Python standard library alone. Third-party code may only appear behind the optional transcript index, which every caller degrades without. `pipx install chat-room` resolves no wheels and needs no compiler. | `test_the_room_imports_nothing_outside_the_standard_library` |
| 2 | The room opens no listening socket. It reaches a running Codex session only over a Unix socket that session created. | `test_the_room_never_listens_on_a_socket` |
| 3 | The hook path stays cheap. Context injection runs on every prompt, so it may not shell out to Git for merge analysis; expensive questions belong in an explicitly invoked command. | `test_the_hook_path_never_runs_a_merge_analysis` |
| 4 | `docs/protocol.md` matches the code. Every MCP tool and every subcommand is documented, and nothing is documented that does not exist. | `test_the_protocol_document_matches_the_code` |
| 5 | The README only promises commands that exist. | `test_the_readme_only_promises_commands_that_exist` |
| 6 | The wheel carries every non-Python file the repository ships, so an installed copy behaves like a checkout. | `test_every_shipped_file_is_covered_by_the_package_data_globs` |
| 7 | Shared tables are never written positionally. Several versions of this program may share one database on a machine, and a positional insert couples every writer to the exact column count. | `test_shared_tables_are_never_written_positionally` |
| 8 | The schema record never moves backwards, and a copy whose schema is behind the database refuses to write while continuing to read. | `test_the_schema_record_never_moves_backwards`, `test_a_copy_behind_the_database_reads_but_refuses_to_write` |
| 9 | The room is advisory. It never mutates Git, and a rule at `refuse` denies a write through the host CLI's own permission decision rather than by acting on the repository. | `test_only_a_refused_rule_denies_a_write` |
| 10 | Nothing the room does can stop work when the room itself is broken. Every hook failure mode returns `continue`. | `test_the_hook_fails_open_when_the_room_is_unreadable` |
| 11 | Bounded coverage is reported. A scan that stops early says so, because a conflict detector that quietly stops looking reads as "no conflicts". | `test_a_truncated_conflict_scan_says_so` |
| 12 | A value-free filter rejects credential shapes before anything is persisted. | `test_value_free_filter` |
| 13 | Every user-facing read runs as a real process against a real Git project, not only against the store in-process. A bug that depends on the process exiting is invisible to any test that *is* that process. | `test_the_process_boundary_covers_every_subcommand`, `test_a_fresh_process_reports_the_conflict_it_finds` |
| 17 | An actor is a row in a table, not a branch in the code. Every vendor CLI answers the same questions — how to start a turn, how to resume one, how to carry an image, whether it exposes a channel that can be woken in place — so adding one is data. | `test_an_actor_is_a_row_in_a_table_not_a_branch_in_the_code`, `test_every_actor_builds_a_turn_the_same_way` |
| 16 | Nothing reaches PyPI that has not first been published to TestPyPI and installed back from that index. Publishing is irreversible and yanking is deliberately manual — Warehouse's yank route is UI-only and requires re-authentication — so the last check has to come before the irreversible step. | `.github/workflows/package.yml` (`rehearse` → `verify` → `publish`) |
| 15 | Every bounded scan reports its own coverage. A cap that stops early without saying so turns a partial answer into a false clean bill of health, and both scanners are capped. | `test_a_truncated_conflict_scan_says_so`, `test_merge_readiness_says_when_it_only_looked_at_some_of_the_pairs` |
| 14 | Bounded work is reported by the caller, not assumed by it. `spend` is the one read that cannot degrade without the optional index, and says so. | `test_spend_says_what_it_needs_when_the_index_is_absent` |

## Why this file is checked too

`test_every_architecture_constraint_names_a_test_that_exists` parses the table above and fails
when a row names a test that is gone. One row names a workflow rather than a test, because a
release pipeline cannot be asserted from inside the suite it releases. Deleting the enforcement without deleting the claim is
exactly how a document like this becomes decoration.
