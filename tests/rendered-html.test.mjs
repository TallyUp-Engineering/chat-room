import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const root = new URL("../", import.meta.url);
const protocol = readFileSync(new URL("docs/protocol.md", root), "utf8");
const plugin = JSON.parse(readFileSync(new URL("plugins/chat-room/.codex-plugin/plugin.json", root), "utf8"));

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(new Request("http://localhost/", { headers: { accept: "text/html" } }), { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } }, { waitUntil() {}, passThroughOnException() {} });
}

// React separates adjacent text nodes with an empty comment, which would split any
// assertion that spans an interpolation boundary.
async function body() {
  return (await (await render()).text()).replaceAll("<!-- -->", "");
}

test("renders the Chat Room product and safety boundary", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<title>Chat Room/);
  assert.match(html, /The chat room for your/);
  assert.match(html, /Room ≠ authority/);
  assert.match(html, /TallyUp-Engineering\/chat-room/);
});

// The page states facts about the program. It earns that by generating them from the
// program's own sources, so the drift this guards against is the generation silently
// falling back to nothing — a parser that matches zero rows still renders a valid page.
test("publishes every command and tool the protocol document declares", async () => {
  const html = await body();
  const declared = [...protocol.matchAll(/^\|\s*`([^`]+)`\s*\|/gm)].map((row) => row[1]);
  assert.ok(declared.length > 20, `expected a populated protocol document, saw ${declared.length} rows`);
  for (const term of declared) {
    assert.ok(html.includes(term), `the site omits "${term}", which docs/protocol.md declares`);
  }
});

test("takes its version from the plugin manifest", async () => {
  const html = await body();
  assert.match(html, new RegExp(`v${plugin.version.replace(/\./g, "\\.")}`));
});
