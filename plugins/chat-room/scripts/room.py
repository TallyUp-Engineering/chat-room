#!/usr/bin/env python3
"""Chat Room: a local, advisory message bus for Git worktrees and agents."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import plistlib
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
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urlparse


PLUGIN_NAME = "chat-room"
VERSION = "0.2.0"
SCHEMA_VERSION = 2
ACTIVE_WINDOW_SECONDS = 30 * 60
WAKE_ENDPOINT_ENV = "CHAT_ROOM_WAKE_ENDPOINT"
CLIENT_ENV = "CHAT_ROOM_CLIENT"
DATA_ENV = "CHAT_ROOM_DATA"
MESSAGE_KINDS = (
    "allocation", "request", "decision", "observation", "update",
    "blocker", "defect", "handoff", "authority", "proposal", "message",
)
CONTEXT_EVENTS = ("SessionStart", "UserPromptSubmit", "SubagentStart", "PostToolUse")
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
CONFLICT_SCAN_TTL_SECONDS = 30
CONFLICT_SCAN_LOCK = threading.Lock()
CONFLICT_SCANS: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
CONFLICT_SCANS_RUNNING: Set[str] = set()


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
            "worktree": Path(cwd).name or "worktree", "read_only": True,
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
            "updated_at": updated, "recency": chat_recency(updated), "worktree": Path(project).name or "worktree", "read_only": True,
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
    return {"chat": summary, "messages": messages[-1000:]}


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
    return items


def changed_worktree_paths(path: Path) -> Set[str]:
    try:
        result = subprocess.run(["git", "-C", str(path), "status", "--porcelain=v1", "-z", "--untracked-files=all"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {entry[3:] for entry in result.stdout.split("\0") if len(entry) > 3 and entry[2] == " "}


def scan_preemptive_conflicts(repo: Repository) -> None:
    by_path: Dict[str, List[Dict[str, Any]]] = {}
    try:
        for worktree in list_worktree_references(repo):
            path = Path(str(worktree["path"]))
            if not path.exists():
                continue
            for changed in changed_worktree_paths(path):
                by_path.setdefault(changed, []).append(worktree)
        conflicts = [{"path": path, "worktrees": worktrees} for path, worktrees in by_path.items() if len(worktrees) > 1]
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


def coordination_alerts(targets: Dict[str, Any], threads: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
    return alerts


def client_name() -> str:
    value = os.environ.get(CLIENT_ENV, "codex").strip().lower()
    return value if value in ("codex", "claude", "human") else "agent"


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


def wake_codex(endpoint: str, session_id: str) -> None:
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
        call(2, "turn/start", {"threadId": session_id, "input": [{"type": "text", "text": WAKE_PROMPT}]})
    finally:
        connection.close()


class RoomStore:
    def __init__(self, data_dir: Path) -> None:
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

    def _migrate(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rooms(room_id TEXT PRIMARY KEY,project_identity TEXT NOT NULL,common_dir TEXT NOT NULL,repository_root TEXT NOT NULL,created_at TEXT NOT NULL,last_seen_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,room_id TEXT NOT NULL,timestamp TEXT NOT NULL,session_id TEXT,sender TEXT NOT NULL,recipients_json TEXT NOT NULL,kind TEXT NOT NULL,topic TEXT NOT NULL,status TEXT NOT NULL,message TEXT NOT NULL,cwd TEXT,worktree TEXT,branch TEXT,head TEXT,metadata_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS messages_room_id_id ON messages(room_id,id);
        CREATE TABLE IF NOT EXISTS presence(room_id TEXT NOT NULL,participant_id TEXT NOT NULL,session_id TEXT,agent_id TEXT,role TEXT NOT NULL,state TEXT NOT NULL,cwd TEXT NOT NULL,worktree TEXT NOT NULL,branch TEXT,head TEXT,started_at TEXT NOT NULL,seen_at TEXT NOT NULL,last_event TEXT NOT NULL,handle TEXT,wake_endpoint TEXT,PRIMARY KEY(room_id,participant_id));
        CREATE TABLE IF NOT EXISTS cursors(room_id TEXT NOT NULL,participant_id TEXT NOT NULL,last_message_id INTEGER NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(room_id,participant_id));
        CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,room_id TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,status TEXT NOT NULL,title TEXT NOT NULL,reason TEXT NOT NULL,opener TEXT NOT NULL,participants_json TEXT NOT NULL,paths_json TEXT NOT NULL,source TEXT NOT NULL,metadata_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS threads_room_status_updated ON threads(room_id,status,updated_at DESC);
        """)
        self.connection.execute("INSERT OR REPLACE INTO metadata VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        self.connection.commit()

    def register_room(self, repo: Repository) -> None:
        now = utc_now()
        self.connection.execute("""INSERT INTO rooms VALUES(?,?,?,?,?,?) ON CONFLICT(room_id) DO UPDATE SET project_identity=excluded.project_identity,common_dir=excluded.common_dir,repository_root=excluded.repository_root,last_seen_at=excluded.last_seen_at""", (repo.room_id, repo.project_identity, str(repo.common_dir), str(repo.worktree), now, now))
        self.connection.commit()

    def latest_repository(self) -> Optional[Repository]:
        row = self.connection.execute("SELECT repository_root FROM rooms ORDER BY last_seen_at DESC LIMIT 1").fetchone()
        return resolve_repository(row["repository_root"]) if row else None

    def upsert_presence(self, repo: Repository, participant_id: str, session_id: Optional[str], agent_id: Optional[str], role: str, state: str, event: str, wake_endpoint: Optional[str] = None) -> None:
        self.register_room(repo)
        now = utc_now()
        previous = self.connection.execute("SELECT handle,started_at FROM presence WHERE room_id=? AND participant_id=?", (repo.room_id, participant_id)).fetchone()
        handle = str(previous["handle"]) if previous and previous["handle"] else slug(role.replace(":", "-"), "agent")
        started = str(previous["started_at"]) if previous else now
        self.connection.execute("""INSERT INTO presence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(room_id,participant_id) DO UPDATE SET session_id=excluded.session_id,agent_id=excluded.agent_id,role=excluded.role,state=excluded.state,cwd=excluded.cwd,worktree=excluded.worktree,branch=excluded.branch,head=excluded.head,seen_at=excluded.seen_at,last_event=excluded.last_event,wake_endpoint=excluded.wake_endpoint""", (repo.room_id, participant_id, session_id, agent_id, role, state, str(repo.cwd), str(repo.worktree), repo.branch, repo.head, started, now, event, handle, wake_endpoint))
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
        handle = target_token(requested)[1:]
        active = [m for m in self.members(repo.room_id) if m["state"] != "offline"]
        matches = [m for m in active if str(m.get("session_id") or "").startswith(session_ref) or str(m["participant_id"]).startswith(session_ref)]
        if len(matches) != 1: raise RoomError("session reference must match exactly one active session")
        if any(m["target"] == "@" + handle and m["participant_id"] != matches[0]["participant_id"] for m in active): raise RoomError(f"@{handle} is already active")
        self.connection.execute("UPDATE presence SET handle=?,seen_at=? WHERE room_id=? AND participant_id=?", (handle, utc_now(), repo.room_id, matches[0]["participant_id"]))
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
            result.append(value)
        return result

    def thread(self, repo: Repository, thread_id: str) -> Dict[str, Any]:
        value = next((item for item in self.threads(repo, True) if item["id"] == thread_id), None)
        if value is None:
            raise RoomError("coordination thread does not exist")
        return value

    def open_thread(self, repo: Repository, title: str, reason: str, opener: str, participants: Sequence[str], paths: Sequence[str], source: str = "manual", thread_id: Optional[str] = None) -> Dict[str, Any]:
        heading = concise(ensure_value_free(title), 120)
        why = concise(ensure_value_free(reason), 240)
        targets = list(dict.fromkeys(target_token(value) for value in participants if str(value).strip()))
        unknown = [value for value in targets if value not in self.allowed_targets(repo)]
        if unknown:
            raise RoomError("inactive or unknown thread participant(s): " + ", ".join(unknown))
        clean_paths = list(dict.fromkeys(concise(path, 300) for path in paths if str(path).strip()))
        now = utc_now()
        identifier = thread_id or "thread-" + hashlib.sha256(os.urandom(32)).hexdigest()[:12]
        self.connection.execute(
            """INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,status='open',title=excluded.title,reason=excluded.reason,participants_json=excluded.participants_json,paths_json=excluded.paths_json,metadata_json=excluded.metadata_json""",
            (identifier, repo.room_id, now, now, "open", heading, why, concise(opener, 100), json.dumps(targets), json.dumps(clean_paths), source, json.dumps({}, sort_keys=True)),
        )
        self.connection.commit()
        return self.thread(repo, identifier)

    def close_thread(self, repo: Repository, thread_id: str) -> Dict[str, Any]:
        self.thread(repo, thread_id)
        self.connection.execute("UPDATE threads SET status='resolved',updated_at=? WHERE room_id=? AND id=?", (utc_now(), repo.room_id, thread_id))
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
        for conflict in conflicts:
            worktrees = conflict["worktrees"]
            worktree_paths = {str(item["path"]) for item in worktrees}
            participants = [str(item["target"]) for item in worktrees]
            participants.extend(str(member["target"]) for member in members if member["state"] != "offline" and str(member["worktree"]) in worktree_paths)
            stable_targets = sorted(str(item["target"]) for item in worktrees)
            digest = hashlib.sha256(f"{repo.room_id}\n{conflict['path']}\n{' '.join(stable_targets)}".encode()).hexdigest()[:12]
            thread_id = "conflict-" + digest
            active_ids.add(thread_id)
            self.open_thread(repo, f"Potential conflict: {conflict['path']}", "preemptive file overlap", "@chat-room", participants, [str(conflict["path"])], "preemptive-conflict", thread_id)
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
        if kind not in MESSAGE_KINDS: raise RoomError("unsupported message kind")
        self.register_room(repo)
        body = ensure_value_free(message)
        resolved = self.resolve_targets(repo, recipients, body)
        now = utc_now()
        cursor = self.connection.execute("""INSERT INTO messages(room_id,timestamp,session_id,sender,recipients_json,kind,topic,status,message,cwd,worktree,branch,head,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (repo.room_id, now, session_id, sender[:100], json.dumps(resolved), kind, topic[:120], status[:160], body, str(repo.cwd), str(repo.worktree), repo.branch, repo.head, json.dumps(metadata or {}, sort_keys=True)))
        self.connection.commit()
        posted = self.message(int(cursor.lastrowid))
        posted["wake"] = self.dispatch_wakes(repo, posted)
        return posted

    def message(self, message_id: int) -> Dict[str, Any]:
        row = self.connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None: raise RoomError("message does not exist")
        return message_from_row(row)

    def read(self, room_id: str, after_id: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM messages WHERE room_id=? AND id>? ORDER BY id ASC LIMIT ?", (room_id, max(0, int(after_id)), max(1, min(100, int(limit))))).fetchall()
        return [message_from_row(row) for row in rows]

    def recent(self, room_id: str, limit: int = 30) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT ?", (room_id, max(1, min(2000, int(limit))))).fetchall()
        return [message_from_row(row) for row in reversed(rows)]

    def status(self, repo: Repository) -> Dict[str, Any]:
        self.register_room(repo); members = self.members(repo.room_id)
        last = self.connection.execute("SELECT COALESCE(MAX(id),0) value FROM messages WHERE room_id=?", (repo.room_id,)).fetchone()["value"]
        return {"room_id": repo.room_id, "project_identity": repo.project_identity, "common_dir": str(repo.common_dir), "worktree": str(repo.worktree), "branch": repo.branch, "head": repo.head, "members_total": len(members), "members_online": sum(m["state"] == "online" for m in members), "members_idle": sum(m["state"] == "idle" for m in members), "last_message_id": int(last), "authority": "advisory-only"}

    def dispatch_wakes(self, repo: Repository, message: Dict[str, Any]) -> Dict[str, Any]:
        recipients = set(message["recipients"]); result: Dict[str, Any] = {"attempted": 0, "started": [], "failed": []}
        for member in self.members(repo.room_id):
            if not member.get("wakeable_idle") or not recipients.intersection({member["target"], member["worktree_target"]}): continue
            if message.get("session_id") == member.get("session_id"): continue
            result["attempted"] += 1
            try:
                wake_codex(str(member["wake_endpoint"]), str(member["session_id"])); result["started"].append(member["target"])
            except RoomError as error:
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
    messages = [m for m in store.recent(repo.room_id, 30) if not m["recipients"] or targets.intersection(m["recipients"])][-10:]
    lines = [f"Chat Room (advisory): room={repo.room_id} handle={member['target']} worktree={member['worktree_target']} branch={repo.branch or 'detached'}.", "Use room tools for material coordination. Re-observe repository state before acting."]
    if messages:
        lines.append("Recent room messages:")
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
        {"name": "room_threads", "description": "List open manual and preemptive-conflict coordination threads.", "inputSchema": {"type": "object", "properties": {"cwd": cwd}}},
        {"name": "room_thread_open", "description": "Open a coordination thread for design direction, review, handoff, blocker, or conflict resolution.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "title": {"type": "string"}, "reason": {"type": "string"}, "participants": {"type": "array", "items": {"type": "string"}}, "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "reason"]}},
        {"name": "room_thread_close", "description": "Mark a coordination thread resolved without changing Git state.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "thread_id": {"type": "string"}}, "required": ["thread_id"]}},
        {"name": "room_identify", "description": "Assign an active session a semantic @handle.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "session_id": {"type": "string"}, "handle": {"type": "string"}}, "required": ["session_id", "handle"]}},
        {"name": "room_post", "description": "Post one value-free coordination message. Use thread_id for central routing, or tag active @handles and #worktrees ad hoc.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "sender": {"type": "string"}, "session_id": {"type": "string"}, "thread_id": {"type": "string"}, "recipients": {"type": "array", "items": {"type": "string"}}, "kind": {"type": "string", "enum": list(MESSAGE_KINDS)}, "topic": {"type": "string"}, "status": {"type": "string"}, "message": {"type": "string", "maxLength": 4000}}, "required": ["kind", "topic", "message"]}},
        {"name": "room_handoff", "description": "Post a structured handoff with source, paths, proof, blocker, and next owner.", "inputSchema": {"type": "object", "properties": {"cwd": cwd, "sender": {"type": "string"}, "session_id": {"type": "string"}, "topic": {"type": "string"}, "source_sha": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}, "proof": {"type": "string"}, "blocker": {"type": "string"}, "next_owner": {"type": "string"}}, "required": ["topic", "source_sha", "paths", "proof", "next_owner"]}},
    ]


def execute_tool(store: RoomStore, name: str, args: Dict[str, Any]) -> Any:
    repo = select_repository(store, args.get("cwd"))
    if name == "room_status": return store.status(repo)
    if name == "room_read": return {"room_id": repo.room_id, "messages": store.read(repo.room_id, int(args.get("after_id", 0)), int(args.get("limit", 50)))}
    if name == "room_members": return {"room_id": repo.room_id, "members": store.members(repo.room_id)}
    if name == "room_targets": return store.targets(repo)
    if name == "room_threads": return {"room_id": repo.room_id, "threads": store.sync_preemptive_conflicts(repo)}
    if name == "room_thread_open": return store.open_thread(repo, str(args["title"]), str(args["reason"]), str(args.get("opener") or f"{client_name()}-session"), [str(x) for x in args.get("participants", [])], [str(x) for x in args.get("paths", [])])
    if name == "room_thread_close": return store.close_thread(repo, str(args["thread_id"]))
    if name == "room_identify": return store.claim_handle(repo, str(args["session_id"]), str(args["handle"]))
    if name == "room_post":
        sender = str(args.get("sender") or f"{client_name()}-session"); session_id = str(args.get("session_id") or "") or None
        if args.get("thread_id"): return store.post_thread(repo, str(args["thread_id"]), sender, str(args["message"]), session_id)
        return store.post(repo, sender, str(args["kind"]), str(args["topic"]), str(args.get("status") or "posted"), str(args["message"]), [str(x) for x in args.get("recipients", [])], session_id)
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
    server_version = "ChatRoom/0.1"
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
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(payload)
    def do_GET(self) -> None:
        if not self.valid_host(): self.send_error(HTTPStatus.MISDIRECTED_REQUEST); return
        parsed = urlparse(self.path); path = parsed.path
        if path == "/api/snapshot":
            with RoomStore(self.app.data_dir) as store:
                threads = store.sync_preemptive_conflicts(self.app.repo)
                targets = store.targets(self.app.repo)
                self.send_json({"status": store.status(self.app.repo), "messages": store.recent(self.app.repo.room_id, 2000), "targets": targets, "threads": threads, "alerts": coordination_alerts(targets, threads)})
            return
        if path == "/api/chats":
            if not self.authorized(): self.send_json({"error": "invalid local token"}, 403); return
            chats, _files = discover_chat_catalog(self.app.repo)
            self.send_json({"chats": chats})
            return
        if path == "/api/chat":
            if not self.authorized(): self.send_json({"error": "invalid local token"}, 403); return
            query = parse_qs(parsed.query)
            try:
                self.send_json(chat_transcript(self.app.repo, str(query.get("client", [""])[0]), str(query.get("id", [""])[0])))
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
        if path not in ("/api/messages", "/api/threads", "/api/thread-close"): self.send_error(404); return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 16384); body = json.loads(self.rfile.read(length) or b"{}")
            with RoomStore(self.app.data_dir) as store:
                if path == "/api/threads":
                    value = store.open_thread(self.app.repo, str(body.get("title") or ""), str(body.get("reason") or "coordination"), "@human", [str(x) for x in body.get("participants", [])], [str(x) for x in body.get("paths", [])])
                elif path == "/api/thread-close":
                    value = store.close_thread(self.app.repo, str(body.get("thread_id") or ""))
                elif body.get("thread_id"):
                    value = store.post_thread(self.app.repo, str(body["thread_id"]), "@human", str(body.get("message") or ""))
                else:
                    value = store.post(self.app.repo, "@human", str(body.get("kind") or "message"), str(body.get("topic") or "general"), "posted", str(body.get("message") or ""), [str(x) for x in body.get("recipients", [])])
            self.send_json(value, 201)
        except (RoomError, ValueError, json.JSONDecodeError) as error: self.send_json({"error": str(error)}, 400)


class RoomHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: Tuple[str, int], repo: Repository, data_dir: Path, static_dir: Path, token: str, hostname: str):
        super().__init__(address, RoomHandler); self.repo = repo; self.data_dir = data_dir; self.static_dir = static_dir; self.token = token; self.hostname = hostname


def run_ui(data_dir: Path, cwd: Optional[str], host: str, port: int, hostname: str, no_open: bool) -> int:
    with RoomStore(data_dir) as store: repo = select_repository(store, cwd); store.register_room(repo)
    if host not in ("127.0.0.1", "localhost", "::1"): raise RoomError("the UI binds only to loopback")
    hostname = hostname.strip().lower().rstrip(".")
    if hostname != "localhost" and not hostname.endswith(".localhost"): raise RoomError("the browser hostname must be localhost or end in .localhost")
    token = hashlib.sha256(os.urandom(32)).hexdigest(); static_dir = Path(__file__).resolve().parents[1] / "assets"
    server = RoomHTTPServer((host, port), repo, data_dir, static_dir, token, hostname); url = f"http://{hostname}:{server.server_address[1]}/"
    print(f"Chat Room {repo.room_id}\n{url}\nPress Ctrl-C to stop.")
    if not no_open: threading.Timer(.3, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


def service_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.accountable.chat-room.plist"


def install_service(data_dir: Path, cwd: Optional[str], hostname: str, port: int) -> Dict[str, Any]:
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
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "--data-dir", str(data_dir), "ui", "--cwd", str(repo.worktree), "--host", "127.0.0.1", "--port", str(port), "--hostname", hostname, "--no-open"],
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
    return {"status": "installed", "url": f"http://{hostname}:{port}/", "project": repo.project_identity, "plist": str(plist_path)}


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
    executable = shutil.which("codex")
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
    sub = value.add_subparsers(dest="command", required=True)
    for name in ("status", "targets", "members", "threads", "read", "chat", "ui"):
        command = sub.add_parser(name); command.add_argument("--cwd")
        if name == "read": command.add_argument("--after-id", type=int, default=0); command.add_argument("--limit", type=int, default=50)
        if name == "chat": command.add_argument("--sender", default="@human")
        if name == "ui": command.add_argument("--host", default="127.0.0.1"); command.add_argument("--port", type=int, default=7391); command.add_argument("--hostname", default="chatroom.localhost"); command.add_argument("--no-open", action="store_true")
    post = sub.add_parser("post"); post.add_argument("--cwd"); post.add_argument("--sender", default="@human"); post.add_argument("--kind", choices=MESSAGE_KINDS, default="message"); post.add_argument("--topic", default="general"); post.add_argument("--status", default="posted"); post.add_argument("--recipient", action="append", default=[]); post.add_argument("--message", required=True)
    identify = sub.add_parser("identify"); identify.add_argument("--cwd"); identify.add_argument("--session", required=True); identify.add_argument("--handle", required=True)
    thread_open = sub.add_parser("thread-open"); thread_open.add_argument("--cwd"); thread_open.add_argument("--title", required=True); thread_open.add_argument("--reason", default="coordination"); thread_open.add_argument("--participant", action="append", default=[]); thread_open.add_argument("--path", action="append", default=[])
    thread_close = sub.add_parser("thread-close"); thread_close.add_argument("--cwd"); thread_close.add_argument("--thread", required=True)
    service = sub.add_parser("service"); service_actions = service.add_subparsers(dest="service_action", required=True)
    service_install = service_actions.add_parser("install"); service_install.add_argument("--cwd"); service_install.add_argument("--hostname", default="chatroom.localhost"); service_install.add_argument("--port", type=int, default=7391)
    service_actions.add_parser("status"); service_actions.add_parser("uninstall")
    sub.add_parser("hook"); sub.add_parser("mcp")
    codex = sub.add_parser("codex"); codex.add_argument("args", nargs=argparse.REMAINDER)
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "hook": return run_hook(args.data_dir)
        if args.command == "mcp": return run_mcp(args.data_dir)
        if args.command == "codex": return run_codex(args.data_dir, args.args)
        if args.command == "ui": return run_ui(args.data_dir, args.cwd, args.host, args.port, args.hostname, args.no_open)
        if args.command == "service":
            if args.service_action == "install": value = install_service(args.data_dir, args.cwd, args.hostname, args.port)
            elif args.service_action == "status": value = service_status()
            else: value = uninstall_service()
            print(json.dumps(value, indent=2, sort_keys=True)); return 0
        with RoomStore(args.data_dir) as store:
            repo = select_repository(store, getattr(args, "cwd", None))
            if args.command == "status": value = store.status(repo)
            elif args.command == "targets": value = store.targets(repo)
            elif args.command == "members": value = {"room_id": repo.room_id, "members": store.members(repo.room_id)}
            elif args.command == "threads": value = {"room_id": repo.room_id, "threads": store.sync_preemptive_conflicts(repo)}
            elif args.command == "read": value = {"room_id": repo.room_id, "messages": store.read(repo.room_id, args.after_id, args.limit)}
            elif args.command == "identify": value = store.claim_handle(repo, args.session, args.handle)
            elif args.command == "thread-open": value = store.open_thread(repo, args.title, args.reason, "@human", args.participant, args.path)
            elif args.command == "thread-close": value = store.close_thread(repo, args.thread)
            elif args.command == "post": value = store.post(repo, args.sender, args.kind, args.topic, args.status, args.message, args.recipient)
            elif args.command == "chat": return run_chat(store, repo, args.sender)
            else: raise RoomError("unknown command")
        print(json.dumps(value, indent=2, sort_keys=True)); return 0
    except RoomError as error:
        print(f"chat-room: {error}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
