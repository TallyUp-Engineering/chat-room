const token = new URL(location.href).hash.match(/token=([a-f0-9]+)/)?.[1] || "";
const authHeaders = token ? { "X-Chat-Room-Token": token } : {};
let currentView = { kind: "room", id: "room" };
let roomData = null;
let catalogData = { chats: [] };

const esc = value => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" })[character]);
const rich = value => esc(value).replace(/(^|\s)([@#][a-z0-9-]+)/gi, '$1<span class="tag">$2</span>');
const time = value => value ? new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";

async function request(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options, headers: { ...authHeaders, ...(options.headers || {}) } });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Chat Room service unavailable");
  return body;
}

function showError(error) {
  const item = document.querySelector("#error");
  item.textContent = error.message;
  item.hidden = false;
}

function clearError() { document.querySelector("#error").hidden = true; }

function targetInput(target) {
  const input = document.querySelector("#message");
  input.value = `${target} ${input.value}`;
  input.focus();
}

function attachTargetHandlers() {
  document.querySelectorAll("[data-target]").forEach(node => { node.onclick = () => targetInput(node.dataset.target); });
}

function buddy(member) {
  const state = member.state === "online" ? "" : member.state;
  return `<button class="buddy" data-target="${esc(member.target)}"><span class="dot ${state}"></span><div><b>${esc(member.target)}</b><small>${esc(member.worktree_target)} · ${esc(member.role)}</small></div>${member.wakeable_idle ? '<span class="wake">WAKE</span>' : ''}</button>`;
}

function resetMessages(notice) {
  const messages = document.querySelector("#messages");
  messages.innerHTML = `<p class="${currentView.kind === 'history' ? 'history-notice' : 'notice'}">${esc(notice)}</p>`;
  return messages;
}

function renderRoomMessages(data) {
  const thread = currentView.kind === "thread" ? data.threads.find(item => item.id === currentView.id) : null;
  const items = thread ? data.messages.filter(item => item.metadata?.thread_id === thread.id) : data.messages;
  const messages = resetMessages(thread ? `${thread.reason} · ${thread.participants.join(' ') || 'no participants yet'}` : "All Chat Room activity is shown as read. Tag an actor or worktree directly, or open a coordination thread.");
  for (const message of items) {
    const row = document.createElement("div");
    row.className = "msg";
    row.innerHTML = `<div class="meta"><b>${esc(message.sender)}</b>${time(message.timestamp)}</div><div><span class="kind">${esc(message.kind)}</span>${rich(message.message)}</div>`;
    messages.append(row);
  }
  messages.scrollTop = messages.scrollHeight;
}

function renderThreads(threads) {
  const container = document.querySelector("#threads");
  document.querySelector("#thread-count").textContent = threads.length;
  container.innerHTML = threads.map(thread => `<button class="thread-item ${currentView.kind === 'thread' && currentView.id === thread.id ? 'selected' : ''}" data-thread="${esc(thread.id)}"><span class="thread-mark">↔</span><span><b>${esc(thread.title)}</b><small>${esc(thread.reason)} · ${thread.participants.length} involved</small></span></button>`).join("");
  container.querySelectorAll("[data-thread]").forEach(node => { node.onclick = () => openThread(node.dataset.thread); });
}

function renderSnapshot(data) {
  roomData = data;
  document.querySelector("#room-title").textContent = data.status.room_id;
  document.querySelector("#authority").textContent = `${data.status.project_identity} • advisory-only`;
  document.querySelector("#counts").textContent = `${data.messages.length} messages • ${data.status.members_online} active`;
  document.querySelector("#combined-count").textContent = data.messages.length;
  document.querySelector("#worktree-count").textContent = data.targets.worktrees.length;
  document.querySelector("#worktrees").innerHTML = data.targets.worktrees.map(worktree => `<button data-target="${esc(worktree.target)}"># ${esc(worktree.name)} ${worktree.active_agents ? `(${worktree.active_agents})` : ''}</button>`).join("");
  renderThreads(data.threads || []);
  const online = data.targets.agents.filter(agent => agent.state === "online");
  const idle = data.targets.agents.filter(agent => agent.state === "idle");
  const offline = data.targets.agents.filter(agent => agent.state === "offline");
  document.querySelector("#members").innerHTML = `<div class="group">⌄ Online — ${online.length}</div>${online.map(buddy).join('')}<div class="group">⌄ Idle — ${idle.length}</div>${idle.map(buddy).join('')}<div class="group">⌄ Offline — ${offline.length}</div>${offline.map(buddy).join('')}`;
  attachTargetHandlers();
  if (currentView.kind === "room" || currentView.kind === "thread") renderRoomView();
}

function renderRoomView() {
  if (!roomData) return;
  const thread = currentView.kind === "thread" ? roomData.threads.find(item => item.id === currentView.id) : null;
  if (currentView.kind === "thread" && !thread) currentView = { kind: "room", id: "room" };
  const activeThread = currentView.kind === "thread" ? roomData.threads.find(item => item.id === currentView.id) : null;
  document.querySelector("#chat-pane").classList.remove("history-mode");
  document.querySelector("#view-title").textContent = activeThread?.title || "All activity";
  document.querySelector("#identity").textContent = activeThread ? `${activeThread.reason} · central ref ${activeThread.id}` : `${roomData.status.project_identity} · combined room`;
  document.querySelector("#view-status").textContent = "● connected locally";
  document.querySelector("#close-thread").hidden = !activeThread;
  document.querySelector("#compose-label").textContent = activeThread ? `thread ${activeThread.id} · routes to every participant` : "message as @human · ad hoc tagging is built in";
  document.querySelector("#combined-room").classList.toggle("active", !activeThread);
  renderThreads(roomData.threads || []);
  renderRoomMessages(roomData);
}

function openRoom() {
  currentView = { kind: "room", id: "room" };
  renderRoomView();
}

function openThread(threadId) {
  currentView = { kind: "thread", id: threadId };
  renderRoomView();
}

function renderCatalog(data) {
  catalogData = data;
  const query = document.querySelector("#chat-filter").value.trim().toLowerCase();
  const visibleChats = query ? data.chats.filter(chat => `${chat.client} ${chat.title} ${chat.worktree}`.toLowerCase().includes(query)) : data.chats;
  const groups = new Map();
  for (const chat of visibleChats) {
    if (!groups.has(chat.client)) groups.set(chat.client, []);
    groups.get(chat.client).push(chat);
  }
  document.querySelector("#chat-count").textContent = query ? `${visibleChats.length}/${data.chats.length}` : data.chats.length;
  document.querySelector("#chats").innerHTML = [...groups.entries()].map(([client, chats]) => `<section class="chat-group"><div class="chat-group-title">${esc(client)} — ${chats.length}</div>${chats.map(chat => `<button class="history-item ${currentView.kind === 'history' && currentView.id === chat.id ? 'selected' : ''}" data-client="${esc(chat.client)}" data-chat="${esc(chat.id)}"><span class="history-mark">${esc(chat.client.slice(0, 1))}</span><span><b>${esc(chat.title)}</b><small>${esc(chat.worktree)} · ${time(chat.updated_at)}</small></span></button>`).join('')}</section>`).join('') || '<p class="nav-empty">No matching local chat histories.</p>';
  document.querySelectorAll("[data-chat]").forEach(node => { node.onclick = () => openHistory(node.dataset.client, node.dataset.chat); });
}

async function openHistory(client, sessionId) {
  try {
    const data = await request(`/api/chat?client=${encodeURIComponent(client)}&id=${encodeURIComponent(sessionId)}`);
    currentView = { kind: "history", id: sessionId, client };
    document.querySelector("#chat-pane").classList.add("history-mode");
    document.querySelector("#view-title").textContent = data.chat.title;
    document.querySelector("#identity").textContent = `${data.chat.client} · ${data.chat.worktree} · local read-only history`;
    document.querySelector("#view-status").textContent = "● indexed locally";
    document.querySelector("#close-thread").hidden = true;
    document.querySelector("#combined-room").classList.remove("active");
    const messages = resetMessages("Read-only mirror of the local CLI history. Tool calls, hidden instructions, and reasoning are not displayed or imported.");
    for (const message of data.messages) {
      const row = document.createElement("div");
      row.className = "msg";
      row.innerHTML = `<div class="meta"><b>${message.role === 'user' ? '@human' : esc(data.chat.client)}</b>${time(message.timestamp)}</div><div>${rich(message.body)}</div>`;
      messages.append(row);
    }
    messages.scrollTop = messages.scrollHeight;
    await refreshCatalog();
    clearError();
  } catch (error) { showError(error); }
}

async function refreshRoom() {
  try { renderSnapshot(await request("/api/snapshot")); clearError(); }
  catch (error) { showError(error); }
}

async function refreshCatalog() {
  try { renderCatalog(await request("/api/chats")); clearError(); }
  catch (error) { showError(error); }
}

document.querySelector("#combined-room").onclick = openRoom;
document.querySelector("#chat-filter").oninput = () => renderCatalog(catalogData);
document.querySelector("#toggle-sidebar").onclick = () => {
  const collapsed = document.querySelector("#layout").classList.toggle("sidebar-collapsed");
  document.querySelector("#toggle-sidebar").textContent = collapsed ? "Show sidebar" : "Hide sidebar";
  localStorage.setItem("chat-room-sidebar", collapsed ? "collapsed" : "open");
};
if (localStorage.getItem("chat-room-sidebar") === "collapsed") {
  document.querySelector("#layout").classList.add("sidebar-collapsed");
  document.querySelector("#toggle-sidebar").textContent = "Show sidebar";
}
document.querySelector("#new-thread").onclick = () => { document.querySelector("#thread-form").hidden = false; document.querySelector("#thread-title").focus(); };
document.querySelector("#cancel-thread").onclick = () => { document.querySelector("#thread-form").hidden = true; };
document.querySelector("#thread-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const participants = document.querySelector("#thread-participants").value.trim().split(/\s+/).filter(Boolean);
    const paths = document.querySelector("#thread-paths").value.trim().split(/\s+/).filter(Boolean);
    const thread = await request("/api/threads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: document.querySelector("#thread-title").value, reason: document.querySelector("#thread-reason").value, participants, paths }) });
    event.target.reset(); event.target.hidden = true;
    await refreshRoom(); openThread(thread.id); clearError();
  } catch (error) { showError(error); }
});
document.querySelector("#close-thread").onclick = async () => {
  try { await request("/api/thread-close", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ thread_id: currentView.id }) }); openRoom(); await refreshRoom(); }
  catch (error) { showError(error); }
};
document.querySelector("#composer").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.querySelector("#message");
  const message = input.value.trim();
  if (!message) return;
  try {
    await request("/api/messages", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, kind: "message", topic: "general", thread_id: currentView.kind === "thread" ? currentView.id : undefined }) });
    input.value = ""; await refreshRoom();
  } catch (error) { showError(error); }
});

refreshRoom();
refreshCatalog();
setInterval(refreshRoom, 1500);
setInterval(refreshCatalog, 15000);
