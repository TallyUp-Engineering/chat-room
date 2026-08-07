import {
  Entry,
  Span,
  invariants,
  mcpTools,
  readCommands,
  spans,
  tagline,
  version,
  writeCommands,
} from "./content";

const REPOSITORY = "https://github.com/TallyUp-Engineering/chat-room";

function Rich({ value }: { value: string }) {
  return (
    <>
      {spans(value).map((span: Span, index: number) =>
        span.emphasis === "code" ? <code key={index}>{span.text}</code>
        : span.emphasis === "strong" ? <strong key={index}>{span.text}</strong>
        : <span key={index}>{span.text}</span>
      )}
    </>
  );
}

function Group({ label, entries }: { label: string; entries: Entry[] }) {
  return (
    <div className="cli-group">
      <div className="panel-label">{label} — {entries.length}</div>
      {entries.map((entry) => (
        <div className="cli-row" key={entry.term}>
          <code className="cli-term">{entry.term}</code>
          <span className="cli-detail"><Rich value={entry.detail} /></span>
        </div>
      ))}
    </div>
  );
}

export function ChatRoom() {
  return (
    <main className="site-shell">
      <nav className="site-nav" aria-label="Primary navigation">
        <div className="wordmark"><span className="wordmark-mark">CR</span><span>Chat Room<small>local-first coordination</small></span></div>
        <div className="nav-links"><a href="#how-it-works">How it works</a><a href={`${REPOSITORY}/blob/main/docs/protocol.md`}>Docs</a><a className="nav-cta" href={REPOSITORY}>View on GitHub ↗</a></div>
      </nav>

      <section className="hero">
        <div className="hero-copy">
          <div className="eyebrow">Local-first • open source • agent-aware</div>
          <h1>The chat room for your <span>engineering team.</span></h1>
          <p>{tagline} Tag an active agent. Wake an idle session. Keep Git—not chat—as authority. It runs in your terminal and opens no listening socket.</p>
          <div className="hero-actions"><a className="primary" href={REPOSITORY}>Get Chat Room</a><a className="secondary" href="#how-it-works">See the protocol</a></div>
          <code className="install">codex plugin marketplace add TallyUp-Engineering/chat-room</code>
        </div>

        <div className="messenger" aria-label={`The chat-room ${version} command surface`}>
          <header className="window-titlebar"><span>chat-room — v{version}</span><span className="window-controls" aria-hidden="true"><i>—</i><i>□</i><i>×</i></span></header>
          <div className="menu-bar"><span>Read</span><span>Write</span><span>Tools</span><span>Invariants</span></div>
          <div className="cli-layout">
            <aside className="rail">
              <div className="panel-label">Invariants</div>
              <ul className="invariant-list">
                {invariants.map((line) => <li key={line}><Rich value={line} /></li>)}
              </ul>
              <div className="rail-note"><strong>Room ≠ authority.</strong><br/>Messages coordinate intent. Repository and provider state decide what is true.</div>
            </aside>

            <section className="cli-main">
              <div className="system-message">
                Every command and tool below is read from <code>docs/protocol.md</code> when this page
                is built, and the test suite fails when that document stops matching the program.
                This list cannot quietly go stale.
              </div>
              <Group label="Read" entries={readCommands} />
              <Group label="Write" entries={writeCommands} />
              <Group label="MCP tools" entries={mcpTools} />
            </section>
            <footer className="window-footer">
              <span>room: git:chat-room • advisory-only</span>
              <span>{readCommands.length + writeCommands.length} commands • {mcpTools.length} MCP tools</span>
            </footer>
          </div>
        </div>
      </section>

      <section className="feature-strip" id="how-it-works">
        <article className="feature"><b>Coordinate open CLIs</b><p>See active actors together and pull the right sessions into one focused channel.</p></article>
        <article className="feature"><b>Prevent collisions</b><p>Potential shared-worktree and same-file conflicts become temporary coordination channels.</p></article>
        <article className="feature"><b>Find neglected work</b><p>Mechanical activity signals surface potentially stale worktrees for investigation, never automatic deletion.</p></article>
        <article className="feature"><b>Keep authority clear</b><p>Coordination stays advisory; Git and provider observations remain authoritative.</p></article>
      </section>

      <footer className="site-footer"><span>Apache-2.0 • Runs entirely on your machine</span><span>Generated from the protocol document at build time.</span></footer>
    </main>
  );
}
