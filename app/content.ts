// The site states facts about a program that lives in this repository, so it reads them
// from the program's own sources at build time rather than restating them.
//
// docs/protocol.md is the load-bearing input: tests/test_room.py already fails when its
// tool and command tables drift from room.py, so a change to the CLI must reach the
// document, and Vite inlines the document here. Nothing on this page is hand-maintained
// except the copy that is genuinely marketing.
import protocol from "../docs/protocol.md?raw";
import plugin from "../plugins/chat-room/.codex-plugin/plugin.json";

export type Entry = { term: string; detail: string };

// No `m` flag: `$` has to mean end of document, so the last section reads to the end.
function section(title: string): string {
  const match = protocol.match(new RegExp(`\\n## ${title}\\n([\\s\\S]*?)(?=\\n## |$)`));
  if (!match) throw new Error(`docs/protocol.md is missing the "${title}" section`);
  return match[1];
}

// Only rows whose first cell is code are data; the header row never matches.
function entries(body: string): Entry[] {
  const rows = [...body.matchAll(/^\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|\s*$/gm)];
  return rows.map((row) => ({ term: row[1], detail: row[2] }));
}

function bullets(body: string): string[] {
  return [...body.matchAll(/^- (.+)$/gm)].map((row) => row[1]);
}

const commandLine = section("Command line");
const writeAt = commandLine.indexOf("| Write |");
if (writeAt < 0) throw new Error('docs/protocol.md is missing the "Write" command table');

export const version = plugin.version;
export const tagline = plugin.interface.shortDescription;
export const readCommands = entries(commandLine.slice(0, writeAt));
export const writeCommands = entries(commandLine.slice(writeAt));
export const mcpTools = entries(section("MCP tools"));
export const rungs = entries(section("House rules"));
export const invariants = bullets(section("Invariants"));

// `code` and **strong** spans, kept as data so the renderer stays markup-free.
export type Span = { text: string; emphasis: "code" | "strong" | "none" };

export function spans(value: string): Span[] {
  const pieces = value.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).filter(Boolean);
  return pieces.map((piece) => {
    if (piece.startsWith("`")) return { text: piece.slice(1, -1), emphasis: "code" as const };
    if (piece.startsWith("**")) return { text: piece.slice(2, -2), emphasis: "strong" as const };
    return { text: piece, emphasis: "none" as const };
  });
}
