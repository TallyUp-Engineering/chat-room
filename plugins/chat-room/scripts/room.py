#!/usr/bin/env python3
"""Chat Room: a local, advisory message bus for Git worktrees and agents."""

from __future__ import annotations

import argparse
import base64
import binascii
import errno
import hashlib
import json
import os
import plistlib
import queue
import re
import select
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


PLUGIN_NAME = "chat-room"
VERSION = "0.6.0"
SCHEMA_VERSION = 6
ACTIVE_WINDOW_SECONDS = 30 * 60
WAKE_ENDPOINT_ENV = "CHAT_ROOM_WAKE_ENDPOINT"
CLIENT_ENV = "CHAT_ROOM_CLIENT"
DATA_ENV = "CHAT_ROOM_DATA"
MESSAGE_KINDS = (
    "allocation", "request", "decision", "observation", "update",
    "blocker", "defect", "handoff", "authority", "proposal", "message",
)
CONTEXT_EVENTS = ("SessionStart", "UserPromptSubmit", "SubagentStart")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:glpat|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:password|passwd|access[_-]?token|secret[_-]?key)\s*[=:]\s*\S{8,}"),
)
AGENT_MENTION = re.compile(r"(?<![A-Za-z0-9_@])@\s*([a-z][a-z0-9-]{0,63})", re.I)
WORKTREE_MENTION = re.compile(r"(?<![A-Za-z0-9_#])#([a-z][a-z0-9-]{0,63})", re.I)
WAKE_PROMPT = (
    "You were explicitly tagged in Chat Room while idle. Read the injected "
    "coordination context, re-observe repository state before acting, and reply in the room when useful."
)
CHAT_CATALOG_TTL_SECONDS = 15
CHAT_CATALOG_LOCK = threading.Lock()
CHAT_CATALOGS: Dict[str, Tuple[float, List[Dict[str, Any]], Dict[Tuple[str, str], Path]]] = {}
CHAT_DELIVERY_LOCK = threading.Lock()
CHAT_DELIVERIES: Dict[Tuple[str, str], Dict[str, Any]] = {}
MAX_CHAT_IMAGE_BYTES = 10 * 1024 * 1024
MAX_CHAT_IMAGES = 5
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
CONFLICT_SCAN_TTL_SECONDS = 30
MAX_CONFLICT_PROBES = 40
# One tag must not be able to bill a vendor turn on a loop.
WAKE_COOLDOWN_SECONDS = 60
# ponytail: the browser re-fetches this on every change signal, so it is a window, not
# the archive. Raise it, or page the room log, if a room ever needs deeper scrollback.
SNAPSHOT_MESSAGE_LIMIT = 300
CONFLICT_SCAN_LOCK = threading.Lock()
CONFLICT_SCANS: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
CONFLICT_SCANS_RUNNING: Set[str] = set()


# One source of truth for the shape. `_migrate` applies it and the doctor builds a throwaway
# reference database from it, so "expected columns" can never drift from what is created.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rooms(room_id TEXT PRIMARY KEY,project_identity TEXT NOT NULL,common_dir TEXT NOT NULL,repository_root TEXT NOT NULL,created_at TEXT NOT NULL,last_seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,room_id TEXT NOT NULL,timestamp TEXT NOT NULL,session_id TEXT,sender TEXT NOT NULL,recipients_json TEXT NOT NULL,kind TEXT NOT NULL,topic TEXT NOT NULL,status TEXT NOT NULL,message TEXT NOT NULL,cwd TEXT,worktree TEXT,branch TEXT,head TEXT,metadata_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_room_id_id ON messages(room_id,id);
CREATE TABLE IF NOT EXISTS presence(room_id TEXT NOT NULL,participant_id TEXT NOT NULL,session_id TEXT,agent_id TEXT,role TEXT NOT NULL,state TEXT NOT NULL,cwd TEXT NOT NULL,worktree TEXT NOT NULL,branch TEXT,head TEXT,started_at TEXT NOT NULL,seen_at TEXT NOT NULL,last_event TEXT NOT NULL,handle TEXT,wake_endpoint TEXT,PRIMARY KEY(room_id,participant_id));
CREATE TABLE IF NOT EXISTS handle_claims(room_id TEXT NOT NULL,participant_id TEXT NOT NULL,claimed_at TEXT NOT NULL,PRIMARY KEY(room_id,participant_id));
CREATE TABLE IF NOT EXISTS cursors(room_id TEXT NOT NULL,participant_id TEXT NOT NULL,last_message_id INTEGER NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(room_id,participant_id));
CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,room_id TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,status TEXT NOT NULL,title TEXT NOT NULL,reason TEXT NOT NULL,opener TEXT NOT NULL,participants_json TEXT NOT NULL,paths_json TEXT NOT NULL,source TEXT NOT NULL,metadata_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS threads_room_status_updated ON threads(room_id,status,updated_at DESC);
CREATE TABLE IF NOT EXISTS option_index(namespace TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,metadata_json TEXT NOT NULL,PRIMARY KEY(namespace,key));
"""


class RoomError(RuntimeError):
    pass


@dataclass(frozen=True)
class Repository:
    cwd: Path
    worktree: Path
    common_dir: Path
    remote: str
    project_identity: str
    room_id: str
    branch: Optional[str]
    head: Optional[str]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def recently_seen(value: str) -> bool:
    try:
        seen = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (datetime.now(timezone.utc) - seen).total_seconds() <= ACTIVE_WINDOW_SECONDS


def default_data_dir() -> Path:
    value = os.environ.get(DATA_ENV)
    return Path(value).expanduser().resolve() if value else (Path.home() / ".chat-room").resolve()


def run_git(cwd: Path, *args: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RoomError(f"cannot inspect Git workspace: {error}") from error
    if check and result.returncode != 0:
        raise RoomError((result.stderr or result.stdout).strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def normalize_remote(remote: str) -> Optional[Tuple[str, str]]:
    value = remote.strip()
    if not value:
        return None
    scp = re.match(r"^(?:[^@]+@)?([^:]+):(.+)$", value)
    if scp and "://" not in value:
        host, path = scp.group(1).lower(), scp.group(2)
    else:
        match = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", value, re.I)
        if not match:
            return None
        host, path = match.group(1).lower(), match.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return host, path


def resolve_repository(cwd_value: Optional[str]) -> Optional[Repository]:
    cwd = Path(cwd_value or os.getcwd()).expanduser().resolve()
    try:
        worktree = Path(run_git(cwd, "rev-parse", "--show-toplevel")).resolve()
        common_dir = Path(run_git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    except RoomError:
        return None
    remote = run_git(worktree, "remote", "get-url", "origin", check=False)
    if not remote:
        for remote_name in run_git(worktree, "remote", check=False).splitlines():
            remote = run_git(worktree, "remote", "get-url", remote_name, check=False)
            if remote:
                break
    normalized = normalize_remote(remote)
    if normalized:
        identity = f"git:{normalized[0]}/{normalized[1]}"
    else:
        identity = f"git-local:{common_dir}"
    room_id = hashlib.sha256(f"{identity}\n{common_dir}".encode()).hexdigest()[:20]
    return Repository(
        cwd=cwd, worktree=worktree, common_dir=common_dir, remote=remote,
        project_identity=identity, room_id=room_id,
        branch=run_git(worktree, "branch", "--show-current", check=False) or None,
        head=run_git(worktree, "rev-parse", "HEAD", check=False) or None,
    )


def json_lines(path: Path) -> Iterator[Dict[str, Any]]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def concise(value: Any, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def path_belongs_to_room(value: Any, repo: Repository, cache: Dict[str, bool]) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text in cache:
        return cache[text]
    candidate = Path(text).expanduser().resolve()
    project_root = repo.common_dir.parent.resolve()
    if candidate == project_root or project_root in candidate.parents:
        cache[text] = True
        return True
    observed = resolve_repository(str(candidate)) if candidate.exists() else None
    result = bool(observed and observed.common_dir == repo.common_dir)
    cache[text] = result
    return result


def iso_from_mtime(path: Path) -> str:
    try:
        value = path.stat().st_mtime
    except OSError:
        value = 0
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def chat_recency(updated_at: str) -> str:
    try:
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        age_days = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds() / 86400)
    except (TypeError, ValueError):
        return "inactive"
    if age_days < 7:
        return "recent"
    if age_days < 30:
        return "stale"
    return "inactive"


def discover_chat_catalog(repo: Repository, force: bool = False) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], Path]]:
    now = time.monotonic()
    with CHAT_CATALOG_LOCK:
        cached = CHAT_CATALOGS.get(repo.room_id)
        if cached and not force and now - cached[0] < CHAT_CATALOG_TTL_SECONDS:
            return cached[1], cached[2]

    home = Path.home()
    summaries: List[Dict[str, Any]] = []
    files: Dict[Tuple[str, str], Path] = {}
    path_cache: Dict[str, bool] = {}

    codex_titles: Dict[str, Dict[str, Any]] = {}
    for item in json_lines(Path(os.environ.get("CODEX_HOME", home / ".codex")) / "session_index.jsonl"):
        session_id = str(item.get("id") or "")
        if session_id:
            codex_titles[session_id] = item
    codex_root = Path(os.environ.get("CODEX_HOME", home / ".codex")) / "sessions"
    for path in codex_root.glob("*/*/*/*.jsonl") if codex_root.exists() else []:
        meta = next((item.get("payload") for item in json_lines(path) if item.get("type") == "session_meta"), None)
        if not isinstance(meta, dict):
            continue
        session_id = str(meta.get("id") or meta.get("session_id") or "")
        cwd = str(meta.get("cwd") or "")
        if not session_id or not path_belongs_to_room(cwd, repo, path_cache):
            continue
        indexed = codex_titles.get(session_id, {})
        updated = str(indexed.get("updated_at") or iso_from_mtime(path))
        summaries.append({
            "client": "Codex", "id": session_id,
            "title": concise(indexed.get("thread_name") or f"Codex chat {session_id[:8]}"),
            "updated_at": updated, "recency": chat_recency(updated),
            "worktree": Path(cwd).name or "worktree", "cwd": cwd, "read_only": True,
        })
        files[("codex", session_id)] = path

    claude_history: Dict[str, Dict[str, Any]] = {}
    for item in json_lines(home / ".claude" / "history.jsonl"):
        session_id = str(item.get("sessionId") or "")
        if not session_id:
            continue
        previous = claude_history.get(session_id, {})
        if int(item.get("timestamp") or 0) >= int(previous.get("timestamp") or 0):
            claude_history[session_id] = item
    claude_root = home / ".claude" / "projects"
    for path in claude_root.glob("*/*.jsonl") if claude_root.exists() else []:
        session_id = path.stem
        indexed = claude_history.get(session_id, {})
        project = str(indexed.get("project") or "")
        if not project:
            project = str(next((item.get("cwd") for item in json_lines(path) if item.get("cwd")), ""))
        if not path_belongs_to_room(project, repo, path_cache):
            continue
        timestamp = int(indexed.get("timestamp") or 0)
        updated = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat().replace("+00:00", "Z") if timestamp else iso_from_mtime(path)
        title = concise(indexed.get("display") or f"Claude chat {session_id[:8]}")
        summaries.append({
            "client": "Claude", "id": session_id, "title": title,
            "updated_at": updated, "recency": chat_recency(updated), "worktree": Path(project).name or "worktree", "cwd": project, "read_only": True,
        })
        files[("claude", session_id)] = path

    summaries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    with CHAT_CATALOG_LOCK:
        CHAT_CATALOGS[repo.room_id] = (now, summaries, files)
    return summaries, files


def text_content(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    parts = [str(item.get("text") or "") for item in value if isinstance(item, dict) and item.get("type") == "text"]
    return "\n".join(part for part in parts if part.strip()).strip()


def chat_transcript(repo: Repository, client: str, session_id: str) -> Dict[str, Any]:
    summaries, files = discover_chat_catalog(repo)
    key = (client.lower(), session_id)
    path = files.get(key)
    summary = next((item for item in summaries if item["client"].lower() == key[0] and item["id"] == session_id), None)
    if path is None or summary is None:
        raise RoomError("local chat session was not found in this Git project")
    messages: List[Dict[str, str]] = []
    if key[0] == "codex":
        for item in json_lines(path):
            if item.get("type") != "event_msg" or not isinstance(item.get("payload"), dict):
                continue
            payload = item["payload"]
            role = {"user_message": "user", "agent_message": "assistant"}.get(str(payload.get("type") or ""))
            body = str(payload.get("message") or "").strip()
            if role and body:
                messages.append({"role": role, "body": body, "timestamp": str(item.get("timestamp") or "")})
    elif key[0] == "claude":
        for item in json_lines(path):
            role = str(item.get("type") or "")
            if role not in ("user", "assistant") or item.get("isSidechain"):
                continue
            message = item.get("message") if isinstance(item.get("message"), dict) else {}
            body = text_content(message.get("content"))
            if body:
                messages.append({"role": role, "body": body, "timestamp": str(item.get("timestamp") or "")})
    return {"chat": summary, "messages": messages}


def slug(value: str, fallback: str = "target", limit: int = 64) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return (result or fallback)[:limit].rstrip("-")


def target_token(value: str, default_prefix: str = "@") -> str:
    text = value.strip().lower()
    prefix = text[0] if text[:1] in ("@", "#") else default_prefix
    return prefix + slug(text[1:] if text[:1] in ("@", "#") else text)


def mentioned_targets(message: str) -> List[str]:
    values = ["@" + value.lower() for value in AGENT_MENTION.findall(message)]
    values.extend("#" + value.lower() for value in WORKTREE_MENTION.findall(message))
    return list(dict.fromkeys(values))


def ensure_value_free(value: str) -> str:
    text = value.strip()
    if not text:
        raise RoomError("message must not be empty")
    if len(text) > 4000:
        raise RoomError("message exceeds 4000 characters")
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise RoomError("message resembles credential material; room posts must be value-free")
    return text


def worktree_target(path: Path) -> str:
    return "#" + slug(path.name, "root")


def list_worktree_references(repo: Repository) -> List[Dict[str, Any]]:
    output = run_git(repo.worktree, "worktree", "list", "--porcelain")
    items: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for line in output.splitlines() + [""]:
        if not line:
            if current.get("path"):
                path = Path(str(current["path"])).resolve()
                current.update(path=str(path), name=path.name, target=worktree_target(path))
                items.append(current)
            current = {}
        elif line.startswith("worktree "):
            current["path"] = line[9:]
        elif line.startswith("branch "):
            current["branch"] = line[7:].removeprefix("refs/heads/")
        elif line == "detached":
            current["detached"] = True
    branch_dates: Dict[str, str] = {}
    for line in run_git(repo.worktree, "for-each-ref", "--format=%(refname:short)%09%(committerdate:iso-strict)", "refs/heads", check=False).splitlines():
        branch, separator, updated = line.partition("\t")
        if separator:
            branch_dates[branch] = updated
    for item in items:
        updated = branch_dates.get(str(item.get("branch") or ""), "")
        item["updated_at"] = updated or None
        try:
            observed = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            item["age_days"] = max(0, int((datetime.now(timezone.utc) - observed).total_seconds() // 86400))
        except (TypeError, ValueError):
            item["age_days"] = None
    return items


def changed_worktree_paths(path: Path) -> Set[str]:
    try:
        result = subprocess.run(["git", "-C", str(path), "status", "--porcelain=v1", "-z", "--untracked-files=all"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {entry[3:] for entry in result.stdout.split("\0") if len(entry) > 3 and entry[2] == " "}


def merge_conflict_paths(repo: Repository, left: str, right: str) -> Set[str]:
    """Paths Git reports as conflicting when the two branches are merged."""
    if not left or not right or left == right:
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", str(repo.worktree), "merge-tree", "--write-tree", "--name-only", left, right],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 1:
        # 0 merges cleanly; anything above 1 is an unusable ref or unrelated history.
        return set()
    conflicted: Set[str] = set()
    for line in result.stdout.splitlines()[1:]:
        if not line.strip():
            break
        conflicted.add(line)
    return conflicted


def scan_preemptive_conflicts(repo: Repository) -> None:
    try:
        worktrees = [item for item in list_worktree_references(repo) if Path(str(item["path"])).exists()]
        dirty = {str(item["path"]): changed_worktree_paths(Path(str(item["path"]))) for item in worktrees}
        # ponytail: separate worktrees are separate checkouts, so two of them holding the
        # same relative path is the normal workflow, not a conflict. Shared dirty paths only
        # nominate a pair; Git decides whether the branches actually collide. Probes are
        # capped because the pairing is O(n^2) — raise MAX_CONFLICT_PROBES if a large project
        # starts missing pairs, or key the cache on branch heads to skip unchanged pairs.
        by_path: Dict[str, List[Dict[str, Any]]] = {}
        probes = 0
        for index, left in enumerate(worktrees):
            for right in worktrees[index + 1:]:
                if probes >= MAX_CONFLICT_PROBES:
                    break
                if not dirty[str(left["path"])] & dirty[str(right["path"])]:
                    continue
                probes += 1
                for value in merge_conflict_paths(repo, str(left.get("branch") or ""), str(right.get("branch") or "")):
                    group = by_path.setdefault(value, [])
                    group.extend(item for item in (left, right) if item not in group)
        conflicts = [{"path": path, "worktrees": worktrees_for} for path, worktrees_for in by_path.items()]
        with CONFLICT_SCAN_LOCK:
            CONFLICT_SCANS[repo.room_id] = (time.monotonic(), conflicts)
    finally:
        with CONFLICT_SCAN_LOCK:
            CONFLICT_SCANS_RUNNING.discard(repo.room_id)


def preemptive_conflicts(repo: Repository) -> List[Dict[str, Any]]:
    now = time.monotonic()
    with CONFLICT_SCAN_LOCK:
        cached = CONFLICT_SCANS.get(repo.room_id)
        if cached and now - cached[0] < CONFLICT_SCAN_TTL_SECONDS:
            return cached[1]
        if repo.room_id not in CONFLICT_SCANS_RUNNING:
            CONFLICT_SCANS_RUNNING.add(repo.room_id)
            threading.Thread(target=scan_preemptive_conflicts, args=(repo,), daemon=True).start()
        return cached[1] if cached else []


def coordination_alerts(targets: Dict[str, Any], threads: Sequence[Dict[str, Any]], options: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []
    agents = list(targets.get("agents") or [])
    for worktree in targets.get("worktrees") or []:
        count = int(worktree.get("active_agents") or 0)
        if count < 2:
            continue
        participants = [str(worktree["target"])]
        participants.extend(str(agent["target"]) for agent in agents if str(agent.get("worktree") or "") == str(worktree.get("path") or ""))
        alerts.append({
            "id": "shared-worktree-" + hashlib.sha256(str(worktree["path"]).encode()).hexdigest()[:10],
            "type": "shared-worktree", "severity": "warning", "icon": "shared-worktree",
            "title": f"{count} workers in {worktree['target']}",
            "detail": "Coordinate ownership before either actor writes.",
            "participants": participants, "paths": [], "thread_id": None,
        })
    for thread in threads:
        if thread["source"] == "preemptive-conflict":
            alert_type, icon, severity = "file-overlap", "file-overlap", "warning"
        elif thread["reason"] == "design direction":
            alert_type, icon, severity = "decision-needed", "decision-needed", "attention"
        elif thread["reason"] == "blocker":
            alert_type, icon, severity = "blocker", "blocker", "critical"
        elif thread["reason"] == "handoff":
            alert_type, icon, severity = "handoff", "handoff", "attention"
        else:
            continue
        alerts.append({
            "id": f"{alert_type}-{thread['id']}", "type": alert_type, "severity": severity, "icon": icon,
            "title": thread["title"], "detail": thread["reason"], "participants": thread["participants"],
            "paths": thread["paths"], "thread_id": thread["id"],
        })
    policy = next((item for item in (options or {}).get("notification_policy", []) if item.get("key") == "stale_worktree_days"), {"value": "30"})
    try:
        stale_days = max(1, int(str(policy.get("value") or "30")))
    except ValueError:
        stale_days = 30
    stale = [worktree for worktree in targets.get("worktrees") or [] if not int(worktree.get("active_agents") or 0) and isinstance(worktree.get("age_days"), int) and int(worktree["age_days"]) >= stale_days]
    if stale:
        alerts.append({
            "id": "potentially-stale-worktrees", "type": "stale-worktrees", "severity": "attention", "icon": "stale-worktree",
            "title": f"{len(stale)} potentially stale worktree{'s' if len(stale) != 1 else ''}",
            "detail": f"No active actor and last branch commit at least {stale_days} days ago. Route an investigation before disposition.",
            "participants": ["@human", *[str(item["target"]) for item in stale]],
            "paths": [str(item["path"]) for item in stale], "thread_id": None,
        })
    return alerts


def client_name() -> str:
    value = os.environ.get(CLIENT_ENV, "codex").strip().lower()
    return value if value in ("codex", "claude", "human") else "agent"


def find_cli_executable(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    candidates = (
        Path.home() / ".local" / "bin" / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
    )
    return next((str(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def endpoint_socket_path(endpoint: str) -> Path:
    if not endpoint.startswith("unix:///"):
        raise RoomError("wake endpoint must be an absolute local Unix socket")
    return Path(endpoint[len("unix://"):]).resolve()


def socket_ready(endpoint: str) -> bool:
    try:
        path = endpoint_socket_path(endpoint)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(.15)
        client.connect(str(path))
        client.close()
        return True
    except (OSError, RoomError):
        return False


def _read_exact(connection: socket.socket, length: int) -> bytes:
    value = bytearray()
    while len(value) < length:
        part = connection.recv(length - len(value))
        if not part:
            raise RoomError("Codex app server closed the connection")
        value.extend(part)
    return bytes(value)


def _send_frame(connection: socket.socket, payload: bytes) -> None:
    mask = os.urandom(4)
    length = len(payload)
    header = bytearray([0x81])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.extend([0x80 | 126]); header.extend(struct.pack("!H", length))
    else:
        header.extend([0x80 | 127]); header.extend(struct.pack("!Q", length))
    header.extend(mask)
    connection.sendall(bytes(header) + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))


def _recv_frame(connection: socket.socket) -> Dict[str, Any]:
    first, second = _read_exact(connection, 2)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(connection, 8))[0]
    mask = _read_exact(connection, 4) if second & 0x80 else b""
    payload = _read_exact(connection, length)
    if mask:
        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    if first & 0x0F == 8:
        raise RoomError("Codex app server closed the wake connection")
    return json.loads(payload.decode())


def wake_codex(endpoint: str, session_id: str, prompt: str = WAKE_PROMPT, images: Sequence[Path] = ()) -> None:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(6)
    try:
        connection.connect(str(endpoint_socket_path(endpoint)))
        key = base64.b64encode(os.urandom(16)).decode()
        request = f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        connection.sendall(request.encode())
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(connection.recv(4096))
        if not response.startswith(b"HTTP/1.1 101"):
            raise RoomError("Codex app server rejected the wake connection")
        def call(request_id: int, method: str, params: Dict[str, Any]) -> None:
            _send_frame(connection, json.dumps({"id": request_id, "method": method, "params": params}, separators=(",", ":")).encode())
            while True:
                item = _recv_frame(connection)
                if item.get("id") == request_id:
                    if item.get("error"):
                        raise RoomError(str(item["error"]))
                    return
        call(1, "initialize", {"clientInfo": {"name": PLUGIN_NAME, "version": VERSION}})
        _send_frame(connection, b'{"method":"initialized","params":{}}')
        inputs: List[Dict[str, str]] = [{"type": "text", "text": prompt}]
        inputs.extend({"type": "localImage", "path": str(path)} for path in images)
        call(2, "turn/start", {"threadId": session_id, "input": inputs})
    finally:
        connection.close()


def chat_delivery_state(summary: Dict[str, Any], members: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    client = str(summary.get("client") or "").lower()
    session_id = str(summary.get("id") or "")
    member = next((item for item in members if str(item.get("session_id") or "") == session_id and str(item.get("state") or "") != "offline"), None)
    key = (client, session_id)
    with CHAT_DELIVERY_LOCK:
        delivery = CHAT_DELIVERIES.get(key)
        process = delivery.get("process") if delivery else None
        running = bool(process and process.poll() is None)
        if delivery and not running:
            for path in delivery.get("attachments") or []:
                try: Path(path).unlink()
                except OSError: pass
            CHAT_DELIVERIES.pop(key, None)
    if running:
        return {"ready": False, "mode": "running", "label": "Agent is responding", "detail": "This conversation refreshes as the active agent responds."}
    if member:
        endpoint = str(member.get("wake_endpoint") or "")
        if client == "codex" and member.get("last_event") == "Stop" and endpoint and socket_ready(endpoint):
            return {"ready": True, "mode": "live", "label": "Ready to message", "detail": "Sends through this conversation's existing local connection."}
        return {"ready": False, "mode": "active-unattached", "label": "Active elsewhere", "detail": "This conversation already has an active writer, so it stays view-only here to prevent competing turns."}
    executable = find_cli_executable("codex" if client == "codex" else "claude" if client == "claude" else "")
    if executable:
        return {"ready": True, "mode": "resume", "label": "Ready to continue", "detail": f"Starts one local {summary['client']} turn in this stored conversation with its normal configuration and safety rules."}
    return {"ready": False, "mode": "unavailable", "label": "View only", "detail": f"No local {summary.get('client') or 'vendor'} conversation adapter is available on this machine."}


def materialize_chat_images(data_dir: Path, attachments: Sequence[Dict[str, Any]]) -> List[Path]:
    if len(attachments) > MAX_CHAT_IMAGES:
        raise RoomError(f"a chat turn supports at most {MAX_CHAT_IMAGES} images")
    root = data_dir / "chat-images"; root.mkdir(parents=True, exist_ok=True)
    result: List[Path] = []
    try:
        for item in attachments:
            mime = str(item.get("type") or "").lower()
            suffix = IMAGE_TYPES.get(mime)
            encoded = str(item.get("data") or "")
            if not suffix or not encoded:
                raise RoomError("unsupported or empty chat image")
            if encoded.startswith("data:"):
                prefix, separator, encoded = encoded.partition(",")
                if not separator or ";base64" not in prefix or not prefix.startswith(f"data:{mime};"):
                    raise RoomError("chat image data URL does not match its media type")
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as error:
                raise RoomError("chat image is not valid base64") from error
            if not payload or len(payload) > MAX_CHAT_IMAGE_BYTES:
                raise RoomError(f"each chat image must be between 1 byte and {MAX_CHAT_IMAGE_BYTES // (1024 * 1024)} MiB")
            path = root / f"{time.time_ns()}-{os.urandom(6).hex()}{suffix}"
            old = os.umask(0o077)
            try: path.write_bytes(payload)
            finally: os.umask(old)
            os.chmod(path, 0o600); result.append(path)
        return result
    except Exception:
        for path in result:
            try: path.unlink()
            except OSError: pass
        raise


def cleanup_chat_images(paths: Sequence[Path]) -> None:
    for path in paths:
        try: path.unlink()
        except OSError: pass


def spawn_cli_turn(data_dir: Path, client: str, worktree: Path, body: str, session_id: Optional[str] = None, images: Sequence[Path] = ()) -> Dict[str, Any]:
    """Run one local CLI turn: resume `session_id`, or start a new session when it is None.

    The prompt always travels on stdin, never in process arguments, so it stays out of the
    process table. Ordinary vendor configuration and sandbox rules still apply.
    """
    normalized = client.strip().lower()
    executable = find_cli_executable(normalized if normalized in ("codex", "claude") else "")
    if not executable:
        raise RoomError(f"the {client} CLI is not installed on this machine")
    if normalized == "codex":
        command = [executable, "exec"] + (["resume", "--all"] if session_id else [])
        for path in images:
            command.extend(["--image", str(path)])
        if session_id:
            command.append(session_id)
        command.append("-")
    elif normalized == "claude":
        command = [executable, "--print"] + (["--resume", session_id] if session_id else [])
        if images:
            body += "\n\nAttached local image files:\n" + "\n".join(f"- {path}" for path in images) + "\nUse these images as part of the request."
    else:
        raise RoomError("unsupported chat client")
    logs = data_dir / "chat-deliveries"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{normalized}-{slug(session_id or 'new-session', 'session')}.log"
    environment = os.environ.copy()
    environment[CLIENT_ENV] = normalized
    handle = open(log_path, "ab", buffering=0)
    try:
        process = subprocess.Popen(command, cwd=str(worktree), env=environment, stdin=subprocess.PIPE, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        if process.stdin is None:
            raise RoomError("CLI delivery stdin is unavailable")
        process.stdin.write((body + "\n").encode())
        process.stdin.close()
    finally:
        handle.close()
    return {"process": process, "log": str(log_path), "client": normalized}


def track_delivery(client: str, key: str, process: "subprocess.Popen[bytes]", images: Sequence[Path] = ()) -> None:
    with CHAT_DELIVERY_LOCK:
        CHAT_DELIVERIES[(client, key)] = {"process": process, "attachments": [str(path) for path in images], "started_at": time.monotonic()}


def running_delivery(client: str, key: str) -> bool:
    with CHAT_DELIVERY_LOCK:
        delivery = CHAT_DELIVERIES.get((client, key))
        process = delivery.get("process") if delivery else None
    return bool(process and process.poll() is None)


def stop_delivery(client: str, session_id: str) -> Dict[str, Any]:
    """Interrupt the local turn this room started — the Ctrl-C you give up by not holding a terminal."""
    with CHAT_DELIVERY_LOCK:
        delivery = CHAT_DELIVERIES.get((client.strip().lower(), session_id))
        process = delivery.get("process") if delivery else None
    if process is None or process.poll() is not None:
        raise RoomError("this room is not running a local turn for that session")
    process.terminate()
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        process.kill()
    return {"status": "stopped", "client": client.strip().lower(), "session_id": session_id}


def start_session(data_dir: Path, repo: Repository, client: str, worktree_value: Optional[str], prompt: str) -> Dict[str, Any]:
    """Open new agent work in a chosen worktree without a terminal."""
    body = ensure_value_free(prompt)
    normalized = client.strip().lower()
    if normalized not in ("codex", "claude"):
        raise RoomError("agent client must be codex or claude")
    worktree = Path(worktree_value).expanduser().resolve() if worktree_value else repo.worktree
    if not worktree.is_dir():
        raise RoomError("worktree path does not exist")
    if not path_belongs_to_room(worktree, repo, {}):
        raise RoomError("worktree does not belong to this project")
    started = spawn_cli_turn(data_dir, normalized, worktree, body)
    process = started["process"]
    track_delivery(normalized, f"new:{process.pid}", process)
    return {"status": "started", "client": normalized, "worktree": str(worktree), "worktree_target": worktree_target(worktree), "pid": process.pid, "log": started["log"]}


def start_chat_delivery(data_dir: Path, repo: Repository, client: str, session_id: str, message: str, members: Sequence[Dict[str, Any]], attachments: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    if not str(message).strip() and not attachments:
        raise RoomError("chat turn must include text or an image")
    body = ensure_value_free(message) if str(message).strip() else "Please review the attached image(s)."
    summaries, _files = discover_chat_catalog(repo, force=True)
    summary = next((item for item in summaries if str(item["client"]).lower() == client.lower() and item["id"] == session_id), None)
    if summary is None:
        raise RoomError("local chat session was not found in this Git project")
    delivery = chat_delivery_state(summary, members)
    if not delivery["ready"]:
        raise RoomError(str(delivery["detail"]))
    images = materialize_chat_images(data_dir, attachments)
    active = next((item for item in members if str(item.get("session_id") or "") == session_id and str(item.get("state") or "") != "offline"), None)
    if delivery["mode"] == "live" and active:
        try: wake_codex(str(active["wake_endpoint"]), session_id, body, images)
        except Exception:
            cleanup_chat_images(images)
            raise
        threading.Timer(300, cleanup_chat_images, args=(images,)).start()
        return {"status": "sent", "mode": "live", "session_id": session_id}
    normalized = str(summary["client"]).lower()
    worktree = Path(str(summary.get("cwd") or repo.worktree)).expanduser().resolve()
    if not path_belongs_to_room(worktree, repo, {}):
        cleanup_chat_images(images)
        raise RoomError("chat worktree no longer belongs to this project")
    try:
        started = spawn_cli_turn(data_dir, normalized, worktree, body, session_id, images)
    except Exception:
        cleanup_chat_images(images)
        raise
    process = started["process"]
    track_delivery(normalized, session_id, process, images)
    return {"status": "started", "mode": "resume", "session_id": session_id, "pid": process.pid, "images": len(images)}


class RoomStore:
    def __init__(self, data_dir: Path) -> None:
        self.behind = False
        self.data_dir = data_dir.expanduser().resolve()
        old = os.umask(0o077)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        finally:
            os.umask(old)
        self.database_path = self.data_dir / "chat-room.sqlite3"
        self.connection = sqlite3.connect(str(self.database_path), timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        try:
            os.chmod(self.database_path, 0o600)
        except OSError:
            pass

    def __enter__(self) -> "RoomStore": return self
    def __exit__(self, *_args: Any) -> None: self.connection.close()

    def installed_schema(self) -> int:
        try:
            row = self.connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(str(row["value"])) if row and str(row["value"]).isdigit() else 0

    def _require_writable(self) -> None:
        if self.behind:
            raise RoomError(
                f"this room database was written by a newer chat-room (schema {self.installed_schema()}, this copy speaks {SCHEMA_VERSION}); "
                "reads still work — upgrade this copy to write. Run `chat-room doctor` for the details."
            )

    def _migrate(self) -> None:
        installed = self.installed_schema()
        if installed > SCHEMA_VERSION:
            # A newer chat-room owns this database. Reading stays safe; writing would put back
            # a shape it has already moved past, which is how one stale copy corrupts everyone.
            self.behind = True
            return
        self.connection.executescript(SCHEMA_SQL)
        defaults = (
            ("worktree_action", "investigate", "Investigate unmerged work", {"order": 10, "prompt": "Inspect the referenced worktree or conflict, report unique unmerged work and evidence, and recommend a safe disposition. Do not mutate Git."}),
            ("worktree_action", "consolidate", "Consolidate", {"order": 20, "prompt": "Compare the referenced work against current authority, identify the smallest consumer-closed consolidation, and report the exact safe sequence before changing Git."}),
            ("worktree_action", "delete", "Delete after proof", {"order": 30, "prompt": "Prove the referenced work has no unique reachable value and report a recoverable deletion plan. Do not delete until the human explicitly approves the exact targets."}),
            ("notification_policy", "stale_worktree_days", "30", {"label": "Potentially stale after days"}),
            ("delivery_policy", "wake_on_tag", "direct", {"label": "Carry tags into idle sessions", "choices": "off, direct, all"}),
        )
        self.connection.executemany("INSERT OR IGNORE INTO option_index(namespace,key,value,metadata_json) VALUES(?,?,?,?)", [(namespace, key, value, json.dumps(metadata, sort_keys=True)) for namespace, key, value, metadata in defaults])
        row = self.connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        installed = int(str(row["value"])) if row and str(row["value"]).isdigit() else 0
        if installed < 5:
            # Schema 4 seeded 2 days, contradicting the documented and displayed 30.
            self.connection.execute("UPDATE option_index SET value='30' WHERE namespace='notification_policy' AND key='stale_worktree_days' AND value='2'")
        # A 0.6 pre-release widened presence in place. Every other version of this script on
        # the machine still writes it positionally, so the extra column broke their hooks the
        # moment they touched the shared database. Narrow it back and keep claims beside it.
        if any(str(row["name"]) == "claimed" for row in self.connection.execute("PRAGMA table_info(presence)")):
            self.connection.execute(
                "INSERT OR IGNORE INTO handle_claims(room_id,participant_id,claimed_at)"
                " SELECT room_id,participant_id,seen_at FROM presence WHERE claimed=1"
            )
            try:
                self.connection.execute("ALTER TABLE presence DROP COLUMN claimed")
            except sqlite3.OperationalError:
                pass
        self.connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        self.connection.commit()

    def register_room(self, repo: Repository) -> None:
        if self.behind:
            return  # A heartbeat, not user intent; a stale copy simply stops touching it.
        now = utc_now()
        self.connection.execute("""INSERT INTO rooms(room_id,project_identity,common_dir,repository_root,created_at,last_seen_at) VALUES(?,?,?,?,?,?) ON CONFLICT(room_id) DO UPDATE SET project_identity=excluded.project_identity,common_dir=excluded.common_dir,repository_root=excluded.repository_root,last_seen_at=excluded.last_seen_at""", (repo.room_id, repo.project_identity, str(repo.common_dir), str(repo.worktree), now, now))
        self.connection.commit()

    def options(self) -> Dict[str, List[Dict[str, Any]]]:
        rows = self.connection.execute("SELECT * FROM option_index ORDER BY namespace,key").fetchall()
        result: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(str(row["namespace"]), []).append({"key": row["key"], "value": row["value"], "metadata": json.loads(row["metadata_json"])})
        return result

    def option(self, namespace: str, key: str) -> Dict[str, Any]:
        row = self.connection.execute("SELECT * FROM option_index WHERE namespace=? AND key=?", (namespace, key)).fetchone()
        if row is None:
            raise RoomError(f"unknown indexed option: {namespace}/{key}")
        return {"namespace": row["namespace"], "key": row["key"], "value": row["value"], "metadata": json.loads(row["metadata_json"])}

    def set_option(self, namespace: str, key: str, value: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._require_writable()
        clean_namespace = re.sub(r"[^a-z0-9_-]+", "-", namespace.strip().lower()).strip("-") or "options"
        clean_key = slug(key, "option")
        clean_value = concise(ensure_value_free(value), 160)
        metadata_json = json.dumps(metadata or {}, sort_keys=True)
        ensure_value_free(metadata_json)
        self.connection.execute("INSERT INTO option_index(namespace,key,value,metadata_json) VALUES(?,?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value,metadata_json=excluded.metadata_json", (clean_namespace, clean_key, clean_value, metadata_json))
        self.connection.commit()
        return self.option(clean_namespace, clean_key)

    def display_name(self, namespace: str, key: str, fallback: str) -> str:
        try:
            return str(self.option(namespace, slug(key))["value"])
        except RoomError:
            return fallback

    def rename(self, repo: Repository, kind: str, reference: str, label: str, client: str = "") -> Dict[str, Any]:
        clean = concise(ensure_value_free(label), 120)
        if kind == "room":
            value = self.set_option("room_name", repo.room_id, clean, {"kind": "room"})
        elif kind == "chat":
            summaries, _files = discover_chat_catalog(repo)
            if not any(item["id"] == reference and str(item["client"]).lower() == client.lower() for item in summaries):
                raise RoomError("local chat session was not found in this Git project")
            value = self.set_option("chat_name", f"{client}-{reference}", clean, {"kind": "chat", "client": client, "session_id": reference})
        elif kind == "channel":
            self.thread(repo, reference)
            self.connection.execute("UPDATE threads SET title=?,updated_at=? WHERE room_id=? AND id=?", (clean, utc_now(), repo.room_id, reference))
            self.connection.commit()
            return {"kind": kind, "reference": reference, "label": clean}
        else:
            raise RoomError("only rooms, channels, and chats can be renamed")
        return {"kind": kind, "reference": reference, "label": value["value"]}

    def route_notification(self, repo: Repository, title: str, alert_type: str, actor: str, action_key: str, participants: Sequence[str], paths: Sequence[str]) -> Dict[str, Any]:
        action = self.option("worktree_action", action_key)
        selected_actor = target_token(actor)
        routed = ["@human", selected_actor, *participants]
        thread = self.open_thread(repo, title, f"{alert_type}: {action['value']}", "@human", routed, paths, "notification-route", metadata={"audience": "human-loop", "origin": "notification"})
        tags = " ".join(thread["participants"])
        prompt = str(action["metadata"].get("prompt") or action["value"])
        self.post_thread(repo, thread["id"], "@human", f"{tags} {prompt} Notification: {thread['title']}.")
        return thread

    def latest_repository(self) -> Optional[Repository]:
        row = self.connection.execute("SELECT repository_root FROM rooms ORDER BY last_seen_at DESC LIMIT 1").fetchone()
        return resolve_repository(row["repository_root"]) if row else None

    def unique_handle(self, room_id: str, participant_id: str, preferred: str) -> str:
        """Settle a handle at write time so a target never moves between sessions."""
        rows = self.connection.execute("SELECT handle FROM presence WHERE room_id=? AND participant_id<>? AND state<>'offline'", (room_id, participant_id)).fetchall()
        if preferred not in {str(row["handle"]) for row in rows}:
            return preferred
        return f"{preferred}-{hashlib.sha256(participant_id.encode()).hexdigest()[:6]}"

    def upsert_presence(self, repo: Repository, participant_id: str, session_id: Optional[str], agent_id: Optional[str], role: str, state: str, event: str, wake_endpoint: Optional[str] = None) -> None:
        self._require_writable()
        self.register_room(repo)
        now = utc_now()
        previous = self.connection.execute("SELECT handle,started_at FROM presence WHERE room_id=? AND participant_id=?", (repo.room_id, participant_id)).fetchone()
        claimed = self.connection.execute("SELECT 1 FROM handle_claims WHERE room_id=? AND participant_id=?", (repo.room_id, participant_id)).fetchone() is not None
        if claimed and previous and previous["handle"]:
            handle = str(previous["handle"])
        else:
            # A generated handle follows its role, so a session that moves worktree stops
            # advertising the old one. Uniqueness is settled here, never at read time.
            handle = self.unique_handle(repo.room_id, participant_id, slug(role.replace(":", "-"), "agent"))
        started = str(previous["started_at"]) if previous else now
        self.connection.execute("""INSERT INTO presence(room_id,participant_id,session_id,agent_id,role,state,cwd,worktree,branch,head,started_at,seen_at,last_event,handle,wake_endpoint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(room_id,participant_id) DO UPDATE SET session_id=excluded.session_id,agent_id=excluded.agent_id,role=excluded.role,state=excluded.state,cwd=excluded.cwd,worktree=excluded.worktree,branch=excluded.branch,head=excluded.head,seen_at=excluded.seen_at,last_event=excluded.last_event,handle=excluded.handle,wake_endpoint=excluded.wake_endpoint""", (repo.room_id, participant_id, session_id, agent_id, role, state, str(repo.cwd), str(repo.worktree), repo.branch, repo.head, started, now, event, handle, wake_endpoint))
        self.connection.execute("DELETE FROM presence WHERE room_id=? AND state='offline' AND seen_at<?", (repo.room_id, (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")))
        self.connection.commit()

    def members(self, room_id: str) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM presence WHERE room_id=? ORDER BY seen_at DESC", (room_id,)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            if value["state"] == "online" and not recently_seen(value["seen_at"]): value["state"] = "idle"
            endpoint_live = bool(value.get("wake_endpoint") and socket_ready(str(value["wake_endpoint"])))
            value["target"] = "@" + str(value.get("handle") or "agent")
            value["worktree_target"] = worktree_target(Path(value["worktree"]))
            value["wakeable_idle"] = bool(value["state"] in ("online", "idle") and endpoint_live and value.get("last_event") == "Stop" and str(value.get("role", "")).startswith("codex:"))
            result.append(value)
        return result

    def claim_handle(self, repo: Repository, session_ref: str, requested: str) -> Dict[str, Any]:
        self._require_writable()
        handle = target_token(requested)[1:]
        active = [m for m in self.members(repo.room_id) if m["state"] != "offline"]
        matches = [m for m in active if str(m.get("session_id") or "").startswith(session_ref) or str(m["participant_id"]).startswith(session_ref)]
        if len(matches) != 1: raise RoomError("session reference must match exactly one active session")
        if any(m["target"] == "@" + handle and m["participant_id"] != matches[0]["participant_id"] for m in active): raise RoomError(f"@{handle} is already active")
        self.connection.execute("UPDATE presence SET handle=?,seen_at=? WHERE room_id=? AND participant_id=?", (handle, utc_now(), repo.room_id, matches[0]["participant_id"]))
        self.connection.execute("INSERT OR REPLACE INTO handle_claims(room_id,participant_id,claimed_at) VALUES(?,?,?)", (repo.room_id, matches[0]["participant_id"], utc_now()))
        self.connection.commit()
        return next(m for m in self.members(repo.room_id) if m["participant_id"] == matches[0]["participant_id"])

    def targets(self, repo: Repository) -> Dict[str, Any]:
        self.register_room(repo)
        agents = [m for m in self.members(repo.room_id) if m["state"] != "offline"]
        worktrees = list_worktree_references(repo)
        for worktree in worktrees:
            worktree["active_agents"] = sum(Path(m["worktree"]).resolve() == Path(worktree["path"]).resolve() for m in agents)
        return {"room_id": repo.room_id, "agents": agents, "worktrees": worktrees, "human": {"target": "@human"}}

    def allowed_targets(self, repo: Repository) -> Set[str]:
        allowed = {"@human"}
        allowed.update(m["target"] for m in self.members(repo.room_id) if m["state"] != "offline")
        allowed.update(w["target"] for w in list_worktree_references(repo))
        return allowed

    def threads(self, repo: Repository, include_resolved: bool = False) -> List[Dict[str, Any]]:
        self.register_room(repo)
        where = "room_id=?" if include_resolved else "room_id=? AND status='open'"
        rows = self.connection.execute(f"SELECT * FROM threads WHERE {where} ORDER BY updated_at DESC", (repo.room_id,)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["participants"] = json.loads(value.pop("participants_json"))
            value["paths"] = json.loads(value.pop("paths_json"))
            value["metadata"] = json.loads(value.pop("metadata_json"))
            value["audience"] = value["metadata"].get("audience") or ("agents" if value["source"] == "preemptive-conflict" else "human-loop" if "@human" in value["participants"] else "agents")
            value["origin"] = value["metadata"].get("origin") or value["opener"]
            value["lifetime"] = "temporary" if value["source"] in ("preemptive-conflict", "notification-route", "temporary-channel") else "durable"
            result.append(value)
        return result

    def thread(self, repo: Repository, thread_id: str) -> Dict[str, Any]:
        value = next((item for item in self.threads(repo, True) if item["id"] == thread_id), None)
        if value is None:
            raise RoomError("coordination thread does not exist")
        return value

    def open_thread(self, repo: Repository, title: str, reason: str, opener: str, participants: Sequence[str], paths: Sequence[str], source: str = "manual", thread_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._require_writable()
        heading = concise(ensure_value_free(title), 120)
        why = concise(ensure_value_free(reason), 240)
        thread_metadata = dict(metadata or {})
        audience = str(thread_metadata.get("audience") or "agents")
        if audience not in ("agents", "human-loop"):
            raise RoomError("thread audience must be agents or human-loop")
        thread_metadata["audience"] = audience
        thread_metadata["origin"] = concise(str(thread_metadata.get("origin") or opener), 80)
        targets = list(dict.fromkeys(target_token(value) for value in participants if str(value).strip()))
        if audience == "agents":
            targets = [value for value in targets if value != "@human"]
        elif "@human" not in targets:
            targets.insert(0, "@human")
        unknown = [value for value in targets if value not in self.allowed_targets(repo)]
        if unknown:
            if source == "preemptive-conflict":
                targets = [value for value in targets if value not in unknown]
            else:
                raise RoomError("inactive or unknown thread participant(s): " + ", ".join(unknown))
        clean_paths = list(dict.fromkeys(concise(path, 300) for path in paths if str(path).strip()))
        now = utc_now()
        identifier = thread_id or "thread-" + hashlib.sha256(os.urandom(32)).hexdigest()[:12]
        self.connection.execute(
            """INSERT INTO threads(id,room_id,created_at,updated_at,status,title,reason,opener,participants_json,paths_json,source,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,status='open',title=excluded.title,reason=excluded.reason,participants_json=excluded.participants_json,paths_json=excluded.paths_json,metadata_json=excluded.metadata_json""",
            (identifier, repo.room_id, now, now, "open", heading, why, concise(opener, 100), json.dumps(targets), json.dumps(clean_paths), source, json.dumps(thread_metadata, sort_keys=True)),
        )
        self.connection.commit()
        return self.thread(repo, identifier)

    def close_thread(self, repo: Repository, thread_id: str) -> Dict[str, Any]:
        self._require_writable()
        thread = self.thread(repo, thread_id)
        status = "resolved" if thread["lifetime"] == "temporary" else "archived"
        self.connection.execute("UPDATE threads SET status=?,updated_at=? WHERE room_id=? AND id=?", (status, utc_now(), repo.room_id, thread_id))
        self.connection.commit()
        return self.thread(repo, thread_id)

    def post_thread(self, repo: Repository, thread_id: str, sender: str, message: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        thread = self.thread(repo, thread_id)
        if thread["status"] != "open":
            raise RoomError("coordination thread is resolved")
        posted = self.post(repo, sender, "message", f"thread:{thread_id}", "posted", message, thread["participants"], session_id, {"thread_id": thread_id})
        self.connection.execute("UPDATE threads SET updated_at=? WHERE room_id=? AND id=?", (utc_now(), repo.room_id, thread_id))
        self.connection.commit()
        return posted

    def sync_preemptive_conflicts(self, repo: Repository) -> List[Dict[str, Any]]:
        active_ids: Set[str] = set()
        members = self.members(repo.room_id)
        with CONFLICT_SCAN_LOCK:
            had_scan = repo.room_id in CONFLICT_SCANS
        conflicts = preemptive_conflicts(repo)
        if not had_scan:
            return self.threads(repo)
        grouped: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        for conflict in conflicts:
            stable_targets = tuple(sorted(str(item["target"]) for item in conflict["worktrees"]))
            group = grouped.setdefault(stable_targets, {"worktrees": conflict["worktrees"], "paths": []})
            group["paths"].append(str(conflict["path"]))
        for stable_targets, group in grouped.items():
            worktrees = group["worktrees"]
            paths = sorted(set(group["paths"]))
            worktree_paths = {str(item["path"]) for item in worktrees}
            participants = [str(item["target"]) for item in worktrees]
            participants.extend(str(member["target"]) for member in members if member["state"] != "offline" and str(member["worktree"]) in worktree_paths)
            digest = hashlib.sha256(f"{repo.room_id}\n{' '.join(stable_targets)}".encode()).hexdigest()[:12]
            thread_id = "conflict-" + digest
            active_ids.add(thread_id)
            prompted = self.connection.execute("SELECT 1 FROM messages WHERE room_id=? AND topic=? AND sender='@chat-room' LIMIT 1", (repo.room_id, f"thread:{thread_id}")).fetchone() is not None
            title = f"Merge conflict: {paths[0]}" if len(paths) == 1 else f"Merge conflict: {len(paths)} files"
            self.open_thread(repo, title, "branches do not merge cleanly", "@chat-room", participants, paths, "preemptive-conflict", thread_id, {"audience": "agents", "origin": "git merge-tree"})
            if not prompted:
                tags = " ".join(dict.fromkeys(participants))
                path_summary = ", ".join(paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
                self.post_thread(
                    repo,
                    thread_id,
                    "@chat-room",
                    f"{tags} these branches conflict on {path_summary}. Git reports the collision today; agree who rebases and in what order before the merge.",
                )
        open_auto = self.connection.execute("SELECT id FROM threads WHERE room_id=? AND status='open' AND source='preemptive-conflict'", (repo.room_id,)).fetchall()
        stale = [str(row["id"]) for row in open_auto if str(row["id"]) not in active_ids]
        if stale:
            self.connection.executemany("UPDATE threads SET status='resolved',updated_at=? WHERE room_id=? AND id=?", [(utc_now(), repo.room_id, value) for value in stale])
            self.connection.commit()
        return self.threads(repo)

    def resolve_targets(self, repo: Repository, recipients: Sequence[str], body: str) -> List[str]:
        requested = [target_token(value) for value in recipients if value.strip()] + mentioned_targets(body)
        requested = list(dict.fromkeys(requested))
        if not requested: return []
        allowed = self.allowed_targets(repo)
        unknown = [item for item in requested if item not in allowed]
        if unknown: raise RoomError("inactive or unknown target(s): " + ", ".join(unknown))
        return requested

    def post(self, repo: Repository, sender: str, kind: str, topic: str, status: str, message: str, recipients: Sequence[str], session_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._require_writable()
        if kind not in MESSAGE_KINDS: raise RoomError("unsupported message kind")
        self.register_room(repo)
        body = ensure_value_free(message)
        resolved = self.resolve_targets(repo, recipients, body)
        now = utc_now()
        cursor = self.connection.execute("""INSERT INTO messages(room_id,timestamp,session_id,sender,recipients_json,kind,topic,status,message,cwd,worktree,branch,head,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (repo.room_id, now, session_id, sender[:100], json.dumps(resolved), kind, topic[:120], status[:160], body, str(repo.cwd), str(repo.worktree), repo.branch, repo.head, json.dumps(metadata or {}, sort_keys=True)))
        self.connection.commit()
        posted = self.message(int(cursor.lastrowid))
        posted["wake"] = self.dispatch_wakes(repo, posted, self.data_dir)
        return posted

    def message(self, message_id: int) -> Dict[str, Any]:
        row = self.connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None: raise RoomError("message does not exist")
        return message_from_row(row)

    def context_cursor(self, room_id: str, participant_id: str) -> int:
        row = self.connection.execute("SELECT last_message_id FROM cursors WHERE room_id=? AND participant_id=?", (room_id, participant_id)).fetchone()
        return int(row["last_message_id"]) if row else 0

    def advance_cursor(self, room_id: str, participant_id: str, message_id: int) -> None:
        if self.behind:
            return  # Context still injects; it just replays until this copy is upgraded.
        self.connection.execute("INSERT INTO cursors(room_id,participant_id,last_message_id,updated_at) VALUES(?,?,?,?) ON CONFLICT(room_id,participant_id) DO UPDATE SET last_message_id=excluded.last_message_id,updated_at=excluded.updated_at", (room_id, participant_id, int(message_id), utc_now()))
        self.connection.commit()

    def read(self, room_id: str, after_id: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM messages WHERE room_id=? AND id>? ORDER BY id ASC LIMIT ?", (room_id, max(0, int(after_id)), max(1, min(100, int(limit))))).fetchall()
        return [message_from_row(row) for row in rows]

    def search(self, room_id: str, query: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Reach a message the snapshot window no longer carries, without dropping to grep."""
        text = query.strip()
        if not text:
            return []
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.connection.execute(
            "SELECT * FROM messages WHERE room_id=? AND (message LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\' OR topic LIKE ? ESCAPE '\\')"
            " ORDER BY id DESC LIMIT ?",
            (room_id, pattern, pattern, pattern, max(1, min(200, int(limit)))),
        ).fetchall()
        return [message_from_row(row) for row in reversed(rows)]

    def recent(self, room_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT ?", (room_id, max(1, min(2000, int(limit))))).fetchall()
        return [message_from_row(row) for row in reversed(rows)]

    def status(self, repo: Repository) -> Dict[str, Any]:
        self.register_room(repo); members = self.members(repo.room_id)
        last = self.connection.execute("SELECT COALESCE(MAX(id),0) value FROM messages WHERE room_id=?", (repo.room_id,)).fetchone()["value"]
        return {"room_id": repo.room_id, "project_identity": repo.project_identity, "common_dir": str(repo.common_dir), "worktree": str(repo.worktree), "branch": repo.branch, "head": repo.head, "members_total": len(members), "members_online": sum(m["state"] == "online" for m in members), "members_idle": sum(m["state"] == "idle" for m in members), "last_message_id": int(last), "authority": "advisory-only"}

    def dispatch_wakes(self, repo: Repository, message: Dict[str, Any], data_dir: Optional[Path] = None) -> Dict[str, Any]:
        """Carry a tagged message into the addressed session instead of waiting for it to look.

        A live Codex app-server connection takes the message directly; every other idle
        session gets one vendor CLI turn. That turn costs vendor tokens, so delivery is
        deliberately narrow: only a direct @handle by default, never system chatter, never
        the sender's own session, and never twice inside the cooldown.
        """
        policy = self.display_name("delivery_policy", "wake_on_tag", "direct")
        result: Dict[str, Any] = {"attempted": 0, "started": [], "failed": [], "policy": policy}
        recipients = set(message["recipients"])
        if policy == "off" or not recipients or str(message.get("sender") or "") == "@chat-room":
            return result
        for member in self.members(repo.room_id):
            if member["state"] == "offline":
                continue
            addressed = {member["target"]} if policy != "all" else {member["target"], member["worktree_target"]}
            if not recipients.intersection(addressed):
                continue
            session_id = str(member.get("session_id") or "")
            client = str(member.get("role") or "").split(":")[0]
            if not session_id or client not in ("codex", "claude"):
                continue
            if message.get("session_id") == session_id or running_delivery(client, session_id):
                continue
            with CHAT_DELIVERY_LOCK:
                previous = CHAT_DELIVERIES.get((client, session_id), {})
                recent = time.monotonic() - float(previous.get("started_at") or 0) < WAKE_COOLDOWN_SECONDS
            if recent:
                continue
            result["attempted"] += 1
            body = f"{WAKE_PROMPT}\n\nFrom {message.get('sender')} in Chat Room: {message.get('message')}"
            try:
                if member.get("wakeable_idle"):
                    wake_codex(str(member["wake_endpoint"]), session_id, body)
                elif data_dir is not None:
                    started = spawn_cli_turn(data_dir, client, Path(str(member["worktree"])), body, session_id)
                    track_delivery(client, session_id, started["process"])
                else:
                    continue
                result["started"].append(member["target"])
            except (RoomError, OSError) as error:
                result["failed"].append({"target": member["target"], "reason": str(error)[:240]})
        return result


def message_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {"id": int(row["id"]), "schema": "chat-room.message.v1", "room_id": row["room_id"], "timestamp": row["timestamp"], "session_id": row["session_id"], "sender": row["sender"], "recipients": json.loads(row["recipients_json"]), "kind": row["kind"], "topic": row["topic"], "status": row["status"], "message": row["message"], "cwd": row["cwd"], "worktree": row["worktree"], "branch": row["branch"], "head": row["head"], "metadata": json.loads(row["metadata_json"])}


def select_repository(store: RoomStore, cwd: Optional[str]) -> Repository:
    repo = resolve_repository(cwd or os.getcwd())
    if repo: return repo
    latest = store.latest_repository()
    if latest: return latest
    raise RoomError("no Git project room found; run from a Git worktree or pass --cwd")


def participant_identity(payload: Dict[str, Any], repo: Repository) -> Tuple[str, str, Optional[str], Optional[str]]:
    session_id = str(payload.get("session_id") or "") or None
    agent_id = str(payload.get("agent_id") or "") or None
    participant = "session:" + session_id if session_id else "agent:" + agent_id if agent_id else f"process:{os.getpid()}:{repo.worktree}"
    return participant, f"{client_name()}:{repo.worktree.name}", session_id, agent_id


def compact_context(store: RoomStore, repo: Repository, participant_id: str, event: str) -> str:
    member = next((m for m in store.members(repo.room_id) if m["participant_id"] == participant_id), None)
    if member is None: return ""
    targets = {member["target"], member["worktree_target"]}
    # Only what this participant has not been shown yet; the cursor advances past
    # everything examined so an unaddressed message is not re-scanned forever.
    incoming = store.read(repo.room_id, store.context_cursor(repo.room_id, participant_id), 100)
    messages = [m for m in incoming if not m["recipients"] or targets.intersection(m["recipients"])][-10:]
    if incoming:
        store.advance_cursor(repo.room_id, participant_id, incoming[-1]["id"])
    lines = [f"Chat Room (advisory): room={repo.room_id} handle={member['target']} worktree={member['worktree_target']} branch={repo.branch or 'detached'}.", "Use room tools for material coordination. Re-observe repository state before acting."]
    if messages:
        lines.append("New room messages:")
        lines.extend(f"[{m['id']}] {m['kind']} {m['sender']} -> {','.join(m['recipients']) or 'room'} [{m['topic']}]: {m['message']}" for m in messages)
    elif event != "SessionStart":
        return ""
    return "\n".join(lines)


def hook_output(event: str, context: str = "") -> Dict[str, Any]:
    value: Dict[str, Any] = {"continue": True}
    if context and event in CONTEXT_EVENTS: value["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": context}
    return value


def run_hook(data_dir: Path) -> int:
    try: payload = json.load(sys.stdin)
    except json.JSONDecodeError: print(json.dumps({"continue": True})); return 0
    event = str(payload.get("hook_event_name") or "")
    repo = resolve_repository(str(payload.get("cwd") or os.getcwd()))
    if repo is None: print(json.dumps(hook_output(event))); return 0
    participant, role, session_id, agent_id = participant_identity(payload, repo)
    state = "offline" if event in ("SessionEnd", "SubagentStop") else "online"
    try:
        with RoomStore(data_dir) as store:
            store.upsert_presence(repo, participant, session_id, agent_id, role, state, event, os.environ.get(WAKE_ENDPOINT_ENV) if client_name() == "codex" else None)
            context = compact_context(store, repo, participant, event) if event in CONTEXT_EVENTS else ""
        print(json.dumps(hook_output(event, context), separators=(",", ":")))
    except Exception as error:
        print(json.dumps({"continue": True, "systemMessage": f"Chat Room unavailable: {error}"}))
    return 0


def tool_definitions() -> List[Dict[str, Any]]:
    cwd = {"type": "string", "description": "Optional Git worktree path."}
    return [
        {"name": "room_status", "description": "Show room and repository identity.", "inputSchema": {"type": "object", "properties": {"cwd": cwd}}},
        {"name": "room_read", "description": "Read chronological room messages.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "after_id": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}}},
        {"name": "room_members", "description": "List observed agent sessions and presence.", "inputSchema": {"type": "object", "properties": {"cwd": cwd}}},
        {"name": "room_targets", "description": "List active @agent and #worktree targets.", "inputSchema": {"type": "object", "properties": {"cwd": cwd}}},
        {"name": "room_options", "description": "List the indexed notification actions and policy options used by Chat Room.", "inputSchema": {"type": "object", "properties": {"cwd": cwd}}},
        {"name": "room_option_set", "description": "Add or update one machine-local indexed option without rebuilding the UI.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "namespace": {"type": "string"}, "key": {"type": "string"}, "value": {"type": "string"}, "metadata": {"type": "object"}}, "required": ["namespace", "key", "value"]}},
        {"name": "room_threads", "description": "List open manual and preemptive-conflict coordination threads.", "inputSchema": {"type": "object", "properties": {"cwd": cwd}}},
        {"name": "room_thread_open", "description": "Open agent-only chatter or a human-in-the-loop question with a durable reason and origin.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "title": {"type": "string"}, "reason": {"type": "string"}, "audience": {"type": "string", "enum": ["agents", "human-loop"]}, "origin": {"type": "string"}, "lifetime": {"type": "string", "enum": ["durable", "temporary"]}, "session_id": {"type": "string", "description": "Your session id, so an answer can be carried back to you."}, "participants": {"type": "array", "items": {"type": "string"}}, "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "reason"]}},
        {"name": "room_thread_close", "description": "Mark a coordination thread resolved without changing Git state.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "thread_id": {"type": "string"}}, "required": ["thread_id"]}},
        {"name": "room_identify", "description": "Assign an active session a semantic @handle.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "session_id": {"type": "string"}, "handle": {"type": "string"}}, "required": ["session_id", "handle"]}},
        {"name": "room_post", "description": "Post one value-free coordination message. Use thread_id for central routing, or tag active @handles and #worktrees ad hoc.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "sender": {"type": "string"}, "session_id": {"type": "string"}, "thread_id": {"type": "string"}, "recipients": {"type": "array", "items": {"type": "string"}}, "kind": {"type": "string", "enum": list(MESSAGE_KINDS)}, "topic": {"type": "string"}, "status": {"type": "string"}, "message": {"type": "string", "maxLength": 4000}}, "required": ["kind", "topic", "message"]}},
        {"name": "room_session_start", "description": "Open new agent work in a worktree of this project. Starts one local vendor CLI session; that session bills vendor tokens.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "client": {"type": "string", "enum": ["claude", "codex"]}, "worktree": {"type": "string", "description": "Absolute worktree path; defaults to the current one."}, "prompt": {"type": "string", "maxLength": 4000}}, "required": ["client", "prompt"]}},
        {"name": "room_session_stop", "description": "Interrupt a local turn this room started for a session.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "client": {"type": "string", "enum": ["claude", "codex"]}, "session_id": {"type": "string"}}, "required": ["client", "session_id"]}},
        {"name": "room_search", "description": "Search this room's message history beyond the recent window.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["query"]}},
        {"name": "room_handoff", "description": "Post a structured handoff with source, paths, proof, blocker, and next owner.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "sender": {"type": "string"}, "session_id": {"type": "string"}, "topic": {"type": "string"}, "source_sha": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}, "proof": {"type": "string"}, "blocker": {"type": "string"}, "next_owner": {"type": "string"}}, "required": ["topic", "source_sha", "paths", "proof", "next_owner"]}},
    ]


def execute_tool(store: RoomStore, name: str, args: Dict[str, Any]) -> Any:
    repo = select_repository(store, args.get("cwd"))
    if name == "room_status": return store.status(repo)
    if name == "room_read": return {"room_id": repo.room_id, "messages": store.read(repo.room_id, int(args.get("after_id", 0)), int(args.get("limit", 50)))}
    if name == "room_members": return {"room_id": repo.room_id, "members": store.members(repo.room_id)}
    if name == "room_targets": return store.targets(repo)
    if name == "room_options": return {"room_id": repo.room_id, "options": store.options()}
    if name == "room_option_set": return store.set_option(str(args["namespace"]), str(args["key"]), str(args["value"]), args.get("metadata") if isinstance(args.get("metadata"), dict) else {})
    if name == "room_threads": return {"room_id": repo.room_id, "threads": store.sync_preemptive_conflicts(repo)}
    if name == "room_thread_open":
        source = "temporary-channel" if str(args.get("lifetime") or "durable") == "temporary" else "team-channel"
        opener = str(args.get("opener") or f"{client_name()}-session")
        metadata = {"audience": str(args.get("audience") or "agents"), "origin": str(args.get("origin") or opener)}
        if args.get("session_id"):
            metadata["origin_session"] = str(args["session_id"])
            metadata["origin_client"] = client_name()
        return store.open_thread(repo, str(args["title"]), str(args["reason"]), opener, [str(x) for x in args.get("participants", [])], [str(x) for x in args.get("paths", [])], source, metadata=metadata)
    if name == "room_thread_close": return store.close_thread(repo, str(args["thread_id"]))
    if name == "room_identify": return store.claim_handle(repo, str(args["session_id"]), str(args["handle"]))
    if name == "room_post":
        sender = str(args.get("sender") or f"{client_name()}-session"); session_id = str(args.get("session_id") or "") or None
        if args.get("thread_id"): return store.post_thread(repo, str(args["thread_id"]), sender, str(args["message"]), session_id)
        return store.post(repo, sender, str(args["kind"]), str(args["topic"]), str(args.get("status") or "posted"), str(args["message"]), [str(x) for x in args.get("recipients", [])], session_id)
    if name == "room_session_start":
        return start_session(store.data_dir, repo, str(args["client"]), str(args.get("worktree") or ""), str(args["prompt"]))
    if name == "room_session_stop":
        return stop_delivery(str(args["client"]), str(args["session_id"]))
    if name == "room_search":
        return {"room_id": repo.room_id, "messages": store.search(repo.room_id, str(args["query"]), int(args.get("limit", 100)))}
    if name == "room_handoff":
        paths = [str(x) for x in args.get("paths", [])]
        body = f"source={args['source_sha']}; paths={','.join(paths)}; proof={args['proof']}; blocker={args.get('blocker') or 'none'}; next_owner={args['next_owner']}"
        return store.post(repo, str(args.get("sender") or f"{client_name()}-session"), "handoff", str(args["topic"]), "handoff-ready", body, [str(args["next_owner"])], str(args.get("session_id") or "") or None, {"source_sha": args["source_sha"], "paths": paths})
    raise RoomError(f"unknown room tool: {name}")


def run_mcp(data_dir: Path) -> int:
    with RoomStore(data_dir) as store:
        for raw in sys.stdin:
            if not raw.strip(): continue
            request_id: Any = None
            try:
                request = json.loads(raw); request_id = request.get("id"); method = request.get("method"); params = request.get("params") or {}
                if method == "initialize": result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": PLUGIN_NAME, "version": VERSION}}
                elif method == "ping": result = {}
                elif method == "tools/list": result = {"tools": tool_definitions()}
                elif method == "tools/call":
                    value = execute_tool(store, str(params.get("name") or ""), params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": json.dumps(value, indent=2, sort_keys=True)}], "structuredContent": value if isinstance(value, dict) else {"value": value}, "isError": False}
                elif method in ("notifications/initialized", "notifications/cancelled"): continue
                else: raise RoomError(f"method not found: {method}")
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except Exception as error:
                response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(error)}}
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n"); sys.stdout.flush()
    return 0


class RoomHandler(BaseHTTPRequestHandler):
    server_version = "ChatRoom/0.6"
    def log_message(self, *_args: Any) -> None: return
    @property
    def app(self) -> "RoomHTTPServer": return self.server  # type: ignore[return-value]
    def authorized(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        return self.headers.get("X-Chat-Room-Token") == self.app.token or f"chat_room_token={self.app.token}" in cookie.split("; ")
    def valid_host(self) -> bool:
        value = self.headers.get("Host", "").lower()
        host = value[1:value.find("]")] if value.startswith("[") and "]" in value else value.rsplit(":", 1)[0]
        return host in {"127.0.0.1", "localhost", "::1", self.app.hostname}
    def send_json(self, value: Any, status: int = 200) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode()
        try:
            self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def do_GET(self) -> None:
        if not self.valid_host(): self.send_error(HTTPStatus.MISDIRECTED_REQUEST); return
        parsed = urlparse(self.path); path = parsed.path
        if path == "/api/snapshot":
            if not self.authorized(): self.send_json({"error": "invalid local token"}, 403); return
            with RoomStore(self.app.data_dir) as store:
                threads = store.sync_preemptive_conflicts(self.app.repo)
                targets = store.targets(self.app.repo)
                options = store.options()
                status = store.status(self.app.repo)
                status["display_name"] = store.display_name("room_name", self.app.repo.room_id, self.app.repo.worktree.name or "Chat Room")
                self.send_json({"status": status, "messages": store.recent(self.app.repo.room_id, SNAPSHOT_MESSAGE_LIMIT), "targets": targets, "threads": threads, "alerts": coordination_alerts(targets, threads, options), "options": options, "events_url": f"ws://{self.app.hostname}:{self.app.events_port}/events"})
            return
        if path == "/api/search":
            if not self.authorized(): self.send_json({"error": "invalid local token"}, 403); return
            query = parse_qs(parsed.query)
            with RoomStore(self.app.data_dir) as store:
                self.send_json({"messages": store.search(self.app.repo.room_id, str(query.get("q", [""])[0]))})
            return
        if path == "/api/chats":
            if not self.authorized(): self.send_json({"error": "invalid local token"}, 403); return
            chats, _files = discover_chat_catalog(self.app.repo)
            with RoomStore(self.app.data_dir) as store:
                for chat in chats:
                    chat["title"] = store.display_name("chat_name", f"{chat['client']}-{chat['id']}", str(chat["title"]))
            self.send_json({"chats": chats})
            return
        if path == "/api/chat":
            if not self.authorized(): self.send_json({"error": "invalid local token"}, 403); return
            query = parse_qs(parsed.query)
            try:
                value = chat_transcript(self.app.repo, str(query.get("client", [""])[0]), str(query.get("id", [""])[0]))
                with RoomStore(self.app.data_dir) as store:
                    value["chat"]["title"] = store.display_name("chat_name", f"{value['chat']['client']}-{value['chat']['id']}", str(value["chat"]["title"]))
                    value["delivery"] = chat_delivery_state(value["chat"], store.members(self.app.repo.room_id))
                self.send_json(value)
            except RoomError as error:
                self.send_json({"error": str(error)}, 404)
            return
        static = {"/": ("index.html", "text/html; charset=utf-8"), "/room.css": ("room.css", "text/css"), "/room.js": ("room.js", "text/javascript"), "/icons.svg": ("icons.svg", "image/svg+xml")}
        if path in static:
            file_name, mime = static[path]; payload = (self.app.static_dir / file_name).read_bytes()
            self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", "no-store")
            if path == "/": self.send_header("Set-Cookie", f"chat_room_token={self.app.token}; Path=/; HttpOnly; SameSite=Strict")
            self.end_headers(); self.wfile.write(payload); return
        self.send_error(404)
    def do_POST(self) -> None:
        if not self.valid_host(): self.send_error(HTTPStatus.MISDIRECTED_REQUEST); return
        if not self.authorized(): self.send_json({"error": "invalid local token"}, 403); return
        path = urlparse(self.path).path
        if path not in ("/api/messages", "/api/threads", "/api/thread-close", "/api/chat-send", "/api/route-alert", "/api/rename", "/api/session-start", "/api/session-stop"): self.send_error(404); return
        try:
            declared = int(self.headers.get("Content-Length", "0"))
            maximum = MAX_CHAT_IMAGE_BYTES * MAX_CHAT_IMAGES * 2 if path == "/api/chat-send" else 16384
            if declared > maximum:
                self.send_json({"error": "request body exceeds the local Chat Room limit"}, 413); return
            body = json.loads(self.rfile.read(declared) or b"{}")
            with RoomStore(self.app.data_dir) as store:
                if path == "/api/session-start":
                    value = start_session(self.app.data_dir, self.app.repo, str(body.get("client") or ""), str(body.get("worktree") or ""), str(body.get("prompt") or ""))
                elif path == "/api/session-stop":
                    value = stop_delivery(str(body.get("client") or ""), str(body.get("session_id") or ""))
                elif path == "/api/chat-send":
                    attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else []
                    value = start_chat_delivery(self.app.data_dir, self.app.repo, str(body.get("client") or ""), str(body.get("session_id") or ""), str(body.get("message") or ""), store.members(self.app.repo.room_id), [item for item in attachments if isinstance(item, dict)])
                elif path == "/api/rename":
                    value = store.rename(self.app.repo, str(body.get("kind") or ""), str(body.get("reference") or ""), str(body.get("label") or ""), str(body.get("client") or ""))
                elif path == "/api/route-alert":
                    value = store.route_notification(self.app.repo, str(body.get("title") or "Notification settlement"), str(body.get("alert_type") or "notification"), str(body.get("actor") or "@human"), str(body.get("action") or "investigate"), [str(x) for x in body.get("participants", [])], [str(x) for x in body.get("paths", [])])
                elif path == "/api/threads":
                    source = "temporary-channel" if str(body.get("lifetime") or "durable") == "temporary" else "team-channel"
                    value = store.open_thread(self.app.repo, str(body.get("title") or ""), str(body.get("reason") or "coordination"), "@human", [str(x) for x in body.get("participants", [])], [str(x) for x in body.get("paths", [])], source, metadata={"audience": str(body.get("audience") or "agents"), "origin": str(body.get("origin") or "human")})
                elif path == "/api/thread-close":
                    value = store.close_thread(self.app.repo, str(body.get("thread_id") or ""))
                elif body.get("thread_id"):
                    value = store.post_thread(self.app.repo, str(body["thread_id"]), "@human", str(body.get("message") or ""))
                else:
                    value = store.post(self.app.repo, "@human", str(body.get("kind") or "message"), str(body.get("topic") or "general"), "posted", str(body.get("message") or ""), [str(x) for x in body.get("recipients", [])])
            self.send_json(value, 202 if path in ("/api/chat-send", "/api/session-start") else 201)
            self.app.event_hub.publish("workspace.changed", {"path": path})
        except (RoomError, ValueError, json.JSONDecodeError) as error: self.send_json({"error": str(error)}, 400)


class EventHub:
    def __init__(self) -> None:
        self.lock = threading.Lock(); self.subscribers: Set[queue.Queue[Dict[str, Any]]] = set(); self.sequence = 0

    def subscribe(self) -> queue.Queue[Dict[str, Any]]:
        receiver: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=32)
        with self.lock: self.subscribers.add(receiver)
        return receiver

    def unsubscribe(self, receiver: queue.Queue[Dict[str, Any]]) -> None:
        with self.lock: self.subscribers.discard(receiver)

    def publish(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        with self.lock:
            self.sequence += 1; event = {"type": event_type, "sequence": self.sequence, "timestamp": utc_now(), "data": data or {}}
            subscribers = list(self.subscribers)
        for receiver in subscribers:
            try: receiver.put_nowait(event)
            except queue.Full:
                try: receiver.get_nowait(); receiver.put_nowait(event)
                except (queue.Empty, queue.Full): pass


class RoomEventServer:
    def __init__(self, host: str, port: int, hostname: str, http_port: int, token: str, hub: EventHub) -> None:
        self.host = host; self.port = port; self.hostname = hostname; self.http_port = http_port; self.token = token; self.hub = hub
        self.ready = threading.Event(); self.thread: Optional[threading.Thread] = None; self.server: Any = None; self.error: Optional[Exception] = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="chat-room-events"); self.thread.start()
        if not self.ready.wait(8): raise RoomError("timed out starting local WebSocket events")
        if isinstance(self.error, ModuleNotFoundError):
            raise RoomError(f"cannot start local WebSocket events: {self.error}. The browser room needs the pinned transport, so start it through the installed `chat-room` command rather than a bare interpreter.")
        if self.error: raise RoomError(f"cannot start local WebSocket events: {self.error}")

    def _run(self) -> None:
        try:
            from websockets.exceptions import ConnectionClosed
            from websockets.sync.server import serve
            origin = f"http://{self.hostname}:{self.http_port}"
            def handler(connection: Any) -> None:
                cookie = str(connection.request.headers.get("Cookie", "")) if connection.request else ""
                if f"chat_room_token={self.token}" not in cookie.split("; "):
                    connection.close(1008, "invalid local token"); return
                receiver = self.hub.subscribe()
                try:
                    connection.send(json.dumps({"type": "connected", "sequence": 0, "timestamp": utc_now(), "data": {}}))
                    while True:
                        try: event = receiver.get(timeout=1.5)
                        except queue.Empty: event = {"type": "sync", "sequence": 0, "timestamp": utc_now(), "data": {}}
                        connection.send(json.dumps(event, separators=(",", ":")))
                except (ConnectionClosed, OSError):
                    pass
                finally:
                    self.hub.unsubscribe(receiver)
            self.server = serve(handler, self.host, self.port, origins=[origin, None], compression="deflate", max_size=65536)
            self.port = int(self.server.socket.getsockname()[1]); self.ready.set(); self.server.serve_forever()
        except Exception as error:
            self.error = error; self.ready.set()

    def stop(self) -> None:
        if self.server is not None: self.server.shutdown()
        if self.thread is not None: self.thread.join(timeout=5)


class RoomHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: Tuple[str, int], repo: Repository, data_dir: Path, static_dir: Path, token: str, hostname: str, events_port: int, event_hub: EventHub):
        super().__init__(address, RoomHandler); self.repo = repo; self.data_dir = data_dir; self.static_dir = static_dir; self.token = token; self.hostname = hostname; self.events_port = events_port; self.event_hub = event_hub


def running_room_url(port: int, hostname: str) -> Optional[str]:
    url = f"http://{hostname}:{port}/"
    request = Request(f"http://127.0.0.1:{port}/", headers={"Host": f"{hostname}:{port}"})
    try:
        with urlopen(request, timeout=.6) as response:
            payload = response.read(4096)
        return url if b"<title>Chat Room</title>" in payload else None
    except (OSError, ValueError):
        return None


def run_ui(data_dir: Path, cwd: Optional[str], host: str, port: int, hostname: str, events_port: Optional[int], no_open: bool) -> int:
    with RoomStore(data_dir) as store: repo = select_repository(store, cwd); store.register_room(repo)
    if host not in ("127.0.0.1", "localhost", "::1"): raise RoomError("the UI binds only to loopback")
    hostname = hostname.strip().lower().rstrip(".")
    if hostname != "localhost" and not hostname.endswith(".localhost"): raise RoomError("the browser hostname must be localhost or end in .localhost")
    token = hashlib.sha256(os.urandom(32)).hexdigest(); static_dir = Path(__file__).resolve().parents[1] / "assets"
    hub = EventHub()
    try:
        provisional = RoomHTTPServer((host, port), repo, data_dir, static_dir, token, hostname, 0, hub)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            existing = running_room_url(port, hostname)
            if existing:
                print(f"Chat Room is already running\n{existing}")
                if not no_open: webbrowser.open(existing)
                return 0
            raise RoomError(f"local port {port} is already used by another process; try: chat-room ui --port {port + 2} --events-port {port + 3}") from None
        raise RoomError(f"could not start the local Chat Room UI: {error}") from None
    http_port = int(provisional.server_address[1]); selected_events_port = int(events_port or (http_port + 1))
    event_server = RoomEventServer(host, selected_events_port, hostname, http_port, token, hub)
    try: event_server.start()
    except Exception: provisional.server_close(); raise
    provisional.events_port = event_server.port; server = provisional; url = f"http://{hostname}:{http_port}/"
    print(f"Chat Room {repo.room_id}\n{url}\nEvents: ws://{hostname}:{event_server.port}/events\nPress Ctrl-C to stop.")
    if not no_open: threading.Timer(.3, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); event_server.stop()
    return 0


def service_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.accountable.chat-room.plist"


def service_path() -> str:
    preferred = [str(Path(sys.executable).resolve().parent), str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    preferred.extend(part for part in os.environ.get("PATH", "").split(os.pathsep) if part)
    return os.pathsep.join(dict.fromkeys(preferred))


def install_service(data_dir: Path, cwd: Optional[str], hostname: str, port: int, events_port: Optional[int]) -> Dict[str, Any]:
    if sys.platform != "darwin":
        raise RoomError("the durable user service currently supports macOS launchd")
    repo = resolve_repository(cwd or os.getcwd())
    if repo is None:
        raise RoomError("service installation must reference a Git worktree")
    hostname = hostname.strip().lower().rstrip(".")
    if hostname != "localhost" and not hostname.endswith(".localhost"):
        raise RoomError("the browser hostname must be localhost or end in .localhost")
    data_dir.mkdir(parents=True, exist_ok=True)
    plist_path = service_plist_path(); plist_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": "com.accountable.chat-room",
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "--data-dir", str(data_dir), "ui", "--cwd", str(repo.worktree), "--host", "127.0.0.1", "--port", str(port), "--events-port", str(events_port or (port + 1)), "--hostname", hostname, "--no-open"],
        "EnvironmentVariables": {"PATH": service_path()},
        "RunAtLoad": True, "KeepAlive": True,
        "StandardOutPath": str(data_dir / "service.log"), "StandardErrorPath": str(data_dir / "service.log"),
        "ProcessType": "Interactive",
    }
    temporary = plist_path.with_suffix(".plist.tmp")
    with temporary.open("wb") as handle: plistlib.dump(payload, handle, sort_keys=True)
    os.chmod(temporary, 0o600); temporary.replace(plist_path)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise RoomError((result.stderr or result.stdout).strip() or "launchd refused the Chat Room service")
    return {"status": "installed", "url": f"http://{hostname}:{port}/", "events_url": f"ws://{hostname}:{events_port or (port + 1)}/events", "project": repo.project_identity, "plist": str(plist_path)}


def service_status() -> Dict[str, Any]:
    plist_path = service_plist_path(); domain = f"gui/{os.getuid()}/com.accountable.chat-room"
    result = subprocess.run(["launchctl", "print", domain], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False) if sys.platform == "darwin" else None
    return {"installed": plist_path.exists(), "running": bool(result and result.returncode == 0), "plist": str(plist_path)}


def uninstall_service() -> Dict[str, Any]:
    plist_path = service_plist_path()
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if plist_path.exists(): plist_path.unlink()
    return {"status": "uninstalled", "plist": str(plist_path)}


def run_codex(data_dir: Path, arguments: Sequence[str]) -> int:
    executable = find_cli_executable("codex")
    if not executable: raise RoomError("codex executable is not available")
    sockets = data_dir / "codex-sessions"; sockets.mkdir(parents=True, exist_ok=True)
    endpoint = f"unix://{sockets / (str(os.getpid()) + '-' + os.urandom(5).hex() + '.sock')}"
    path = endpoint_socket_path(endpoint)
    log = data_dir / "codex-app-server.log"
    with open(log, "ab", buffering=0) as handle:
        server = subprocess.Popen([executable, "app-server", "--listen", endpoint], stdin=subprocess.DEVNULL, stdout=handle, stderr=handle, start_new_session=True)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not socket_ready(endpoint):
        if server.poll() is not None: raise RoomError(f"Codex app server exited; inspect {log}")
        time.sleep(.1)
    env = os.environ.copy(); env[WAKE_ENDPOINT_ENV] = endpoint; env[CLIENT_ENV] = "codex"
    values = list(arguments); values = values[1:] if values[:1] == ["--"] else values
    try: return subprocess.call([executable, "--remote", endpoint, *values], env=env)
    finally:
        server.terminate()
        try: server.wait(5)
        except subprocess.TimeoutExpired: server.kill()
        if path.exists() and stat.S_ISSOCK(path.stat().st_mode): path.unlink()


def reference_columns() -> Dict[str, List[str]]:
    """The shape this build expects, built from the very script that creates it."""
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(SCHEMA_SQL)
        tables = sorted(row[0] for row in reference.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        return {table: [row[1] for row in reference.execute(f"PRAGMA table_info({table})")] for table in tables}
    finally:
        reference.close()


def launcher_report() -> Dict[str, Any]:
    launcher = find_cli_executable(PLUGIN_NAME)
    if not launcher:
        return {"path": None, "version": None}
    try:
        result = subprocess.run([launcher, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"path": launcher, "version": None}
    # Anything that predates --version answers with an argparse usage error, not a version.
    reported = result.stdout.strip() if result.returncode == 0 else ""
    return {"path": launcher, "version": reported or None, "predates_version_flag": result.returncode != 0}


def diagnose(data_dir: Path, repair: bool = False) -> Dict[str, Any]:
    """Answer 'why is the room broken' without needing to read the source."""
    database = data_dir.expanduser().resolve() / "chat-room.sqlite3"
    report: Dict[str, Any] = {
        "version": VERSION, "schema_version": SCHEMA_VERSION,
        "python": sys.version.split()[0], "platform": sys.platform,
        "data_dir": str(data_dir.expanduser().resolve()), "database": str(database),
        "launcher": launcher_report(), "findings": [],
    }
    findings: List[Dict[str, str]] = report["findings"]
    reported = str(report["launcher"]["version"] or "")
    if report["launcher"].get("predates_version_flag"):
        findings.append({"severity": "warning", "detail": f"the installed launcher at {report['launcher']['path']} predates --version, so it is older than {VERSION}; hooks and the browser room are running different code. Reinstall it from this checkout."})
    elif reported and VERSION not in reported:
        findings.append({"severity": "warning", "detail": f"the installed launcher reports {reported!r} while this copy is {VERSION}; hooks and the browser room may be running different code"})
    if not database.exists():
        report["installed_schema"] = None
        findings.append({"severity": "info", "detail": "no room database yet; it is created on first use"})
        return report
    if repair:
        # The migration is the repair: it narrows any shape a newer pre-release widened.
        with RoomStore(data_dir):
            pass
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        installed = int(str(row[0])) if row and str(row[0]).isdigit() else 0
        report["installed_schema"] = installed
        if installed > SCHEMA_VERSION:
            findings.append({"severity": "critical", "detail": f"the database is schema {installed} but this copy speaks {SCHEMA_VERSION}; this copy is behind and refuses to write. Upgrade it."})
        drift: Dict[str, Dict[str, List[str]]] = {}
        for table, expected in reference_columns().items():
            actual = [item[1] for item in connection.execute(f"PRAGMA table_info({table})")]
            if not actual:
                continue
            unexpected = [name for name in actual if name not in expected]
            absent = [name for name in expected if name not in actual]
            if unexpected or absent:
                drift[table] = {"unexpected": unexpected, "absent": absent}
        report["column_drift"] = drift
        for table, delta in drift.items():
            findings.append({"severity": "critical", "detail": f"{table} carries unexpected {delta['unexpected']} and is missing {delta['absent']}; any version writing it positionally will fail. Run `chat-room doctor --repair`."})
    finally:
        connection.close()
    if not findings:
        findings.append({"severity": "ok", "detail": "the room database matches this build"})
    return report


def print_message(item: Dict[str, Any]) -> None:
    targets = " -> " + ",".join(item["recipients"]) if item["recipients"] else ""
    print(f"[{item['id']}] {item['timestamp']} {item['kind'].upper()} {item['sender']}{targets} {item['topic']} [{item['status']}]\n  {item['message']}")


def run_chat(store: RoomStore, repo: Repository, sender: str) -> int:
    participant = f"cli:{os.getpid()}:{repo.worktree}"
    role = f"cli:{repo.worktree.name}"
    store.upsert_presence(repo, participant, None, None, role, "online", "ChatStart")
    try:
        print(f"Chat Room {repo.room_id} — {repo.project_identity}\nTag active @handles and #worktrees. /help for commands.")
        recent = store.recent(repo.room_id, 30)
        for item in recent: print_message(item)
        after = recent[-1]["id"] if recent else 0
        while True:
            try:
                if sys.stdin.isatty():
                    print(f"{sender}> ", end="", flush=True)
                    while not select.select([sys.stdin], [], [], .5)[0]:
                        incoming = store.read(repo.room_id, after, 100)
                        if incoming:
                            print(); [print_message(x) for x in incoming]; after = incoming[-1]["id"]; print(f"{sender}> ", end="", flush=True)
                    line = sys.stdin.readline()
                else: line = sys.stdin.readline()
                if not line: return 0
                line = line.strip()
            except KeyboardInterrupt: print(); return 0
            store.upsert_presence(repo, participant, None, None, role, "online", "ChatInput")
            if line in ("/quit", "/exit"): return 0
            if line == "/help": print("/targets /members /recent /quit — or type a message"); continue
            if line == "/targets": print(json.dumps(store.targets(repo), indent=2)); continue
            if line == "/members": print(json.dumps(store.members(repo.room_id), indent=2)); continue
            if line == "/recent": [print_message(x) for x in store.recent(repo.room_id, 30)]; continue
            if line: print_message(store.post(repo, sender, "message", "general", "posted", line, []))
    finally:
        store.upsert_presence(repo, participant, None, None, role, "offline", "ChatEnd")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="chat-room", description="Local chat for humans, coding agents, and Git worktrees")
    value.add_argument("--data-dir", type=Path, default=default_data_dir())
    value.add_argument("--version", action="version", version=f"{PLUGIN_NAME} {VERSION} (schema {SCHEMA_VERSION})")
    sub = value.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor"); doctor.add_argument("--cwd"); doctor.add_argument("--repair", action="store_true")
    for name in ("status", "targets", "members", "threads", "options", "read", "chat", "ui"):
        command = sub.add_parser(name); command.add_argument("--cwd")
        if name == "read": command.add_argument("--after-id", type=int, default=0); command.add_argument("--limit", type=int, default=50)
        if name == "chat": command.add_argument("--sender", default="@human")
        if name == "ui": command.add_argument("--host", default="127.0.0.1"); command.add_argument("--port", type=int, default=7391); command.add_argument("--events-port", type=int); command.add_argument("--hostname", default="chatroom.localhost"); command.add_argument("--no-open", action="store_true")
    post = sub.add_parser("post"); post.add_argument("--cwd"); post.add_argument("--sender", default="@human"); post.add_argument("--kind", choices=MESSAGE_KINDS, default="message"); post.add_argument("--topic", default="general"); post.add_argument("--status", default="posted"); post.add_argument("--recipient", action="append", default=[]); post.add_argument("--message", required=True)
    start = sub.add_parser("start"); start.add_argument("--cwd"); start.add_argument("--client", choices=("claude", "codex"), required=True); start.add_argument("--worktree"); start.add_argument("--prompt", required=True)
    stop = sub.add_parser("stop"); stop.add_argument("--cwd"); stop.add_argument("--client", choices=("claude", "codex"), required=True); stop.add_argument("--session", required=True)
    search = sub.add_parser("search"); search.add_argument("--cwd"); search.add_argument("--query", required=True); search.add_argument("--limit", type=int, default=100)
    identify = sub.add_parser("identify"); identify.add_argument("--cwd"); identify.add_argument("--session", required=True); identify.add_argument("--handle", required=True)
    thread_open = sub.add_parser("thread-open"); thread_open.add_argument("--cwd"); thread_open.add_argument("--title", required=True); thread_open.add_argument("--reason", default="coordination"); thread_open.add_argument("--audience", choices=("agents", "human-loop"), default="agents"); thread_open.add_argument("--origin", default="human"); thread_open.add_argument("--lifetime", choices=("durable", "temporary"), default="durable"); thread_open.add_argument("--participant", action="append", default=[]); thread_open.add_argument("--path", action="append", default=[])
    thread_close = sub.add_parser("thread-close"); thread_close.add_argument("--cwd"); thread_close.add_argument("--thread", required=True)
    option_set = sub.add_parser("option-set"); option_set.add_argument("--cwd"); option_set.add_argument("--namespace", required=True); option_set.add_argument("--key", required=True); option_set.add_argument("--value", required=True); option_set.add_argument("--metadata", default="{}")
    service = sub.add_parser("service"); service_actions = service.add_subparsers(dest="service_action", required=True)
    service_install = service_actions.add_parser("install"); service_install.add_argument("--cwd"); service_install.add_argument("--hostname", default="chatroom.localhost"); service_install.add_argument("--port", type=int, default=7391); service_install.add_argument("--events-port", type=int)
    service_actions.add_parser("status"); service_actions.add_parser("uninstall")
    sub.add_parser("hook"); sub.add_parser("mcp")
    codex = sub.add_parser("codex"); codex.add_argument("args", nargs=argparse.REMAINDER)
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            report = diagnose(args.data_dir, args.repair)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 1 if any(item["severity"] == "critical" for item in report["findings"]) else 0
        if args.command == "hook": return run_hook(args.data_dir)
        if args.command == "mcp": return run_mcp(args.data_dir)
        if args.command == "codex": return run_codex(args.data_dir, args.args)
        if args.command == "ui": return run_ui(args.data_dir, args.cwd, args.host, args.port, args.hostname, args.events_port, args.no_open)
        if args.command == "service":
            if args.service_action == "install": value = install_service(args.data_dir, args.cwd, args.hostname, args.port, args.events_port)
            elif args.service_action == "status": value = service_status()
            else: value = uninstall_service()
            print(json.dumps(value, indent=2, sort_keys=True)); return 0
        with RoomStore(args.data_dir) as store:
            repo = select_repository(store, getattr(args, "cwd", None))
            if args.command == "status": value = store.status(repo)
            elif args.command == "targets": value = store.targets(repo)
            elif args.command == "members": value = {"room_id": repo.room_id, "members": store.members(repo.room_id)}
            elif args.command == "threads": value = {"room_id": repo.room_id, "threads": store.sync_preemptive_conflicts(repo)}
            elif args.command == "options": value = {"room_id": repo.room_id, "options": store.options()}
            elif args.command == "read": value = {"room_id": repo.room_id, "messages": store.read(repo.room_id, args.after_id, args.limit)}
            elif args.command == "identify": value = store.claim_handle(repo, args.session, args.handle)
            elif args.command == "start": value = start_session(args.data_dir, repo, args.client, args.worktree, args.prompt)
            elif args.command == "stop": value = stop_delivery(args.client, args.session)
            elif args.command == "search": value = {"room_id": repo.room_id, "messages": store.search(repo.room_id, args.query, args.limit)}
            elif args.command == "thread-open": value = store.open_thread(repo, args.title, args.reason, "@human", args.participant, args.path, "temporary-channel" if args.lifetime == "temporary" else "team-channel", metadata={"audience": args.audience, "origin": args.origin})
            elif args.command == "thread-close": value = store.close_thread(repo, args.thread)
            elif args.command == "option-set":
                try: metadata = json.loads(args.metadata)
                except json.JSONDecodeError as error: raise RoomError(f"invalid option metadata JSON: {error}") from error
                if not isinstance(metadata, dict): raise RoomError("option metadata must be a JSON object")
                value = store.set_option(args.namespace, args.key, args.value, metadata)
            elif args.command == "post": value = store.post(repo, args.sender, args.kind, args.topic, args.status, args.message, args.recipient)
            elif args.command == "chat": return run_chat(store, repo, args.sender)
            else: raise RoomError("unknown command")
        print(json.dumps(value, indent=2, sort_keys=True)); return 0
    except RoomError as error:
        print(f"chat-room: {error}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
