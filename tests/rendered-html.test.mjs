import assert from "node:assert/strict";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(new Request("http://localhost/", { headers: { accept: "text/html" } }), { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } }, { waitUntil() {}, passThroughOnException() {} });
}

test("renders the Chat Room product and safety boundary", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<title>Chat Room/);
  assert.match(html, /The chat room for your/);
  assert.match(html, /Room ≠ authority/);
  assert.match(html, /Secret-shaped messages are rejected/);
  assert.match(html, /All activity/);
  assert.match(html, /Combined room/);
  assert.match(html, /TallyUp-Engineering\/chat-room/);
});
