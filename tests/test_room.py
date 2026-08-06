import importlib.util
import contextlib
import http.client
import threading
from http import HTTPStatus
import io
import json
import os
import re
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

    def test_ui_pins_combined_room_and_groups_interface_sessions(self):
        assets = MODULE.parents[1] / "assets"
        html = (assets / "index.html").read_text()
        script = (assets / "room.js").read_text()
        self.assertIn("Command Console", html)
        self.assertIn("Human in the Loop", html)
        self.assertIn("Chatter", html)
        self.assertIn('id="suggestions"', html)
        self.assertNotIn("Needs attention", html)
        self.assertIn('id="chats"', html)
        self.assertIn('id="active-agents"', html)
        self.assertIn('id="human-thread-form"', html)
        self.assertNotIn('id="worktrees"', html)
        self.assertIn("openHistory", script)
        self.assertIn("openThread", script)
        self.assertIn("/api/chat-send", script)
        self.assertIn("/api/rename", script)
        self.assertIn("new WebSocket", script)
        self.assertIn("clipboardData", script)
        self.assertIn('value="all"', html)
        self.assertIn('value="tag"', html)
        self.assertIn("renderRouting", script)
        self.assertIn("What do you want to activate?", script)
        self.assertIn("View room log", script)
        self.assertIn('id="composer" hidden', html)
        self.assertNotIn("Open in CLI", script)
        self.assertIn('#room-routing[hidden]', (assets / "room.css").read_text())
        self.assertNotIn("setInterval(refreshRoom", script)
        # Everything a terminal was still needed for has a control in the room.
        self.assertIn('id="search-form"', html)
        self.assertIn('id="stop-turn"', html)
        self.assertIn('id="unread-count"', html)
        self.assertIn("Start new work", script)
        self.assertIn("/api/session-start", script)
        self.assertIn("/api/session-stop", script)
        self.assertIn("/api/search", script)

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

    @unittest.skipUnless(CHAT_INDEX, "optional transcript index not installed")
    def test_index_reports_an_unreachable_store_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(os.environ, {CHAT_INDEX.DATABASE_URL_ENV: "postgresql+psycopg://someone:secret@127.0.0.1:59999/nope"}):
                with self.assertRaises(CHAT_INDEX.IndexUnavailable) as unreachable:
                    CHAT_INDEX.build_engine(Path(temp))
            # The message locates the store without repeating the credentials in it.
            self.assertNotIn("secret", str(unreachable.exception))

    @contextlib.contextmanager
    def http_server(self):
        """A real loopback server over a real Git worktree.

        The handler is where the token gate, the host check, and every body limit
        actually live, so they are asserted against requests rather than by reading
        the source.
        """
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()

            def git(*args):
                subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            git("init", "-q", "-b", "main")
            git("config", "user.email", "test@example.test")
            git("config", "user.name", "Test")
            (root / "readme.txt").write_text("hello\n")
            git("add", "."); git("commit", "-qm", "base")

            repo = room.resolve_repository(str(root))
            token = "a" * 32
            server = room.RoomHTTPServer(
                ("127.0.0.1", 0), repo, Path(temp) / "data", MODULE.parents[1] / "assets",
                token, "localhost", 0, room.EventHub(),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"127.0.0.1:{server.server_address[1]}", token, root
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    @staticmethod
    def request(authority, method="GET", path="/", token=None, body=None, host=None):
        connection = http.client.HTTPConnection(authority, timeout=10)
        headers = {"Host": host or authority}
        if token:
            headers["X-Chat-Room-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8", "replace")
        finally:
            connection.close()

    def test_http_serves_the_room_and_gates_every_api_on_the_local_token(self):
        with self.http_server() as (authority, token, _root):
            status, page = self.request(authority)
            self.assertEqual(status, 200)
            self.assertIn("<title>Chat Room</title>", page)

            # Static assets the page needs are readable; anything else is not served.
            for asset in ("/room.css", "/room.js", "/icons.svg"):
                self.assertEqual(self.request(authority, path=asset)[0], 200, asset)
            self.assertEqual(self.request(authority, path="/../room.py")[0], 404)
            self.assertEqual(self.request(authority, path="/api/nothing")[0], 404)

            # Every API read requires the local token, including the snapshot.
            for path in ("/api/snapshot", "/api/search?q=x", "/api/chats"):
                self.assertEqual(self.request(authority, path=path)[0], 403, path)
                self.assertEqual(self.request(authority, path=path, token=token)[0], 200, path)

            # And so does every write.
            self.assertEqual(self.request(authority, "POST", "/api/messages", body={"message": "hi"})[0], 403)

    def test_http_refuses_a_foreign_host_header(self):
        with self.http_server() as (authority, token, _root):
            status, _ = self.request(authority, path="/api/snapshot", token=token, host="chat-room.example.test")
            self.assertEqual(status, HTTPStatus.MISDIRECTED_REQUEST)

    def test_http_write_paths_report_their_refusals(self):
        with self.http_server() as (authority, token, root):
            status, body = self.request(authority, "POST", "/api/messages", token=token, body={"message": "coordination note", "kind": "message", "topic": "general"})
            self.assertEqual(status, 201, body)

            # Oversized bodies are refused before they are read.
            status, _ = self.request(authority, "POST", "/api/messages", token=token, body={"message": "x" * 20000})
            self.assertEqual(status, 413)

            # A credential-shaped message never reaches storage.
            status, body = self.request(authority, "POST", "/api/messages", token=token, body={"message": "password=hunter2hunter2"})
            self.assertEqual(status, 400)
            self.assertIn("value-free", body)

            # Starting a session outside this project is refused by path, not by luck.
            status, body = self.request(authority, "POST", "/api/session-start", token=token, body={"client": "claude", "worktree": "/", "prompt": "no"})
            self.assertEqual(status, 400)
            self.assertIn("does not belong", body)

            # An unsupported client never reaches a subprocess.
            status, _ = self.request(authority, "POST", "/api/session-start", token=token, body={"client": "emacs", "worktree": str(root), "prompt": "no"})
            self.assertEqual(status, 400)

            # Stopping a turn nobody started is an error, not a crash.
            self.assertEqual(self.request(authority, "POST", "/api/session-stop", token=token, body={"client": "claude", "session_id": "missing"})[0], 400)
            self.assertEqual(self.request(authority, "POST", "/api/unknown", token=token, body={})[0], 404)

    def test_the_protocol_document_matches_the_code(self):
        # The contract drifted once already: the document advertised seven tools when
        # there were fifteen. It is only a contract if it fails when it stops being true.
        protocol = (MODULE.parents[3] / "docs" / "protocol.md").read_text()
        documented_tools = set(re.findall(r"`(room_[a-z_]+)`", protocol))
        implemented_tools = {tool["name"] for tool in room.tool_definitions()}
        self.assertEqual(implemented_tools - documented_tools, set(), "undocumented MCP tools")
        self.assertEqual(documented_tools - implemented_tools, set(), "documented tools that do not exist")

        documented_routes = set(re.findall(r"`(/api/[a-z-]+)`", protocol))
        implemented_routes = set(room.HTTP_READ_ROUTES) | set(room.HTTP_WRITE_ROUTES)
        self.assertEqual(implemented_routes - documented_routes, set(), "undocumented HTTP routes")
        self.assertEqual(documented_routes - implemented_routes, set(), "documented routes that do not exist")

    def test_shared_tables_are_never_written_positionally(self):
        # Several versions of this script share one database on a machine. A positional
        # INSERT couples every writer to the exact column count, so the next added column
        # breaks whichever versions are not upgraded in the same instant.
        offenders = re.findall(r"INSERT (?:OR [A-Z]+ )?INTO (\w+) VALUES", MODULE.read_text())
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
            with mock.patch.object(room, "spawn_cli_turn", return_value={"process": FakeProcess(), "log": "x", "client": "claude"}) as spawn:
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

    def test_ui_defaults_to_durable_loopback_name(self):
        args = room.parser().parse_args(["ui"])
        self.assertEqual(args.hostname, "chatroom.localhost")
        self.assertEqual(args.port, 7391)

    def test_ui_reuses_an_already_running_chat_room_without_traceback(self):
        occupied = OSError(room.errno.EADDRINUSE, "Address already in use")
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(room, "select_repository", return_value=self.repo()), mock.patch.object(room, "RoomHTTPServer", side_effect=occupied), mock.patch.object(room, "running_room_url", return_value="http://chatroom.localhost:7391/"), mock.patch.object(room.webbrowser, "open") as opened, mock.patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(room.run_ui(Path(temp), None, "127.0.0.1", 7391, "chatroom.localhost", None, False), 0)
        opened.assert_called_once_with("http://chatroom.localhost:7391/")
        self.assertIn("already running", output.getvalue())

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

    def test_cli_discovery_survives_a_minimal_service_path(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(room.shutil, "which", return_value=None), mock.patch.object(room.Path, "home", return_value=Path(temp)):
            executable = Path(temp) / ".local" / "bin" / "codex"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            self.assertEqual(room.find_cli_executable("codex"), str(executable))
        self.assertIn("/opt/homebrew/bin", room.service_path().split(os.pathsep))

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

    def test_chat_images_are_bounded_private_and_passed_to_codex(self):
        repo = self.repo()
        summary = {"client": "Codex", "id": "session-image", "cwd": "/project/lane", "worktree": "lane"}
        attachment = {"name": "paste.png", "type": "image/png", "data": "data:image/png;base64,iVBORw0KGgo="}
        class Sink:
            def write(self, _value): return None
            def close(self): return None
        process = mock.Mock(pid=43, stdin=Sink())
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(room, "discover_chat_catalog", return_value=([summary], {})), mock.patch.object(room, "path_belongs_to_room", return_value=True), mock.patch.object(room.shutil, "which", return_value="/usr/local/bin/codex"), mock.patch.object(room.subprocess, "Popen", return_value=process) as popen:
            value = room.start_chat_delivery(Path(temp), repo, "Codex", "session-image", "review this", [], [attachment])
            command = popen.call_args.args[0]
            image_path = Path(command[command.index("--image") + 1])
            self.assertEqual(stat.S_IMODE(image_path.stat().st_mode), 0o600)
            self.assertEqual(image_path.read_bytes(), b"\x89PNG\r\n\x1a\n")
            self.assertNotIn("review this", command)
            self.assertEqual(value["images"], 1)
        room.CHAT_DELIVERIES.clear()

    def test_event_hub_preserves_indexed_event_order(self):
        hub = room.EventHub()
        receiver = hub.subscribe()
        hub.publish("workspace.changed", {"path": "/api/messages"})
        hub.publish("workspace.changed", {"path": "/api/threads"})
        self.assertEqual([receiver.get_nowait()["sequence"], receiver.get_nowait()["sequence"]], [1, 2])
        hub.unsubscribe(receiver)

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
