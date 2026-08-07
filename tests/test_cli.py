"""The command as a user runs it: a real process, a real Git project, real worktrees.

Every other test in this suite calls the store directly, inside one process. That is why
preemptive conflict detection could stop working entirely and stay green — the scan ran in
a background thread the process never waited for, and the step that opens threads returned
early unless a scan had already finished. Both are invisible when the test *is* the process
that already ran the scan.

These tests spend a subprocess per assertion on purpose. Anything that depends on a fresh
interpreter, an empty in-process cache, or exit codes belongs here rather than upstairs.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOM = Path(__file__).resolve().parents[1] / "plugins" / "chat-room" / "scripts" / "room.py"


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class CommandTests(unittest.TestCase):
    """A project with two lanes colliding on one file and one lane nobody has touched."""

    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        root = Path(cls._temp.name)
        cls.data = root / "data"
        cls.proj = root / "proj"
        (cls.proj / "app").mkdir(parents=True)
        git(root, "init", "-q", "-b", "main", str(cls.proj))
        git(cls.proj, "config", "user.email", "qa@example.test")
        git(cls.proj, "config", "user.name", "QA")
        (cls.proj / "app" / "ui.tsx").write_text('export const nav = "vertical";\n')
        git(cls.proj, "add", "-A")
        git(cls.proj, "commit", "-qm", "base")

        for lane, value in (("lane-one", "horizontal"), ("lane-two", "grid")):
            git(cls.proj, "worktree", "add", "-q", "-b", lane, str(root / lane))
            (root / lane / "app" / "ui.tsx").write_text(f'export const nav = "{value}";\n')
            git(root / lane, "add", "-A")
            git(root / lane, "commit", "-qm", f"{lane}: {value}")
            # Left uncommitted, which is what nominates the pair as a live overlap.
            (root / lane / "app" / "ui.tsx").write_text(f'export const nav = "{value}-wip";\n')

        git(cls.proj, "worktree", "add", "-q", "-b", "old-lane", str(root / "old-lane"))
        (root / "old-lane" / "legacy.txt").write_text("legacy\n")
        git(root / "old-lane", "add", "-A")
        stamp = "2026-01-05T10:00:00"
        subprocess.run(["git", "-C", str(root / "old-lane"), "commit", "-qm", "legacy"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env={**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})

        cls.run_cli("hook", stdin=json.dumps({"hook_event_name": "SessionStart", "cwd": str(root / "lane-one"), "session_id": "sess-ui"}), client="claude")
        cls.run_cli("hook", stdin=json.dumps({"hook_event_name": "SessionStart", "cwd": str(root / "lane-two"), "session_id": "sess-api"}), client="codex")
        cls.run_cli("identify", "--cwd", str(root / "lane-one"), "--session", "sess-ui", "--handle", "ui-agent")
        cls.run_cli("identify", "--cwd", str(root / "lane-two"), "--session", "sess-api", "--handle", "api-agent")
        # A second actor in lane-one, which is what one-actor-per-worktree is about.
        cls.run_cli("hook", stdin=json.dumps({"hook_event_name": "SessionStart", "cwd": str(root / "lane-one"), "session_id": "sess-second"}), client="codex")
        cls.run_cli("identify", "--cwd", str(root / "lane-one"), "--session", "sess-second", "--handle", "second-agent")

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()

    @classmethod
    def run_cli(cls, *args, stdin=None, client=None, cwd=None):
        environment = dict(os.environ)
        if client:
            environment["CHAT_ROOM_CLIENT"] = client
        return subprocess.run(
            [sys.executable, str(ROOM), "--data-dir", str(cls.data), *args],
            input=stdin, text=True, capture_output=True, timeout=120,
            cwd=str(cwd) if cwd else None, env=environment,
        )

    def cli(self, *args, **kwargs):
        done = self.run_cli(*args, **kwargs)
        self.assertEqual(done.returncode, 0, f"{args} failed: {done.stderr.strip()}")
        return done

    def cli_json(self, *args, **kwargs):
        return json.loads(self.cli(*args, **kwargs).stdout)

    # --- the regression this file exists for ---------------------------------

    def test_a_fresh_process_reports_the_conflict_it_finds(self):
        """Detection has to survive the process exiting, which is the whole bug.

        Two worktrees hold uncommitted edits to one file and their branches genuinely
        collide. Each command below is a new interpreter with an empty conflict cache, so
        nothing can be carried over from a previous call.
        """
        threads = self.cli_json("threads", "--cwd", str(self.proj))["threads"]
        overlaps = [item for item in threads if item["source"] == "preemptive-conflict"]
        self.assertEqual(len(overlaps), 1, "a fresh process reported no overlap")
        self.assertEqual(overlaps[0]["paths"], ["app/ui.tsx"])
        self.assertIn("#lane-one", overlaps[0]["participants"])
        self.assertIn("#lane-two", overlaps[0]["participants"])

        alerts = self.cli_json("alerts", "--cwd", str(self.proj))["alerts"]
        self.assertIn("file-overlap", {item["type"] for item in alerts})

    def test_a_lane_nobody_has_touched_is_reported_stale(self):
        alerts = self.cli_json("alerts", "--cwd", str(self.proj))["alerts"]
        stale = [item for item in alerts if item["type"] == "stale-worktrees"]
        self.assertEqual(len(stale), 1)
        self.assertTrue(any("old-lane" in path for path in stale[0]["paths"]))

    def test_ready_separates_merging_cleanly_from_being_safe_to_land(self):
        verdict = self.cli_json("ready", "--cwd", str(self.proj), "--into", "main")
        self.assertEqual(verdict["into"], "main")
        collisions = {item["branch"]: item["collides_with"] for item in verdict["branches"]}
        # Each lane merges into main on its own; they still collide with each other.
        self.assertTrue(all(item["merges_cleanly"] for item in verdict["branches"]))
        self.assertEqual([entry["branch"] for entry in collisions["lane-one"]], ["lane-two"])
        self.assertEqual(verdict["latent"], 2)

    def test_the_board_places_the_conflict_where_a_human_looks(self):
        rendered = self.cli("board", "--cwd", str(self.proj)).stdout
        self.assertIn("BACKLOG", rendered)
        self.assertIn("Merge conflict", rendered)
        self.assertRegex(rendered, r"#lane-one")

    def test_the_board_alone_surfaces_a_live_collision(self):
        """The board is the most human-facing read, so it cannot be the one view that
        misses a collision. It used to render the conflict threads without running the scan
        that creates them, so a room where nobody had run `threads` first showed nothing."""
        with tempfile.TemporaryDirectory() as fresh:
            done = subprocess.run([sys.executable, str(ROOM), "--data-dir", fresh, "board", "--cwd", str(self.proj)],
                                  text=True, capture_output=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Merge conflict", done.stdout, "a first-ever `board` hid a live collision")

    # --- rules, through the hook a host CLI actually calls --------------------

    def test_a_refused_rule_denies_a_write_and_an_advisory_one_does_not(self):
        room_id = self.cli_json("status", "--cwd", str(self.proj))["room_id"]
        payload = json.dumps({"hook_event_name": "PreToolUse", "cwd": str(Path(self.proj).parent / "lane-one"),
                              "session_id": "sess-ui", "tool_name": "Write"})

        advisory = json.loads(self.cli("hook", stdin=payload, client="claude").stdout)
        self.assertNotIn("permissionDecision", json.dumps(advisory))

        self.cli("option-set", "--cwd", str(self.proj), "--namespace", "rules",
                 "--key", "one-actor-per-worktree", "--value", "refuse")
        refused = json.loads(self.cli("hook", stdin=payload, client="claude").stdout)
        decision = refused["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("one-actor-per-worktree", decision["permissionDecisionReason"])
        self.assertTrue(refused["continue"])

        self.cli("option-set", "--cwd", str(self.proj), "--namespace", "rules",
                 "--key", "one-actor-per-worktree", "--value", "advise")
        self.assertTrue(room_id)

    def test_a_raised_rule_travels_in_injected_context(self):
        payload = json.dumps({"hook_event_name": "SessionStart", "cwd": str(Path(self.proj).parent / "lane-one"), "session_id": "sess-ui"})
        quiet = json.loads(self.cli("hook", stdin=payload, client="claude").stdout)
        self.assertNotIn("House rules", json.dumps(quiet))
        self.cli("option-set", "--cwd", str(self.proj), "--namespace", "rules", "--key", "file-overlap", "--value", "warn")
        try:
            raised = json.loads(self.cli("hook", stdin=payload, client="claude").stdout)
            self.assertIn("file-overlap (warn)", raised["hookSpecificOutput"]["additionalContext"])
        finally:
            self.cli("option-set", "--cwd", str(self.proj), "--namespace", "rules", "--key", "file-overlap", "--value", "advise")

    def test_a_tag_that_arrives_mid_turn_is_handed_over_when_the_session_rests(self):
        """Nothing outside a process can reach it once it is parked at a prompt.

        The moment it comes to rest is the last chance, so a tag that arrived while it was
        working is handed over there — in the session a human is watching, rather than in a
        detached turn they never see.
        """
        lane = Path(self.proj).parent / "lane-two"
        rest = json.dumps({"hook_event_name": "Stop", "cwd": str(lane), "session_id": "sess-api"})

        # Drain anything already waiting, so the assertion is about the new tag alone.
        self.cli("hook", stdin=rest, client="codex")
        quiet = json.loads(self.cli("hook", stdin=rest, client="codex").stdout)
        self.assertNotIn("decision", quiet, "a session with nothing waiting was held at the door")

        self.cli("post", "--cwd", str(self.proj), "--sender", "@ui-agent", "--kind", "message",
                 "--topic", "nav", "--message", "@api-agent please rebase onto main before landing")
        handed = json.loads(self.cli("hook", stdin=rest, client="codex").stdout)
        self.assertEqual(handed.get("decision"), "block", "the tag was not handed over at rest")
        self.assertIn("please rebase onto main", handed["reason"])

        # The same message must not hold it a second time, or the session never rests.
        again = json.loads(self.cli("hook", stdin=rest, client="codex").stdout)
        self.assertNotIn("decision", again, "the same tag held the session twice")

    def test_the_delivery_policy_still_governs_handing_over_at_rest(self):
        lane = Path(self.proj).parent / "lane-two"
        rest = json.dumps({"hook_event_name": "Stop", "cwd": str(lane), "session_id": "sess-api"})
        self.cli("hook", stdin=rest, client="codex")
        self.cli("option-set", "--cwd", str(self.proj), "--namespace", "delivery_policy",
                 "--key", "wake-on-tag", "--value", "off")
        try:
            self.cli("post", "--cwd", str(self.proj), "--sender", "@ui-agent", "--kind", "message",
                     "--topic", "nav", "--message", "@api-agent this must not interrupt you")
            answer = json.loads(self.cli("hook", stdin=rest, client="codex").stdout)
            self.assertNotIn("decision", answer, "wake_on_tag=off still interrupted a session")
        finally:
            self.cli("option-set", "--cwd", str(self.proj), "--namespace", "delivery_policy",
                     "--key", "wake-on-tag", "--value", "direct")

    # --- the surface as a whole ----------------------------------------------

    def test_every_read_only_subcommand_runs_and_answers(self):
        """A command that crashes on a real project is not covered by a store-level test."""
        for name in ("status", "targets", "members", "threads", "options", "read",
                     "alerts", "chats", "rules", "projects", "doctor"):
            with self.subTest(command=name):
                done = self.cli(name, "--cwd", str(self.proj))
                self.assertNotIn("Traceback", done.stderr)
                json.loads(done.stdout)
        self.assertIn("BACKLOG", self.cli("board", "--cwd", str(self.proj)).stdout)

    def test_spend_says_what_it_needs_when_the_index_is_absent(self):
        """`spend` is the one read that cannot degrade — it has no token data without the
        index. It must fail with an instruction rather than a traceback, and say so in the
        protocol document, because everything else keeps working without the extra."""
        done = self.run_cli("spend", "--cwd", str(self.proj))
        if done.returncode == 0:
            self.assertIn("worktrees", json.loads(done.stdout))
            return
        self.assertNotIn("Traceback", done.stderr)
        self.assertIn("chat-room[index]", done.stderr)
        protocol = (Path(__file__).resolve().parents[1] / "docs" / "protocol.md").read_text(encoding="utf-8")
        row = [line for line in protocol.splitlines() if "`chat-room spend`" in line][0]
        self.assertIn("index", row, "protocol.md does not say that spend needs the optional index")

    def test_printing_never_fails_on_the_content_it_is_asked_to_print(self):
        """The board is drawn with box characters and room messages are user text.

        A console on a legacy code page must degrade the glyph, not kill the command —
        `chat-room board` did exactly that on Windows until stdout was reconfigured.
        """
        self.cli("post", "--cwd", str(self.proj), "--kind", "message", "--topic", "unicode",
                 "--message", "handoff ready — naïve café 日本語 ✅")
        legacy = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        for command in (["board", "--cwd", str(self.proj)], ["read", "--cwd", str(self.proj), "--limit", "5"]):
            with self.subTest(command=command[0]):
                done = subprocess.run([sys.executable, str(ROOM), "--data-dir", str(self.data), *command],
                                      text=True, capture_output=True, timeout=120, env=legacy)
                self.assertEqual(done.returncode, 0, f"{command[0]} failed on a legacy code page: {done.stderr.strip()}")
                self.assertNotIn("UnicodeEncodeError", done.stderr)

    def test_the_process_boundary_covers_every_subcommand(self):
        """Every subcommand is either exercised here or named as deliberately uncovered.

        Without this, a command added later gets store-level coverage and never runs as a
        process, which is exactly the gap that hid the conflict regression.
        """
        listing = self.run_cli("--help").stdout
        advertised = set(re.findall(r"[{,]([a-z][a-z-]*)", listing.split("positional arguments")[0]))
        source = Path(__file__).read_text(encoding="utf-8")
        exercised = set(re.findall(r'"([a-z][a-z-]*)", "--cwd"', source)) | set(re.findall(r'cli\("([a-z][a-z-]*)"', source))
        exercised |= set(re.findall(r'for name in \(([^)]*)\)', source, re.S)[0].replace('"', "").replace("\n", "").split(", "))
        # Commands that start vendor turns, need a TTY, or speak a wire protocol are covered
        # at store level instead; running them here would bill tokens or hang.
        deliberately_uncovered = {"chat", "codex", "mcp", "start", "send", "stop", "index",
                                  "search", "post", "thread-open", "thread-close", "rename",
                                  "identify", "hook", "option-set"}
        missing = advertised - exercised - deliberately_uncovered
        self.assertEqual(missing, set(), f"subcommands never run as a process: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main()
