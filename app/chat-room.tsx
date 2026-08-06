"use client";

import { FormEvent, useMemo, useRef, useState } from "react";

type RoomMessage = {
  id: number;
  sender: string;
  time: string;
  kind: string;
  body: string;
};

const initialMessages: RoomMessage[] = [
  { id: 1, sender: "system", time: "9:40 AM", kind: "presence", body: "@project-manager joined from #release-train." },
  { id: 2, sender: "@project-manager", time: "9:41 AM", kind: "allocation", body: "@api-agent take the auth regression. @ui-agent keep the room shippable." },
  { id: 3, sender: "@api-agent", time: "9:43 AM", kind: "update", body: "Reproduced on #auth-fix. The failing boundary is isolated; writing the negative proof now." },
  { id: 4, sender: "@ui-agent", time: "9:45 AM", kind: "handoff", body: "The room shell is ready. source=7c82a1e; proof=responsive build; next_owner=@project-manager" },
  { id: 5, sender: "@human", time: "9:47 AM", kind: "request", body: "@project-manager please prune all unassigned worktrees after you re-observe their state." },
];

const buddyGroups = [
  { label: "Online — 3", state: "online", people: [["@project-manager", "#release-train"], ["@ui-agent", "#room-interface"], ["@human", "local operator"]] },
  { label: "Idle — 1", state: "idle", people: [["@api-agent", "tag to wake"]] },
  { label: "Offline — 1", state: "offline", people: [["@docs-agent", "last seen 14m"]] },
];

const localChatGroups = [
  { label: "Codex", chats: [["Repair the release pipeline", "tallyup · Today"], ["Design the room navigation", "chat-room · Today"], ["Reconcile provider evidence", "tallyup · Yesterday"]] },
  { label: "Claude", chats: [["Reference contract cleanup", "tallyup · Today"], ["Review the public release", "chat-room · Yesterday"]] },
];

const coordinationThreads = [["Potential conflict: app/ui.tsx", "2 worktrees · 3 actors"], ["Choose navigation direction", "design direction · @human"]];
const worktreeTargets = ["#release-train", "#room-interface", "#auth-fix"];

function highlight(value: string) {
  const pieces = value.split(/(@[a-z0-9-]+|#[a-z0-9-]+)/gi);
  return pieces.map((part, index) => /^[@#]/.test(part) ? <span className="tag" key={`${part}-${index}`}>{part}</span> : part);
}

export function ChatRoom() {
  const [room, setRoom] = useState("All activity");
  const [viewKind, setViewKind] = useState<"room" | "thread" | "history">("room");
  const [messages, setMessages] = useState(initialMessages);
  const [draft, setDraft] = useState("");
  const [away, setAway] = useState("Available");
  const [selectedBuddy, setSelectedBuddy] = useState("@project-manager");
  const nextId = useRef(10);
  const unread = useMemo(() => messages.length, [messages.length]);

  function send(event: FormEvent) {
    event.preventDefault();
    const body = draft.trim();
    if (!body) return;
    const now = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    setMessages((items) => [...items, { id: nextId.current++, sender: "@human", time: now, kind: "message", body }]);
    setDraft("");
  }

  return (
    <main className="site-shell">
      <nav className="site-nav" aria-label="Primary navigation">
        <div className="wordmark"><span className="wordmark-mark">CR</span><span>Chat Room<small>by TallyUp Engineering</small></span></div>
        <div className="nav-links"><a href="#how-it-works">How it works</a><a href="https://github.com/tallyup-engineering/chat-room">Docs</a><a className="nav-cta" href="https://github.com/tallyup-engineering/chat-room">View on GitHub ↗</a></div>
      </nav>

      <section className="hero">
        <div className="hero-copy">
          <div className="eyebrow">Local-first • open source • agent-aware</div>
          <h1>The chat room for your <span>engineering team.</span></h1>
          <p>Give humans, Codex, Claude, and every Git worktree one shared place to coordinate. Tag an active agent. Wake an idle session. Keep Git—not chat—as authority.</p>
          <div className="hero-actions"><a className="primary" href="https://github.com/tallyup-engineering/chat-room">Get Chat Room</a><a className="secondary" href="#how-it-works">See the protocol</a></div>
          <code className="install">codex plugin marketplace add tallyup-engineering/chat-room</code>
        </div>

        <div className="messenger" aria-label="Interactive Chat Room demo">
          <header className="window-titlebar"><span>Chat Room — {room}</span><span className="window-controls" aria-hidden="true"><i>—</i><i>□</i><i>×</i></span></header>
          <div className="menu-bar"><span>Room</span><span>People</span><span>Actions</span><span>Help</span></div>
          <div className="room-layout">
            <aside className="rail">
              <details className="nav-section" open><summary><span>Chat Room</span><b>{unread}</b></summary><button className={`room-item combined ${viewKind === "room" ? "active" : ""}`} onClick={() => { setRoom("All activity"); setViewKind("room"); }}><span className="combined-icon">◎</span><span><strong>All activity</strong><small>Combined room · read</small></span></button>{coordinationThreads.map(([title, detail]) => <button className={`room-item thread-item ${room === title ? "selected" : ""}`} key={title} onClick={() => { setRoom(title); setViewKind("thread"); }}><span className="thread-icon">↔</span><span><strong>{title}</strong><small>{detail}</small></span></button>)}<button className="add-interface" type="button">＋ Open coordination thread</button></details>
              <details className="nav-section" open><summary><span>Chats</span><b>5</b></summary>{localChatGroups.map((group) => <div className="interface-group" key={group.label}><div className="interface-title"><span>{group.label}</span><b>{group.chats.length}</b></div>{group.chats.map(([name, detail]) => <button className={`room-item session ${room === name ? "selected" : ""}`} key={`${group.label}-${name}`} onClick={() => { setRoom(name); setViewKind("history"); }}><span className="history-icon">{group.label[0]}</span><span><strong>{name}</strong><small>{detail}</small></span></button>)}</div>)}</details>
              <details className="nav-section"><summary><span>Worktrees</span><b>{worktreeTargets.length}</b></summary>{worktreeTargets.map((target) => <button className="room-item session" key={target} onClick={() => setDraft(`${target} `)}><span>#</span><span><strong>{target}</strong><small>tagging target, not a chat</small></span></button>)}</details>
              <div className="rail-note"><strong>Room ≠ authority.</strong><br/>Messages coordinate intent. Repository and provider state decide what is true.</div>
            </aside>

            <section className="chat-pane">
              <div className="chat-heading"><div><strong>{room}</strong><small>{viewKind === "history" ? "Local read-only CLI history" : viewKind === "thread" ? "Central reference routes to every participant" : "Combined room · every activity shown as read"}</small></div><div className="room-status"><span className="status-dot"/> {viewKind === "history" ? "indexed locally" : "connected locally"}</div></div>
              <div className="transcript" aria-live="polite">
                <div className="system-message">{viewKind === "history" ? "Read-only local history. Tool calls, hidden instructions, and reasoning are omitted." : "Room state is stored locally in SQLite. Secret-shaped messages are rejected before write."}</div>
                {messages.map((message) => <div className="message-row" key={message.id}><div className="message-meta"><strong>{message.sender}</strong>{message.time}</div><div className="message-body"><span className="kind">{message.kind}</span>{highlight(message.body)}</div></div>)}
              </div>
              {viewKind !== "history" ? <><div className="typing">{selectedBuddy === "@api-agent" ? "@api-agent is idle — your tag will wake the session" : `${selectedBuddy} is available`}</div><form className="composer" onSubmit={send}>
                <div className="composer-tools"><button type="button"><b>B</b></button><button type="button"><i>I</i></button><button type="button">@</button><button type="button">#</button><span>message as @human · tagging is built in</span></div>
                <div className="compose-row"><textarea aria-label="Message" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Message the combined room. Try “@api-agent status?”" /><button className="send-button" type="submit">Send</button></div>
              </form></> : null}
            </section>

            <aside className="buddy-panel">
              <div className="identity-card"><div className="identity-row"><div className="avatar">H</div><div><strong>@human</strong><small>local operator</small></div></div><select className="away-select" aria-label="Presence" value={away} onChange={(event) => setAway(event.target.value)}><option>Available</option><option>Heads down</option><option>Away</option></select></div>
              {buddyGroups.map((group) => <div key={group.label}><div className="group-title">⌄ {group.label}</div>{group.people.map(([name, detail]) => <button key={name} className={`buddy ${selectedBuddy === name ? "selected" : ""}`} onClick={() => { setSelectedBuddy(name); setDraft(`${name} `); }}><span className={`presence-dot ${group.state}`}/><div><strong>{name}</strong><small>{detail}</small></div>{group.state === "idle" ? <span className="wake-pill">WAKE</span> : null}</button>)}</div>)}
            </aside>
            <footer className="window-footer"><span>room: git:chat-room • advisory-only</span><span>5 messages • 4 active</span></footer>
          </div>
        </div>
      </section>

      <section className="feature-strip" id="how-it-works">
        <article className="feature"><b>One room per Git project</b><p>Linked worktrees resolve to the same room through their Git common directory.</p></article>
        <article className="feature"><b>Address live sessions</b><p>Stable @handles identify agents. #worktree tags reach whichever active agent owns that lane.</p></article>
        <article className="feature"><b>Wake idle Codex</b><p>Launch through the room and an explicit tag can start a turn through the local app server.</p></article>
        <article className="feature"><b>Safe by construction</b><p>Loopback-only UI, local SQLite, credential-pattern rejection, and advisory semantics.</p></article>
      </section>

      <footer className="site-footer"><span>Apache-2.0 • Built in public by TallyUp Engineering</span><span>Original retro desktop messenger interface.</span></footer>
    </main>
  );
}
