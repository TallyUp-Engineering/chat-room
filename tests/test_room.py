import ast
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "plugins" / "chat-room" / "scripts" / "room.py"
SPEC = importlib.util.spec_from_file_location("chat_room", MODULE)
room = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = room
SPEC.loader.exec_module(room)

# The transcript index is optional; its tests skip where it is not installed.
CHAT_INDEX = room.load_chat_index()


class RoomTests(unittest.TestCase):
    def repo(self):
        return room.Repository(Path("/project/lane"), Path("/project/lane"), Path("/project/.git"), "git@github.com:acme/project.git", "git:github.com/acme/project", "room-a", "lane", "a" * 40)

    def test_remote_normalization_is_host_agnostic(self):
        self.assertEqual(room.normalize_remote("git@github.com:acme/project.git"), ("github.com", "acme/project"))
        self.assertEqual(room.normalize_remote("https://code.example.test/acme/project.git"), ("code.example.test", "acme/project"))

    def test_repository_uses_first_remote_when_origin_is_absent(self):
        def fake_git(_cwd, *args, **_kwargs):
            values = {
                ("rev-parse", "--show-toplevel"): "/project",
                ("rev-parse", "--path-format=absolute", "--git-common-dir"): "/project/.git",
                ("remote", "get-url", "origin"): "",
                ("remote",): "upstream",
                ("remote", "get-url", "upstream"): "git@code.example.test:acme/project.git",
                ("branch", "--show-current"): "main",
                ("rev-parse", "HEAD"): "a" * 40,
            }
            return values[args]
        with mock.patch.object(room, "run_git", side_effect=fake_git):
            repo = room.resolve_repository("/project")
        self.assertIsNotNone(repo)
        self.assertEqual(repo.project_identity, "git:code.example.test/acme/project")

    def test_value_free_filter(self):
        with self.assertRaises(room.RoomError): room.ensure_value_free("access_token=abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(room.ensure_value_free("provider observation pending"), "provider observation pending")

    def test_mentions_accept_natural_spacing(self):
        self.assertEqual(room.mentioned_targets("@ project-manager inspect #lane-one"), ["@project-manager", "#lane-one"])

    def test_sqlite_post_presence_and_handoff(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            store.upsert_presence(repo, "session:one", "one", None, "codex:lane", "online", "SessionStart")
            member = store.claim_handle(repo, "one", "project-manager")
            self.assertEqual(member["target"], "@project-manager")
            with mock.patch.object(room, "list_worktree_references", return_value=[{"target":"#lane","name":"lane","path":str(repo.worktree),"branch":"lane"}]):
                message = store.post(repo, "@human", "allocation", "cleanup", "posted", "@ project-manager inspect #lane", [])
            self.assertEqual(message["recipients"], ["@project-manager", "#lane"])
            self.assertEqual(store.read(repo.room_id)[0]["schema"], "chat-room.message.v1")
            self.assertEqual(store.status(repo)["authority"], "advisory-only")

    def test_unknown_active_handle_is_rejected(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            with mock.patch.object(room, "list_worktree_references", return_value=[]):
                with self.assertRaises(room.RoomError): store.post(repo, "@human", "allocation", "test", "posted", "@missing hello", [])

    def test_duplicate_default_handles_get_stable_unique_targets(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            store.upsert_presence(repo, "session:one", "one", None, "claude:lane", "online", "SessionStart")
            store.upsert_presence(repo, "session:two", "two", None, "claude:lane", "online", "SessionStart")
            targets = [item["target"] for item in store.members(repo.room_id) if item["state"] == "online"]
            self.assertEqual(len(targets), len(set(targets)))
            self.assertTrue(any(target.startswith("@claude-lane-") for target in targets))

    def test_hook_fails_open_outside_git(self):
        with mock.patch.object(room, "resolve_repository", return_value=None), mock.patch("sys.stdin") as stdin, mock.patch("sys.stdout") as stdout:
            stdin.__iter__.return_value = iter([]); stdin.read.return_value = ""

    def test_terminal_chat_registers_and_releases_cli_presence(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            with mock.patch("sys.stdin", io.StringIO("")), mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(room.run_chat(store, repo, "@human"), 0)
            member = next(item for item in store.members(repo.room_id) if item["role"].startswith("cli:"))
            self.assertEqual(member["state"], "offline")
            self.assertEqual(member["last_event"], "ChatEnd")

    def test_manual_thread_routes_by_central_reference(self):
        repo = self.repo()
        worktrees = [{"target": "#lane", "name": "lane", "path": str(repo.worktree), "branch": "lane"}]
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store, mock.patch.object(room, "list_worktree_references", return_value=worktrees):
            thread = store.open_thread(repo, "Choose the interface direction", "design direction", "@human", ["#lane"], ["app/ui.tsx"])
            posted = store.post_thread(repo, thread["id"], "@human", "@human should the navigation be vertical?")
            self.assertEqual(posted["topic"], f"thread:{thread['id']}")
            self.assertEqual(posted["metadata"]["thread_id"], thread["id"])
            self.assertEqual(posted["recipients"], ["#lane", "@human"])
            self.assertEqual(thread["lifetime"], "durable")
            self.assertEqual(store.close_thread(repo, thread["id"])["status"], "archived")

    def test_temporary_channel_resolves_and_channel_rename_is_local(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store, mock.patch.object(room, "list_worktree_references", return_value=[]):
            thread = store.open_thread(repo, "Short-lived settlement", "coordination", "@human", ["@human"], [], "temporary-channel")
            self.assertEqual(thread["lifetime"], "temporary")
            self.assertEqual(store.rename(repo, "channel", thread["id"], "Settled direction")["label"], "Settled direction")
            self.assertEqual(store.thread(repo, thread["id"])["title"], "Settled direction")
            self.assertEqual(store.close_thread(repo, thread["id"])["status"], "resolved")

    def test_preemptive_overlap_opens_thread_without_changing_git(self):
        repo = self.repo()
        worktrees = [
            {"target": "#lane-one", "name": "lane-one", "path": "/project/one", "branch": "one"},
            {"target": "#lane-two", "name": "lane-two", "path": "/project/two", "branch": "two"},
        ]
        conflicts = [{"path": "app/ui.tsx", "worktrees": worktrees}, {"path": "app/theme.css", "worktrees": worktrees}]
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store, mock.patch.object(room, "list_worktree_references", return_value=worktrees), mock.patch.dict(room.CONFLICT_SCANS, {repo.room_id: (room.time.monotonic(), conflicts)}, clear=True):
            threads = store.sync_preemptive_conflicts(repo)
            self.assertEqual(len(threads), 1)
            self.assertEqual(threads[0]["paths"], ["app/theme.css", "app/ui.tsx"])
            self.assertEqual(threads[0]["source"], "preemptive-conflict")
            self.assertEqual(threads[0]["participants"], ["#lane-one", "#lane-two"])
            self.assertEqual(threads[0]["audience"], "agents")
            prompt = store.read(repo.room_id)[0]
            self.assertIn("#lane-one #lane-two", prompt["message"])
            self.assertNotIn("@human", prompt["message"])
            self.assertIn("these branches conflict on", prompt["message"])

    def repo_with_branches(self, branches):
        """A real repository where each named branch changes one file from a common base."""
        temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp, True)
        root = Path(temp) / "project"
        root.mkdir()

        def git(*args):
            subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        git("init", "-q", "-b", "base")
        git("config", "user.email", "test@example.test")
        git("config", "user.name", "Test")
        (root / "tools.txt").write_text("base\n")
        (root / "other.txt").write_text("base\n")
        git("add", "."); git("commit", "-qm", "base")
        for name, (path, content) in branches.items():
            git("checkout", "-q", "base")
            git("checkout", "-qb", name)
            (root / path).write_text(content)
            git("commit", "-qam", name)
        git("checkout", "-q", "base")
        return root

    def test_projects_groups_worktrees_and_flags_a_room_whose_checkout_is_gone(self):
        # One room covers one project, so two projects mean two rooms. A room whose checkout
        # moved is reported, not dropped — a stale room is invisible until it confuses someone.
        root = self.repo_with_branches({"lane": ("other.txt", "base\nlane\n")})
        repo = room.resolve_repository(str(root))
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            store.register_room(repo)
            store.connection.execute(
                "INSERT INTO rooms(room_id,project_identity,common_dir,repository_root,created_at,last_seen_at)"
                " VALUES('gone','git:example.test/removed','/gone/.git','/gone',?,?)",
                (room.utc_now(), room.utc_now()))
            store.connection.commit()
            with mock.patch.object(room, "list_worktree_references", return_value=[{"target": "#lane", "name": "lane", "path": str(root), "branch": "lane"}]):
                report = store.projects(repo)

        by_project = {item["project"]: item for item in report["projects"]}
        self.assertEqual(report["reachable"], 1)
        live = by_project[repo.project_identity]
        self.assertTrue(live["current"] and live["reachable"])
        self.assertEqual([w["target"] for w in live["worktrees"]], ["#lane"])
        missing = by_project["git:example.test/removed"]
        self.assertFalse(missing["reachable"])
        self.assertEqual(missing["worktrees"], [])

    def test_readiness_reports_branches_that_only_collide_with_each_other(self):
        # Two branches can each merge cleanly into the target and still collide the moment
        # either one lands — two additions at the same place read as independent until they
        # are not. Reporting only "clean against the target" hides that until it is too late.
        root = self.repo_with_branches({
            "add-left": ("tools.txt", "base\nleft addition\n"),
            "add-right": ("tools.txt", "base\nright addition\n"),
            "elsewhere": ("other.txt", "base\nunrelated\n"),
        })
        repo = room.resolve_repository(str(root))
        worktrees = [{"target": "#" + name, "name": name, "path": str(root), "branch": name}
                     for name in ("add-left", "add-right", "elsewhere")]
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store, \
                mock.patch.object(room, "list_worktree_references", return_value=worktrees):
            report = store.merge_readiness(repo, "base")

        found = {item["branch"]: item for item in report["branches"]}
        # Each merges cleanly into the target on its own.
        self.assertTrue(all(item["merges_cleanly"] for item in report["branches"]))
        # The two editing the same line collide with each other, and name the file.
        self.assertEqual([c["branch"] for c in found["add-left"]["collides_with"]], ["add-right"])
        self.assertEqual([c["branch"] for c in found["add-right"]["collides_with"]], ["add-left"])
        self.assertEqual(found["add-left"]["collides_with"][0]["paths"], ["tools.txt"])
        # A branch touching an unrelated file is not dragged in.
        self.assertEqual(found["elsewhere"]["collides_with"], [])
        self.assertEqual((report["clean"], report["latent"], report["conflicted"]), (1, 2, 0))

    def test_a_single_short_lived_process_opens_the_conflict_thread(self):
        """The regression this replaces: the scan was asynchronous and the sync step returned
        early unless a scan had already run. Every CLI invocation is a fresh process and so
        always the first caller, which meant overlaps were detected and then thrown away."""
        repo = self.repo()
        worktrees = [
            {"target": "#lane-one", "name": "lane-one", "path": "/project/one", "branch": "one"},
            {"target": "#lane-two", "name": "lane-two", "path": "/project/two", "branch": "two"},
        ]
        conflicts = [{"path": "app/ui.tsx", "worktrees": worktrees}]
        room.CONFLICT_SCANS.clear()
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store, \
             mock.patch.object(room, "list_worktree_references", return_value=worktrees), \
             mock.patch.object(room, "preemptive_conflicts", return_value=conflicts):
            # No prior scan in this process, exactly like a fresh `chat-room threads`.
            threads = store.sync_preemptive_conflicts(repo)
        opened = [item for item in threads if item["source"] == "preemptive-conflict"]
        self.assertEqual(len(opened), 1, "a fresh process did not open the conflict thread")
        self.assertEqual(opened[0]["paths"], ["app/ui.tsx"])
        room.CONFLICT_SCANS.clear()

    def test_only_real_merge_conflicts_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()

            def git(*args):
                subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            git("init", "-q", "-b", "base")
            git("config", "user.email", "test@example.test")
            git("config", "user.name", "Test")
            (root / "shared.txt").write_text("original\n")
            (root / "untouched.txt").write_text("stable\n")
            git("add", "."); git("commit", "-qm", "base")
            git("checkout", "-qb", "one")
            (root / "shared.txt").write_text("one edits this line\n")
            git("commit", "-qam", "one")
            git("checkout", "-q", "base"); git("checkout", "-qb", "two")
            (root / "shared.txt").write_text("two edits the same line\n")
            git("commit", "-qam", "two")
            git("checkout", "-q", "base"); git("checkout", "-qb", "three")
            (root / "untouched.txt").write_text("three edits a different file\n")
            git("commit", "-qam", "three")

            repo = room.Repository(root, root, root / ".git", "", "git-local:test", "room-merge", "base", "a" * 40)
            # Both branches rewrote the same line, so Git reports the collision.
            self.assertEqual(room.merge_conflict_paths(repo, "one", "two"), {"shared.txt"})
            # Separate files merge cleanly, which is the ordinary parallel-worktree case.
            self.assertEqual(room.merge_conflict_paths(repo, "one", "three"), set())
            self.assertEqual(room.merge_conflict_paths(repo, "one", "one"), set())
            self.assertEqual(room.merge_conflict_paths(repo, "one", "no-such-branch"), set())

    def test_a_copy_behind_the_database_reads_but_refuses_to_write(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp:
            with room.RoomStore(Path(temp)) as store:
                store.upsert_presence(repo, "session:one", "one", None, "claude:lane", "online", "SessionStart")
                with mock.patch.object(room, "list_worktree_references", return_value=[]):
                    store.post(repo, "@human", "message", "general", "posted", "written by the newer copy", [])
            # Stand in for a newer chat-room having owned this database first.
            ahead = sqlite3.connect(str(Path(temp) / "chat-room.sqlite3"))
            ahead.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)", (str(room.SCHEMA_VERSION + 1),))
            ahead.commit(); ahead.close()
            with room.RoomStore(Path(temp)) as store:
                self.assertTrue(store.behind)
                # Reading is always safe.
                self.assertEqual([item["message"] for item in store.recent(repo.room_id)], ["written by the newer copy"])
                self.assertEqual(len(store.members(repo.room_id)), 1)
                # Writing would put back a shape the database has moved past.
                for write in (
                    lambda: store.upsert_presence(repo, "session:two", "two", None, "claude:lane", "online", "SessionStart"),
                    lambda: store.post(repo, "@human", "message", "general", "posted", "nope", []),
                    lambda: store.set_option("delivery_policy", "wake_on_tag", "off"),
                ):
                    with self.assertRaises(room.RoomError):
                        write()
                # Heartbeats go quiet rather than raising, so context injection still works.
                store.advance_cursor(repo.room_id, "session:one", 1)
                store.register_room(repo)

    def test_the_schema_record_never_moves_backwards(self):
        with tempfile.TemporaryDirectory() as temp:
            with room.RoomStore(Path(temp)) as store:
                self.assertEqual(store.installed_schema(), room.SCHEMA_VERSION)
            # An older copy of this script opening the same database must not rewind the record,
            # which is how a stale binary silently re-enables migrations that already ran.
            with mock.patch.object(room, "SCHEMA_VERSION", room.SCHEMA_VERSION - 1):
                with room.RoomStore(Path(temp)) as older:
                    self.assertTrue(older.behind)
            with room.RoomStore(Path(temp)) as store:
                self.assertEqual(store.installed_schema(), room.SCHEMA_VERSION)
                self.assertFalse(store.behind)

    def test_doctor_names_column_drift_and_repairs_it(self):
        with tempfile.TemporaryDirectory() as temp:
            with room.RoomStore(Path(temp)):
                pass
            widened = sqlite3.connect(str(Path(temp) / "chat-room.sqlite3"))
            widened.execute("ALTER TABLE presence ADD COLUMN claimed INTEGER NOT NULL DEFAULT 0")
            widened.commit(); widened.close()

            with mock.patch.object(room, "find_cli_executable", return_value=None):
                report = room.diagnose(Path(temp))
                self.assertEqual(report["column_drift"]["presence"]["unexpected"], ["claimed"])
                self.assertTrue(any(item["severity"] == "critical" for item in report["findings"]))
                repaired = room.diagnose(Path(temp), repair=True)
            self.assertEqual(repaired["column_drift"], {})
            self.assertEqual([item for item in repaired["findings"] if item["severity"] == "critical"], [])

    def test_the_room_works_when_the_optional_index_is_absent(self):
        # The index is an accelerator. Without it every entry point must still answer.
        with mock.patch.dict(sys.modules, {"chat_index": None}):
            with self.assertRaises(room.RoomError) as absent:
                room.search_transcripts(Path("/tmp"), "anything")
            self.assertIn("chat-room[index]", str(absent.exception))

    @unittest.skipUnless(CHAT_INDEX, "optional transcript index not installed")
    def test_transcript_index_backfills_incrementally_and_searches_inside_turns(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "session.jsonl"
            source.write_text("placeholder\n")
            transcript = {
                "chat": {"client": "Claude", "id": "session-one", "title": "Rebuild the projection", "cwd": temp, "worktree": "lane", "updated_at": "2026-08-06T19:00:00Z"},
                "messages": [
                    {"role": "user", "body": "please run the merge-tree check", "timestamp": "2026-08-06T19:00:00Z"},
                    {"role": "assistant", "body": "the branches merge cleanly", "timestamp": "2026-08-06T19:00:05Z"},
                ],
                "source": str(source),
            }
            engine = CHAT_INDEX.build_engine(Path(temp))
            # Windows will not remove a directory whose file is still open, and the
            # connection pool holds it until the engine is disposed. This has to happen
            # before the temporary directory is removed, so it cannot be an addCleanup.
            try:
                self.assertEqual(CHAT_INDEX.backfill(engine, [transcript]), {"indexed": 1, "skipped": 0})
                # An unchanged source is never re-read.
                self.assertEqual(CHAT_INDEX.backfill(engine, [transcript]), {"indexed": 0, "skipped": 1})

                counts = CHAT_INDEX.summary(engine)
                self.assertEqual((counts["actors"], counts["chats"], counts["turns"]), (1, 1, 2))

                found = CHAT_INDEX.search_turns(engine, "merge-tree")
                self.assertEqual([item["body"] for item in found], ["please run the merge-tree check"])
                self.assertEqual(found[0]["client"], "claude")
                self.assertEqual(found[0]["title"], "Rebuild the projection")
                # A wildcard is matched literally rather than widening the search.
                self.assertEqual(CHAT_INDEX.search_turns(engine, "%"), [])
                self.assertEqual(CHAT_INDEX.search_turns(engine, "   "), [])

                # A rewritten transcript must not leave stale turns behind.
                source.write_text("placeholder rewritten\n")
                transcript["messages"] = [{"role": "user", "body": "different question", "timestamp": "2026-08-06T20:00:00Z"}]
                self.assertEqual(CHAT_INDEX.backfill(engine, [transcript]), {"indexed": 1, "skipped": 0})
                self.assertEqual(CHAT_INDEX.summary(engine)["turns"], 1)
                self.assertEqual(CHAT_INDEX.search_turns(engine, "merge-tree"), [])
            finally:
                engine.dispose()

    @unittest.skipUnless(CHAT_INDEX, "optional transcript index not installed")
    def test_index_reports_an_unreachable_store_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(os.environ, {CHAT_INDEX.DATABASE_URL_ENV: "postgresql+psycopg://someone:secret@127.0.0.1:59999/nope"}):
                with self.assertRaises(CHAT_INDEX.IndexUnavailable) as unreachable:
                    CHAT_INDEX.build_engine(Path(temp))
            # The message locates the store without repeating the credentials in it.
            self.assertNotIn("secret", str(unreachable.exception))

    # --- Architecture constraints (docs/architecture.md) --------------------------

    def test_every_architecture_constraint_names_a_test_that_exists(self):
        """A constraint nobody checks is a preference. Keep the document honest."""
        doc = (MODULE.parents[3] / "docs" / "architecture.md").read_text(encoding="utf-8")
        named = set(re.findall(r"`(test_[a-z0-9_]+)`", doc))
        # A row may name a workflow instead, where the enforcement is the pipeline itself.
        self.assertIn(".github/workflows/package.yml", doc)
        self.assertGreaterEqual(len(named), 10, "architecture.md lists suspiciously few constraints")
        suites = (MODULE.parents[3] / "tests")
        defined = set()
        for suite in sorted(suites.glob("test_*.py")):
            defined |= set(re.findall(r"def (test_[a-z0-9_]+)", suite.read_text(encoding="utf-8")))
        self.assertEqual(named - defined, set(), "architecture.md names tests that no longer exist")

    def test_every_named_option_key_survives_slugging(self):
        """Writes slug the key and reads slug the key, so a literal that is not slug-stable
        is written to one row and read from another. That is how the seeded delivery policy
        became unreadable and how the stale threshold silently kept its default."""
        for key in room.OPTION_KEYS:
            self.assertEqual(room.slug(key), key, f"option key {key!r} is not slug-stable")
        for name, *_rest in room.RULE_CATALOG:
            self.assertEqual(room.slug(name), name, f"rule name {name!r} is not slug-stable")

    def test_a_stranded_option_row_never_outranks_the_one_that_can_be_read(self):
        """Only the seed ever wrote an unslugged key; `option-set` always slugged.

        So a stranded row is a dead default, and carrying it across must not clobber the
        value an operator actually set. Where there is nothing to clobber, it carries.
        """
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp:
            with room.RoomStore(Path(temp)) as store:
                store.register_room(repo)
                store.set_option(room.NS_DELIVERY, room.KEY_WAKE_ON_TAG, "off")   # the operator decided
                store.connection.execute(                                          # the old dead seed
                    "INSERT OR REPLACE INTO option_index(namespace,key,value,metadata_json) VALUES(?,?,?,?)",
                    (room.NS_DELIVERY, "wake_on_tag", "direct", "{}"))
                store.connection.execute(                                          # stranded with no counterpart
                    "INSERT OR REPLACE INTO option_index(namespace,key,value,metadata_json) VALUES(?,?,?,?)",
                    (room.NS_NOTIFICATION, "stale_worktree_days", "14", "{}"))
                store.connection.commit()
            with room.RoomStore(Path(temp)) as store:
                delivery = {item["key"]: item["value"] for item in store.options()[room.NS_DELIVERY]}
                notification = {item["key"]: item["value"] for item in store.options()[room.NS_NOTIFICATION]}
                self.assertEqual(store.rules()["stale-worktrees"]["parameter"], 14)
        self.assertNotIn("wake_on_tag", delivery, "the unreachable row survived")
        self.assertEqual(delivery[room.KEY_WAKE_ON_TAG], "off", "a dead seed overwrote the operator's choice")
        self.assertNotIn("stale_worktree_days", notification)
        self.assertEqual(notification[room.KEY_STALE_WORKTREE_DAYS], "14", "a stranded value was dropped instead of carried")

    def test_the_room_imports_nothing_outside_the_standard_library(self):
        # `pipx install chat-room` resolving zero wheels is a property, not an accident.
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        outside = sorted(imported - sys.stdlib_module_names - {"chat_index"})
        self.assertEqual(outside, [], "room.py imports third-party modules")

    def test_the_room_never_listens_on_a_socket(self):
        source = MODULE.read_text(encoding="utf-8")
        for forbidden in (".listen(", ".bind(", "socketserver", "HTTPServer", "BaseHTTPRequestHandler"):
            self.assertNotIn(forbidden, source, f"room.py appears to open a listening socket via {forbidden}")

    def test_the_hook_path_never_runs_a_merge_analysis(self):
        """Context injection runs on every prompt; merge-tree per branch pair does not belong there."""
        repo = self.repo()
        seen = []
        real = room.subprocess.run
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            store.upsert_presence(repo, "s:1", "one", None, "claude:lane", "online", "SessionStart")
            with mock.patch.object(room.subprocess, "run", side_effect=lambda cmd, *a, **k: (seen.append(cmd), real(cmd, *a, **k))[1]):
                room.compact_context(store, repo, "s:1", "UserPromptSubmit")
        flat = " ".join(" ".join(str(part) for part in cmd) for cmd in seen)
        self.assertNotIn("merge-tree", flat)
        self.assertNotIn("--porcelain", flat)

    def test_merge_readiness_says_when_it_only_looked_at_some_of_the_pairs(self):
        """Found against a 40-worktree project: 40 of 820 pairs probed, reported as if whole.

        The cap is exercised by lowering it rather than by building forty worktrees — the
        behaviour that matters is what happens on the far side of the bound, not the size of
        the fixture that gets there.
        """
        repo = self.repo()
        worktrees = [{"target": f"#lane-{i}", "name": f"lane-{i}", "path": f"/project/{i}", "branch": f"lane-{i}"} for i in range(4)]
        with mock.patch.object(room, "list_worktree_references", return_value=worktrees), \
             mock.patch.object(room, "branch_changed_paths", return_value={"app/ui.tsx"}), \
             mock.patch.object(room, "merge_conflict_paths", return_value=set()), \
             mock.patch.object(room, "changed_worktree_paths", return_value=set()), \
             mock.patch.object(room, "MAX_CONFLICT_PROBES", 2), \
             tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            verdict = store.merge_readiness(repo, "main")
        self.assertEqual(verdict["pairs_nominated"], 6, "every pair shares a file, so all six are nominated")
        self.assertEqual(verdict["pairs_probed"], 2, "the cap was not applied")
        self.assertFalse(verdict["complete"])
        self.assertIn("floor, not a total", verdict["detail"])

    def test_merge_readiness_claims_completeness_only_when_it_has_it(self):
        repo = self.repo()
        worktrees = [{"target": f"#lane-{i}", "name": f"lane-{i}", "path": f"/project/{i}", "branch": f"lane-{i}"} for i in range(3)]
        with mock.patch.object(room, "list_worktree_references", return_value=worktrees), \
             mock.patch.object(room, "branch_changed_paths", return_value={"app/ui.tsx"}), \
             mock.patch.object(room, "merge_conflict_paths", return_value=set()), \
             mock.patch.object(room, "changed_worktree_paths", return_value=set()), \
             tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            verdict = store.merge_readiness(repo, "main")
        self.assertTrue(verdict["complete"])
        self.assertNotIn("detail", verdict)

    def test_a_truncated_conflict_scan_says_so(self):
        repo = self.repo()
        room.CONFLICT_SCANS.clear()
        room.CONFLICT_SCANS[repo.room_id] = (room.time.monotonic(), [], {"probed": 40, "cap": 40, "nominated": 91, "complete": False})
        reported = room.incomplete_coverage(repo)
        self.assertEqual(reported["conflict_scan"]["nominated"], 91)
        self.assertIn("chat-room ready", reported["conflict_scan"]["detail"])
        # A complete scan adds nothing, so callers never see a key that means nothing.
        room.CONFLICT_SCANS[repo.room_id] = (room.time.monotonic(), [], {"probed": 3, "cap": 40, "nominated": 3, "complete": True})
        self.assertEqual(room.incomplete_coverage(repo), {})
        room.CONFLICT_SCANS.clear()

    def test_the_protocol_document_matches_the_code(self):
        # The contract drifted once already: the document advertised seven tools when
        # there were fifteen. It is only a contract if it fails when it stops being true.
        protocol = (MODULE.parents[3] / "docs" / "protocol.md").read_text(encoding="utf-8")
        documented_tools = set(re.findall(r"`(room_[a-z_]+)`", protocol))
        implemented_tools = {tool["name"] for tool in room.tool_definitions()}
        self.assertEqual(implemented_tools - documented_tools, set(), "undocumented MCP tools")
        self.assertEqual(documented_tools - implemented_tools, set(), "documented tools that do not exist")

        documented_commands = set(re.findall(r"`chat-room ([a-z-]+)`", protocol))
        implemented_commands = set(room.parser()._subparsers._group_actions[0].choices)
        self.assertEqual(implemented_commands - documented_commands, set(), "undocumented CLI commands")
        self.assertEqual(documented_commands - implemented_commands, set(), "documented commands that do not exist")

    def test_shared_tables_are_never_written_positionally(self):
        # Several versions of this script share one database on a machine. A positional
        # INSERT couples every writer to the exact column count, so the next added column
        # breaks whichever versions are not upgraded in the same instant.
        offenders = re.findall(r"INSERT (?:OR [A-Z]+ )?INTO (\w+) VALUES", MODULE.read_text(encoding="utf-8"))
        self.assertEqual(offenders, [], f"name the columns for: {sorted(set(offenders))}")

    def test_a_previous_version_can_still_write_presence(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp:
            with room.RoomStore(Path(temp)) as store:
                store.upsert_presence(repo, "session:one", "one", None, "claude:lane", "online", "SessionStart")
            # Exactly what 0.5 emits. It must keep working against a 0.6 database.
            legacy = sqlite3.connect(str(Path(temp) / "chat-room.sqlite3"))
            legacy.execute(
                "INSERT INTO presence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (repo.room_id, "session:legacy", "legacy", None, "codex:lane", "online", "/p", "/p", "lane", "a" * 40, room.utc_now(), room.utc_now(), "SessionStart", "codex-lane", None),
            )
            legacy.commit()
            legacy.close()
            with room.RoomStore(Path(temp)) as store:
                self.assertIn("@codex-lane", [item["target"] for item in store.members(repo.room_id)])

    def test_a_claimed_handle_survives_without_widening_presence(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            store.upsert_presence(repo, "session:one", "one", None, "claude:lane", "online", "SessionStart")
            store.claim_handle(repo, "one", "release-captain")
            # A later role change must not drag a deliberately chosen handle along with it.
            store.upsert_presence(repo, "session:one", "one", None, "claude:other", "online", "PostToolUse")
            member = next(item for item in store.members(repo.room_id) if item["participant_id"] == "session:one")
            self.assertEqual(member["target"], "@release-captain")
            columns = [row[1] for row in store.connection.execute("PRAGMA table_info(presence)")]
            self.assertNotIn("claimed", columns)
            self.assertEqual(len(columns), 15)

    def test_started_session_keeps_the_prompt_out_of_process_arguments(self):
        seen = {}

        class FakeProcess:
            pid = 4242
            def __init__(self): self.stdin = io.BytesIO()
            def poll(self): return None

        def fake_popen(command, **kwargs):
            seen["command"] = command
            seen["cwd"] = kwargs.get("cwd")
            return FakeProcess()

        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(room, "find_cli_executable", return_value="/usr/local/bin/claude"), mock.patch.object(room.subprocess, "Popen", side_effect=fake_popen):
                room.spawn_cli_turn(Path(temp), "claude", Path(temp), "rebuild the projection", None)
            self.assertEqual(seen["command"], ["/usr/local/bin/claude", "--print"])
            self.assertNotIn("rebuild the projection", " ".join(seen["command"]))
            with mock.patch.object(room, "find_cli_executable", return_value="/usr/local/bin/claude"), mock.patch.object(room.subprocess, "Popen", side_effect=fake_popen):
                room.spawn_cli_turn(Path(temp), "claude", Path(temp), "carry on", "session-abc")
            self.assertEqual(seen["command"], ["/usr/local/bin/claude", "--print", "--resume", "session-abc"])

    def test_start_session_refuses_a_worktree_outside_this_project(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(room, "path_belongs_to_room", return_value=False):
                with self.assertRaises(room.RoomError):
                    room.start_session(Path(temp), repo, "claude", temp, "do the thing")
            with self.assertRaises(room.RoomError):
                room.start_session(Path(temp), repo, "emacs", temp, "do the thing")

    def test_tagging_an_idle_claude_carries_the_message_into_its_session(self):
        repo = self.repo()
        worktrees = [{"target": "#lane", "name": "lane", "path": str(repo.worktree), "branch": "lane"}]

        class FakeProcess:
            pid = 77
            def poll(self): return None

        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store, mock.patch.object(room, "list_worktree_references", return_value=worktrees):
            store.upsert_presence(repo, "session:one", "claude-session-one", None, "claude:lane", "online", "Stop")
            # Pinned under the cooldown: a freshly booted machine reports a small
            # monotonic clock, and a never-delivered session must still be reachable.
            with mock.patch.object(room, "spawn_cli_turn", return_value={"process": FakeProcess(), "log": "x", "client": "claude"}) as spawn, mock.patch.object(room.time, "monotonic", return_value=3.0):
                posted = store.post(repo, "@human", "message", "general", "posted", "@claude-lane please rebase", [])
            self.assertEqual(posted["wake"]["started"], ["@claude-lane"])
            self.assertIn("please rebase", spawn.call_args[0][3])
            room.CHAT_DELIVERIES.clear()

            # System chatter must never bill a vendor turn, whoever it tags.
            with mock.patch.object(room, "spawn_cli_turn") as quiet:
                store.post(repo, "@chat-room", "message", "general", "posted", "@claude-lane overlap noticed", [])
            quiet.assert_not_called()

            # Neither may an explicitly disabled policy.
            store.set_option("delivery_policy", "wake_on_tag", "off")
            with mock.patch.object(room, "spawn_cli_turn") as disabled:
                store.post(repo, "@human", "message", "general", "posted", "@claude-lane still quiet", [])
            disabled.assert_not_called()

    def test_search_reaches_past_the_recent_window(self):
        repo = self.repo()
        worktrees = [{"target": "#lane", "name": "lane", "path": str(repo.worktree), "branch": "lane"}]
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store, mock.patch.object(room, "list_worktree_references", return_value=worktrees):
            store.post(repo, "@human", "message", "general", "posted", "the rebase door is closed", [])
            for index in range(40):
                store.post(repo, "@human", "message", "general", "posted", f"routine note {index}", [])
            self.assertEqual(len(store.recent(repo.room_id, 10)), 10)
            found = store.search(repo.room_id, "rebase door")
            self.assertEqual([item["message"] for item in found], ["the rebase door is closed"])
            self.assertEqual(store.search(repo.room_id, "   "), [])
            # A wildcard must be matched literally rather than widening the search.
            self.assertEqual(store.search(repo.room_id, "%"), [])

    def test_injected_context_only_carries_unseen_messages(self):
        repo = self.repo()
        worktrees = [{"target": "#lane", "name": "lane", "path": str(repo.worktree), "branch": "lane"}]
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store, mock.patch.object(room, "list_worktree_references", return_value=worktrees):
            store.upsert_presence(repo, "session:one", "one", None, "claude:lane", "online", "SessionStart")
            store.post(repo, "@human", "message", "general", "posted", "first note", [])
            first = room.compact_context(store, repo, "session:one", "UserPromptSubmit")
            self.assertIn("first note", first)
            # The same hook firing again must not replay what was already delivered.
            self.assertNotIn("first note", room.compact_context(store, repo, "session:one", "UserPromptSubmit"))
            store.post(repo, "@human", "message", "general", "posted", "second note", [])
            later = room.compact_context(store, repo, "session:one", "UserPromptSubmit")
            self.assertIn("second note", later)
            self.assertNotIn("first note", later)

    def test_human_loop_and_agent_chatter_share_one_durable_thread_store(self):
        repo = self.repo()
        worktrees = [{"target": "#lane", "name": "lane", "path": str(repo.worktree), "branch": "lane"}]
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store, mock.patch.object(room, "list_worktree_references", return_value=worktrees):
            human = store.open_thread(repo, "Choose navigation", "design direction", "codex-session", ["#lane"], ["app/ui.tsx"], "team-channel", metadata={"audience": "human-loop", "origin": "@codex-lane"})
            chatter = store.open_thread(repo, "Coordinate writes", "shared worktree", "@human", ["@human", "#lane"], [], "temporary-channel", metadata={"audience": "agents", "origin": "observed activity"})
            self.assertEqual(human["participants"], ["@human", "#lane"])
            self.assertEqual(human["audience"], "human-loop")
            self.assertEqual(human["origin"], "@codex-lane")
            self.assertEqual(chatter["participants"], ["#lane"])
            self.assertEqual(chatter["audience"], "agents")

    def test_coordination_alerts_derive_from_worktree_and_thread_indexes(self):
        targets = {
            "worktrees": [{"target": "#lane", "path": "/project/lane", "active_agents": 2}],
            "agents": [{"target": "@one", "worktree": "/project/lane"}, {"target": "@two", "worktree": "/project/lane"}],
        }
        threads = [{"id": "conflict-a", "source": "preemptive-conflict", "reason": "preemptive file overlap", "title": "Potential conflict: app/ui.tsx", "participants": ["#one", "#two"], "paths": ["app/ui.tsx"]}]
        alerts = room.coordination_alerts(targets, threads)
        self.assertEqual([item["type"] for item in alerts], ["shared-worktree", "file-overlap"])
        self.assertEqual(alerts[0]["participants"], ["#lane", "@one", "@two"])
        self.assertEqual(alerts[1]["thread_id"], "conflict-a")

    def test_codex_history_exposes_only_user_and_assistant_messages(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            path.write_text("\n".join([
                json.dumps({"type": "event_msg", "timestamp": "2026-01-01T00:00:00Z", "payload": {"type": "user_message", "message": "hello"}}),
                json.dumps({"type": "response_item", "payload": {"type": "reasoning", "content": "hidden"}}),
                json.dumps({"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z", "payload": {"type": "agent_message", "message": "hi"}}),
            ]))
            summary = {"client": "Codex", "id": "session-a", "title": "Hello", "updated_at": "2026-01-01T00:00:01Z", "worktree": "lane", "read_only": True}
            with mock.patch.object(room, "discover_chat_catalog", return_value=([summary], {("codex", "session-a"): path})):
                history = room.chat_transcript(repo, "Codex", "session-a")
            self.assertEqual([item["body"] for item in history["messages"]], ["hello", "hi"])

    def test_selected_chat_exposes_the_complete_visible_conversation(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            path.write_text("\n".join(json.dumps({"type": "event_msg", "timestamp": f"2026-01-01T00:00:{index % 60:02d}Z", "payload": {"type": "agent_message", "message": f"message {index}"}}) for index in range(1005)))
            summary = {"client": "Codex", "id": "session-all", "title": "All", "updated_at": "2026-01-01T00:00:01Z", "worktree": "lane", "read_only": True}
            with mock.patch.object(room, "discover_chat_catalog", return_value=([summary], {("codex", "session-all"): path})):
                history = room.chat_transcript(repo, "Codex", "session-all")
            self.assertEqual(len(history["messages"]), 1005)

    def test_the_readme_only_promises_commands_that_exist(self):
        """The README described the browser room for a release after it was deleted.

        Prose cannot be generated from the program the way the site is, but it can at least
        be held to the command surface, which is the part that goes stale silently.
        """
        readme = (MODULE.parents[3] / "README.md").read_text(encoding="utf-8")
        implemented = set(room.parser()._subparsers._group_actions[0].choices)
        # Anchored so `pipx inject chat-room sqlalchemy` is not read as a subcommand:
        # either the start of a line in a shell block, or inline code.
        promised = set(re.findall(r"(?m)^chat-room ([a-z][a-z-]*)", readme)) | set(re.findall(r"`chat-room ([a-z][a-z-]*)", readme))
        self.assertTrue(promised)
        self.assertEqual(promised - implemented, set(), "README promises commands that do not exist")

    def test_every_shipped_file_is_covered_by_the_package_data_globs(self):
        """A named glob ships in a checkout and silently misses the wheel.

        Building a wheel here would be slow, so this asserts the cheaper property that
        actually broke: every non-Python file the plugin needs matches a declared pattern.
        """
        import fnmatch
        plugin = MODULE.parents[1]
        pyproject = (MODULE.parents[3] / "pyproject.toml").read_text(encoding="utf-8")
        block = re.search(r"chat_room = \[(.*?)\]", pyproject, re.S)
        self.assertIsNotNone(block, "pyproject.toml declares no package data for chat_room")
        patterns = re.findall(r'"([^"]+)"', block.group(1))
        shipped = [p for p in plugin.rglob("*") if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts]
        self.assertTrue(shipped)
        for path in shipped:
            relative = path.relative_to(plugin).as_posix()
            self.assertTrue(any(fnmatch.fnmatch(relative, pattern) for pattern in patterns),
                            f"{relative} ships in a checkout but matches no package-data pattern")

    def test_a_card_lands_in_the_column_its_state_implies(self):
        repo = self.repo()
        worktrees = [{"target": "#lane", "name": "lane", "path": str(repo.worktree), "branch": "lane"},
                     {"target": "#old", "name": "old", "path": "/project/old", "branch": "old"}]
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store, mock.patch.object(room, "list_worktree_references", return_value=worktrees):
            store.upsert_presence(repo, "s:1", "one", None, "claude:lane", "online", "SessionStart")
            store.claim_handle(repo, "one", "api-agent")
            store.open_thread(repo, "Rebuild the projection", "coordination", "@human", ["@api-agent"], [])
            store.open_thread(repo, "Choose navigation", "design direction", "@human", ["@human"], [], metadata={"audience": "human-loop"})
            store.open_thread(repo, "Sweep stale lanes", "coordination", "@human", ["#old"], [])
            done = store.open_thread(repo, "Remove the browser room", "handoff", "@human", ["@api-agent"], [])
            store.close_thread(repo, done["id"])
            board = store.board(repo)
        self.assertEqual(board["counts"], {"backlog": 1, "doing": 1, "blocked": 1, "done": 1})
        self.assertEqual(board["columns"]["doing"][0]["owners"], ["@api-agent"])
        # Nobody declares a column; it follows from who is active and what waits on a human.
        self.assertEqual(board["columns"]["backlog"][0]["title"], "Sweep stale lanes")
        self.assertEqual(board["columns"]["blocked"][0]["title"], "Choose navigation")

    def test_the_board_renders_every_card_in_its_column(self):
        board = {"room_id": "r", "counts": {}, "columns": {
            "backlog": [{"title": "Sweep stale lanes", "reason": "coordination", "owners": []}],
            "doing": [{"title": "Rebuild the projection", "reason": "coordination", "owners": ["@api-agent"]}],
            "blocked": [{"title": "Choose navigation", "reason": "design direction", "owners": []}],
            "done": [],
        }}
        text = room.render_board(board, width=104)
        self.assertIn("BACKLOG (1)", text)
        self.assertIn("DONE (0)", text)
        self.assertIn("@api-agent", text)
        self.assertIn("waiting on @human", text)
        self.assertIn("—", text)  # an empty column still holds its place

    def test_only_a_refused_rule_denies_a_write(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            store.upsert_presence(repo, "mine", "one", None, "claude:lane", "online", "SessionStart")
            store.upsert_presence(repo, "theirs", "two", None, "codex:lane", "online", "SessionStart")
            # Advisory by default: two actors in one worktree is reported, never blocked.
            self.assertEqual(room.refusals(store, repo, "mine"), [])
            store.set_option("rules", "one-actor-per-worktree", "refuse")
            reasons = room.refusals(store, repo, "mine")
            self.assertEqual(len(reasons), 1)
            self.assertIn("refuse", reasons[0])
            self.assertIn("#lane", reasons[0])
            # A session never refuses its own write, and an empty room refuses nothing.
            store.upsert_presence(repo, "theirs", "two", None, "codex:lane", "offline", "SessionEnd")
            self.assertEqual(room.refusals(store, repo, "mine"), [])

    def test_a_denial_is_only_ever_emitted_for_pretooluse(self):
        denial = room.hook_output("PreToolUse", "", ["one-actor-per-worktree is set to refuse."])
        self.assertEqual(denial["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(denial["continue"])
        # Context injection and denial never travel together.
        self.assertNotIn("additionalContext", denial["hookSpecificOutput"])
        quiet = room.hook_output("SessionStart", "room context")
        self.assertNotIn("permissionDecision", quiet.get("hookSpecificOutput", {}))

    def test_the_hook_fails_open_when_the_room_is_unreadable(self):
        # A broken room must never be able to stop work.
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "chat-room.sqlite3").write_text("not a database")
            payload = json.dumps({"hook_event_name": "PreToolUse", "cwd": str(Path(temp)), "session_id": "a"})
            with mock.patch("sys.stdin", io.StringIO(payload)), mock.patch("sys.stdout", io.StringIO()) as out:
                self.assertEqual(room.run_hook(Path(temp)), 0)
            answer = json.loads(out.getvalue())
        self.assertTrue(answer["continue"])
        self.assertNotIn("permissionDecision", json.dumps(answer))

    def test_a_rule_nobody_set_reports_its_default_as_undecided(self):
        # The difference between a default and an answer is what lets an interrogation
        # ask only what is still open, so it has to survive a round trip.
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            rules = store.rules()
            self.assertEqual({name for name, *_ in room.RULE_CATALOG}, set(rules))
            self.assertTrue(all(item["rung"] == item["default"] and not item["decided"] for item in rules.values()))
            store.set_option("rules", "one-actor-per-worktree", "refuse")
            settled = store.rules()["one-actor-per-worktree"]
            self.assertEqual(settled["rung"], "refuse")
            self.assertTrue(settled["decided"])
            # The room cannot block a write, and must not claim it can.
            self.assertFalse(settled["mechanically_enforced"])

    def test_an_unknown_rung_falls_back_rather_than_suppressing_the_rule(self):
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            store.set_option("rules", "file-overlap", "sometimes")
            self.assertEqual(store.rules()["file-overlap"]["rung"], "advise")

    def test_the_rung_decides_whether_a_condition_is_reported_and_how_loudly(self):
        targets = {"agents": [], "worktrees": [{"target": "#one", "path": "/p/one", "active_agents": 2, "age_days": 0}]}
        heights = {rung: room.coordination_alerts(targets, [], {"one-actor-per-worktree": {"rung": rung}}) for rung in room.RULE_RUNGS}
        self.assertEqual(heights["off"], [])
        self.assertEqual([item["severity"] for item in heights["advise"]], ["warning"])
        self.assertEqual([item["severity"] for item in heights["warn"]], ["attention"])
        self.assertEqual([item["severity"] for item in heights["refuse"]], ["critical"])
        self.assertEqual(heights["refuse"][0]["rule"], "one-actor-per-worktree")

    def test_the_stale_threshold_is_the_operators_number(self):
        targets = {"agents": [], "worktrees": [{"target": "#old", "path": "/p/old", "active_agents": 0, "age_days": 10}]}
        rules = {"stale-worktrees": {"rung": "advise", "parameter": 30}}
        self.assertEqual(room.coordination_alerts(targets, [], rules), [])
        rules["stale-worktrees"]["parameter"] = 7
        stale = room.coordination_alerts(targets, [], rules)
        self.assertEqual([item["type"] for item in stale], ["stale-worktrees"])
        self.assertIn("7 days", stale[0]["title"])

    def test_only_a_raised_rule_reaches_a_session_context(self):
        # An advisory default in every turn would be noise; a raised rule has to travel,
        # because injected context is the only way a hard rule reaches an agent.
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            store.upsert_presence(repo, "session:one", "one", None, "claude:lane", "online", "SessionStart")
            self.assertNotIn("House rules", room.compact_context(store, repo, "session:one", "SessionStart"))
            store.set_option("rules", "one-actor-per-worktree", "refuse")
            context = room.compact_context(store, repo, "session:one", "SessionStart")
            self.assertIn("one-actor-per-worktree (refuse)", context)
            self.assertIn("binding", context)

    def test_chat_recency_defines_recent_and_inactive(self):
        self.assertEqual(room.chat_recency(room.utc_now()), "recent")
        self.assertEqual(room.chat_recency("2000-01-01T00:00:00Z"), "inactive")

    def test_dormant_chat_delivery_uses_installed_cli_adapter(self):
        summary = {"client": "Codex", "id": "session-a"}
        with mock.patch.object(room.shutil, "which", return_value="/usr/local/bin/codex"):
            state = room.chat_delivery_state(summary, [])
        self.assertTrue(state["ready"])
        self.assertEqual(state["mode"], "resume")
        self.assertEqual(state["label"], "Ready to continue")
        active = [{"session_id": "session-a", "state": "online", "last_event": "PostToolUse", "wake_endpoint": None}]
        state = room.chat_delivery_state(summary, active)
        self.assertFalse(state["ready"])
        self.assertEqual(state["mode"], "active-unattached")
        self.assertEqual(state["label"], "Active elsewhere")

    def test_cli_discovery_survives_a_minimal_path(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(room.shutil, "which", return_value=None), mock.patch.object(room.Path, "home", return_value=Path(temp)):
            executable = Path(temp) / ".local" / "bin" / "codex"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            self.assertEqual(room.find_cli_executable("codex"), str(executable))

    def test_chat_delivery_passes_prompt_on_stdin_not_process_arguments(self):
        repo = self.repo()
        prompt = "inspect the focused tests"
        class Sink:
            def __init__(self): self.value = b""
            def write(self, value): self.value += value
            def close(self): return None
        process = mock.Mock(pid=42, stdin=Sink())
        process.poll.return_value = None
        summary = {"client": "Codex", "id": "session-a", "cwd": "/project/lane", "worktree": "lane"}
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(room, "discover_chat_catalog", return_value=([summary], {})), mock.patch.object(room, "path_belongs_to_room", return_value=True), mock.patch.object(room.shutil, "which", return_value="/usr/local/bin/codex"), mock.patch.object(room.subprocess, "Popen", return_value=process) as popen:
            value = room.start_chat_delivery(Path(temp), repo, "Codex", "session-a", prompt, [])
        command = popen.call_args.args[0]
        self.assertNotIn(prompt, command)
        self.assertEqual(command[-1], "-")
        self.assertEqual(process.stdin.value, (prompt + "\n").encode())
        self.assertEqual(value["status"], "started")
        room.CHAT_DELIVERIES.clear()

    def test_chat_images_are_bounded_and_passed_to_codex(self):
        repo = self.repo()
        summary = {"client": "Codex", "id": "session-image", "cwd": "/project/lane", "worktree": "lane"}
        class Sink:
            def write(self, _value): return None
            def close(self): return None
        process = mock.Mock(pid=43, stdin=Sink())
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as temp:
            picture = Path(temp) / "paste.png"
            picture.write_bytes(b"\x89PNG\r\n\x1a\n")
            with self.assertRaises(room.RoomError):
                room.chat_images([str(picture)] * (room.MAX_CHAT_IMAGES + 1))
            with self.assertRaises(room.RoomError):
                room.chat_images([str(Path(temp) / "absent.png")])
            images = room.chat_images([str(picture)])
            with mock.patch.object(room, "discover_chat_catalog", return_value=([summary], {})), mock.patch.object(room, "path_belongs_to_room", return_value=True), mock.patch.object(room.shutil, "which", return_value="/usr/local/bin/codex"), mock.patch.object(room.subprocess, "Popen", return_value=process) as popen:
                value = room.start_chat_delivery(Path(temp), repo, "Codex", "session-image", "review this", [], images)
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("--image") + 1], str(picture.resolve()))
            self.assertNotIn("review this", command)
            self.assertEqual(value["images"], 1)
            # The caller keeps its own file; delivery never consumes it.
            self.assertTrue(picture.exists())
        room.CHAT_DELIVERIES.clear()

    def test_room_and_chat_renames_are_local_index_overlays(self):
        repo = self.repo()
        summary = {"client": "Codex", "id": "session-a"}
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            renamed = store.rename(repo, "room", repo.room_id, "Release room")
            self.assertEqual(renamed["label"], "Release room")
            self.assertEqual(store.display_name("room_name", repo.room_id, "fallback"), "Release room")
            with mock.patch.object(room, "discover_chat_catalog", return_value=([summary], {})):
                store.rename(repo, "chat", "session-a", "Compiler cleanup", "Codex")
            self.assertEqual(store.display_name("chat_name", "Codex-session-a", "fallback"), "Compiler cleanup")


if __name__ == "__main__": unittest.main()
