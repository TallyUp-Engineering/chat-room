import importlib.util
import io
import json
import os
import sqlite3
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
        self.assertIn('#room-routing[hidden]', (assets / "room.css").read_text())
        self.assertNotIn("setInterval(refreshRoom", script)

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
            self.assertIn("Confirm ownership, write order, and handoff", prompt["message"])

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

    def test_notification_options_are_indexed_and_route_only_to_chat(self):
        repo = self.repo()
        worktrees = [{"target": "#lane", "name": "lane", "path": str(repo.worktree), "branch": "lane"}]
        with tempfile.TemporaryDirectory() as temp, room.RoomStore(Path(temp)) as store, mock.patch.object(room, "list_worktree_references", return_value=worktrees):
            self.assertEqual([item["key"] for item in store.options()["worktree_action"]], ["consolidate", "delete", "investigate"])
            store.set_option("worktree_action", "archive", "Archive", {"prompt": "Report an archive plan. Do not mutate Git."})
            store.upsert_presence(repo, "session:one", "one", None, "codex:lane", "online", "Stop")
            store.claim_handle(repo, "one", "worker")
            thread = store.route_notification(repo, "Potentially stale lane", "stale-worktrees", "@worker", "archive", ["#lane"], [str(repo.worktree)])
            self.assertEqual(thread["source"], "notification-route")
            self.assertEqual(thread["participants"], ["@human", "@worker", "#lane"])
            self.assertIn("Do not mutate Git", store.read(repo.room_id)[0]["message"])

    def test_stale_worktrees_are_typed_notifications(self):
        targets = {"agents": [], "worktrees": [{"target": "#old", "path": "/project/old", "active_agents": 0, "age_days": 45}]}
        options = {"notification_policy": [{"key": "stale_worktree_days", "value": "30", "metadata": {}}]}
        alerts = room.coordination_alerts(targets, [], options)
        self.assertEqual(alerts[0]["type"], "stale-worktrees")
        self.assertEqual(alerts[0]["thread_id"], None)

    def test_dormant_chat_delivery_uses_installed_cli_adapter(self):
        summary = {"client": "Codex", "id": "session-a"}
        with mock.patch.object(room.shutil, "which", return_value="/usr/local/bin/codex"):
            state = room.chat_delivery_state(summary, [])
        self.assertTrue(state["ready"])
        self.assertEqual(state["mode"], "resume")
        active = [{"session_id": "session-a", "state": "online", "last_event": "PostToolUse", "wake_endpoint": None}]
        state = room.chat_delivery_state(summary, active)
        self.assertFalse(state["ready"])
        self.assertEqual(state["mode"], "active-unattached")

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
