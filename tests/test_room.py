import importlib.util
import io
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
        self.assertIn("Combined Chat Room", html)
        self.assertIn('id="interfaces"', html)
        self.assertIn("interfaceGroups(data.targets.agents)", script)
        self.assertIn("['Codex',[]],['CLI',[]]", script)

    def test_terminal_chat_registers_and_releases_cli_presence(self):
        repo = self.repo()
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store:
            with mock.patch("sys.stdin", io.StringIO("")), mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(room.run_chat(store, repo, "@human"), 0)
            member = next(item for item in store.members(repo.room_id) if item["role"].startswith("cli:"))
            self.assertEqual(member["state"], "offline")
            self.assertEqual(member["last_event"], "ChatEnd")


if __name__ == "__main__": unittest.main()
