import importlib.util
import io
import json
import os
import sqlite3
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
                message = store.post(repo, "@human", "request", "cleanup", "posted", "@ project-manager inspect #lane", [])
            self.assertEqual(message["recipients"], ["@project-manager", "#lane"])
            self.assertEqual(store.read(repo.room_id)[0]["schema"], "chat-room.message.v1")
            self.assertEqual(store.status(repo)["authority"], "advisory-only")

    def test_unknown_active_handle_is_rejected(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            with mock.patch.object(room, "list_worktree_references", return_value=[]):
                with self.assertRaises(room.RoomError): store.post(repo, "@human", "request", "test", "posted", "@missing hello", [])

    def test_hook_fails_open_outside_git(self):
        with mock.patch.object(room, "resolve_repository", return_value=None), mock.patch("sys.stdin") as stdin, mock.patch("sys.stdout") as stdout:
            stdin.__iter__.return_value = iter([]); stdin.read.return_value = ""

    def test_ui_pins_combined_room_and_groups_interface_sessions(self):
        assets = MODULE.parents[1] / "assets"
        html = (assets / "index.html").read_text()
        script = (assets / "room.js").read_text()
        self.assertIn("All activity", html)
        self.assertIn('id="chats"', html)
        self.assertIn('id="worktrees"', html)
        self.assertIn("openHistory", script)
        self.assertIn("openThread", script)

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
            self.assertEqual(store.close_thread(repo, thread["id"])["status"], "resolved")

    def test_preemptive_overlap_opens_thread_without_changing_git(self):
        repo = self.repo()
        worktrees = [
            {"target": "#lane-one", "name": "lane-one", "path": "/project/one", "branch": "one"},
            {"target": "#lane-two", "name": "lane-two", "path": "/project/two", "branch": "two"},
        ]
        conflicts = [{"path": "app/ui.tsx", "worktrees": worktrees}]
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store, mock.patch.object(room, "list_worktree_references", return_value=worktrees), mock.patch.dict(room.CONFLICT_SCANS, {repo.room_id: (room.time.monotonic(), conflicts)}, clear=True):
            threads = store.sync_preemptive_conflicts(repo)
            self.assertEqual(len(threads), 1)
            self.assertEqual(threads[0]["source"], "preemptive-conflict")
            self.assertEqual(threads[0]["participants"], ["#lane-one", "#lane-two"])

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

    def test_ui_defaults_to_durable_loopback_name(self):
        args = room.parser().parse_args(["ui"])
        self.assertEqual(args.hostname, "chatroom.localhost")
        self.assertEqual(args.port, 7391)

    def test_chat_recency_defines_recent_and_inactive(self):
        self.assertEqual(room.chat_recency(room.utc_now()), "recent")
        self.assertEqual(room.chat_recency("2000-01-01T00:00:00Z"), "inactive")


if __name__ == "__main__": unittest.main()
