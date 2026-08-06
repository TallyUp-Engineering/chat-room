const token = new URL(location.href).hash.match(/token=([a-f0-9]+)/)?.[1] || "";
const authHeaders = token ? { "X-Chat-Room-Token": token } : {};
let currentView = { kind: "room", id: "room" };
let roomData = null;
let catalogData = { chats: [] };
let pendingImages = [];
const renderSignatures = new Map();
let eventsSocket = null;
let eventsUrl = "";
let eventsRetry = null;
let eventsFallback = null;
let refreshQueued = false;
let eventCount = 0;
let connectionFailures = 0;
let consoleComposerActive = false;

function changed(key, value) {
  const signature = JSON.stringify(value);
  if (renderSignatures.get(key) === signature) return false;
  renderSignatures.set(key, signature);
  return true;
}

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
  connectionFailures += 1;
  if (connectionFailures < 2) return;
  const item = document.querySelector("#error");
  item.textContent = error.message === "Failed to fetch" ? "Reconnecting to the local service…" : error.message;
  item.hidden = false;
}

function clearError() { connectionFailures = 0; document.querySelector("#error").hidden = true; }

function clearImages() {
  pendingImages.forEach(item => URL.revokeObjectURL(item.preview));
  pendingImages = [];
  renderImages();
}

function renderImages() {
  const container = document.querySelector("#attachments");
  container.hidden = pendingImages.length === 0;
  container.innerHTML = pendingImages.map((item, index) => `<span class="attachment"><img src="${esc(item.preview)}" alt=""><b>${esc(item.file.name || `image ${index + 1}`)}</b><button type="button" data-remove-image="${index}" aria-label="Remove ${esc(item.file.name || 'image')}">×</button></span>`).join("");
  container.querySelectorAll("[data-remove-image]").forEach(node => { node.onclick = () => { const [removed] = pendingImages.splice(Number(node.dataset.removeImage), 1); if (removed) URL.revokeObjectURL(removed.preview); renderImages(); }; });
}

function addImages(files) {
  const allowed = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
  for (const file of files) {
    if (!allowed.has(file.type) || file.size > 10 * 1024 * 1024 || pendingImages.length >= 5) continue;
    pendingImages.push({ file, preview: URL.createObjectURL(file) });
  }
  renderImages();
}

function fileData(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, type: file.type, data: reader.result });
    reader.onerror = () => reject(reader.error || new Error("Could not read pasted image"));
    reader.readAsDataURL(file);
  });
}

function targetInput(target) {
  const input = document.querySelector("#message");
  const present = new Set((input.value.match(/[@#][a-z0-9-]+/gi) || []).map(value => value.toLowerCase()));
  if (!present.has(target.toLowerCase())) input.value = `${target} ${input.value}`.trimStart();
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
  const items = thread ? data.messages.filter(item => item.metadata?.thread_id === thread.id) : [];
  if (!changed("room-messages", { view: currentView, thread: thread?.updated_at, items: items.map(item => [item.id, item.status, item.message]) })) return;
  const pane = document.querySelector("#messages");
  const stayAtBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 80;
  const previousTop = pane.scrollTop;
  const messages = resetMessages(`${thread.audience === 'human-loop' ? 'Human in the Loop' : 'Agent-only chatter'} · ${thread.reason} · from ${thread.origin}`);
  for (const message of items) {
    const row = document.createElement("div");
    row.className = "msg";
    row.innerHTML = `<div class="meta"><b>${esc(message.sender)}</b>${time(message.timestamp)}</div><div><span class="kind">${esc(message.kind)}</span>${rich(message.message)}</div>`;
    messages.append(row);
  }
  messages.scrollTop = stayAtBottom ? messages.scrollHeight : previousTop;
}

function renderRoomLog(data) {
  const items = data.messages;
  if (!changed("room-log", items.map(item => [item.id, item.status, item.message]))) return;
  const messages = resetMessages("Room log · opened deliberately · all activity is shown as read");
  for (const message of items) {
    const row = document.createElement("div");
    row.className = "msg";
    row.innerHTML = `<div class="meta"><b>${esc(message.sender)}</b>${time(message.timestamp)}</div><div><span class="kind">${esc(message.kind)}</span>${rich(message.message)}</div>`;
    messages.append(row);
  }
  messages.scrollTop = messages.scrollHeight;
}

function activateConsole(scope) {
  consoleComposerActive = true;
  const composer = document.querySelector("#composer");
  composer.hidden = false;
  document.querySelector("#send-scope").value = scope;
  renderRouting();
  document.querySelector("#message").focus();
}

function renderConsoleLanding(data) {
  const agents = data.targets.agents || [];
  const online = agents.filter(agent => agent.state === "online");
  const idle = agents.filter(agent => agent.state === "idle");
  const questions = data.threads.filter(thread => thread.audience === "human-loop").length;
  const chatter = data.threads.filter(thread => thread.audience !== "human-loop").length;
  const shared = (data.alerts || []).filter(alert => alert.type === "shared-worktree").length;
  const signature = { online: online.map(agent => agent.target), idle: idle.map(agent => agent.target), questions, chatter, shared };
  if (!changed("console-landing", signature)) return;
  const messages = document.querySelector("#messages");
  messages.innerHTML = `<section class="activation-panel"><span class="activation-state"><i></i> READY</span><h1>What do you want to activate?</h1><p>${online.length} active worker${online.length === 1 ? '' : 's'}${idle.length ? ` · ${idle.length} idle` : ''}. Nothing is sent until you choose a route.</p><div class="activation-grid"><button type="button" data-console-action="all"${online.length ? '' : ' disabled'}><b>Message everyone</b><small>Send one room message to all active workers.</small></button><button type="button" data-console-action="tag"${online.length ? '' : ' disabled'}><b>Tag a worker</b><small>Choose one active Codex or Claude session.</small></button><button type="button" data-console-action="human"><b>Ask a human question</b><small>Open a durable question with its reason and source.</small></button><button type="button" data-console-action="chatter"><b>Start agent chatter</b><small>Activate a focused agent-only coordination thread.</small></button></div><div class="activation-foot"><span>${questions} human question${questions === 1 ? '' : 's'} · ${chatter} chatter thread${chatter === 1 ? '' : 's'}${shared ? ` · ${shared} shared-worktree signal${shared === 1 ? '' : 's'}` : ''}</span><button type="button" data-console-action="chats">Choose a chat</button><button type="button" data-console-action="log">View room log</button></div></section>`;
  messages.querySelector('[data-console-action="all"]').onclick = () => activateConsole("all");
  messages.querySelector('[data-console-action="tag"]').onclick = () => activateConsole("tag");
  messages.querySelector('[data-console-action="human"]').onclick = () => document.querySelector("#new-human-thread").click();
  messages.querySelector('[data-console-action="chatter"]').onclick = () => document.querySelector("#new-thread").click();
  messages.querySelector('[data-console-action="chats"]').onclick = () => { document.querySelector("#chats-section").open = true; document.querySelector("#chat-filter").focus(); };
  messages.querySelector('[data-console-action="log"]').onclick = openRoomLog;
}

function renderThreads(threads) {
  const container = document.querySelector("#threads");
  const chatter = threads.filter(thread => thread.audience !== "human-loop");
  document.querySelector("#thread-count").textContent = chatter.length;
  if (!changed("threads", { selected: currentView.kind === "thread" ? currentView.id : null, chatter })) return;
  const group = (label, lifetime) => {
    const items = chatter.filter(thread => thread.lifetime === lifetime);
    if (!items.length) return "";
    const rows = items.map(thread => `<button class="thread-item ${currentView.kind === 'thread' && currentView.id === thread.id ? 'selected' : ''}" data-thread="${esc(thread.id)}"><span class="thread-mark">#</span><span><b>${esc(thread.title)}</b><small>${esc(thread.reason)} · ${thread.participants.length} participants · from ${esc(thread.origin)}</small></span></button>`).join("");
    return lifetime === "temporary" ? `<details class="channel-fold"><summary>${esc(label)} — ${items.length}</summary>${rows}</details>` : `<div class="channel-group-title">${esc(label)} — ${items.length}</div>${rows}`;
  };
  container.innerHTML = group("TEAM CHATTER", "durable") + group("TEMP CHATTER", "temporary") || '<p class="nav-empty">No active agent chatter.</p>';
  container.querySelectorAll("[data-thread]").forEach(node => { node.onclick = () => openThread(node.dataset.thread); });
}

function renderHumanThreads(threads) {
  const questions = threads.filter(thread => thread.audience === "human-loop");
  document.querySelector("#human-count").textContent = questions.length;
  if (!changed("human-threads", { selected: currentView.kind === "thread" ? currentView.id : null, questions })) return;
  const container = document.querySelector("#human-threads");
  container.innerHTML = questions.map(thread => `<button class="human-thread ${currentView.kind === 'thread' && currentView.id === thread.id ? 'selected' : ''}" data-thread="${esc(thread.id)}"><span class="human-mark">?</span><span><b>${esc(thread.title)}</b><small>${esc(thread.reason)} · from ${esc(thread.origin)}</small></span><em>OPEN</em></button>`).join("") || '<p class="nav-empty">No open questions.</p>';
  container.querySelectorAll("[data-thread]").forEach(node => { node.onclick = () => openThread(node.dataset.thread); });
}

function activationSuggestions(alerts) {
  const grouped = new Map();
  for (const alert of alerts) {
    if (alert.type !== "shared-worktree" && alert.type !== "file-overlap") continue;
    const participants = (alert.participants || []).filter(target => target !== "@human").sort();
    const key = `${alert.type}:${participants.join("|")}`;
    const item = grouped.get(key) || { type: alert.type, participants, paths: [], alerts: [], title: alert.title };
    item.paths.push(...(alert.paths || [])); item.alerts.push(alert); grouped.set(key, item);
  }
  return [...grouped.values()].map(item => ({ ...item, paths: [...new Set(item.paths)], title: item.type === "file-overlap" && item.paths.length > 1 ? `${item.paths.length} overlapping files` : item.title, reason: item.type === "file-overlap" ? "preemptive conflict" : "shared worktree" }));
}

function renderSuggestions(alerts) {
  const suggestions = activationSuggestions(alerts);
  if (!changed("suggestions", suggestions)) return;
  const container = document.querySelector("#suggestions");
  container.innerHTML = suggestions.map((item, index) => `<div class="suggestion"><span class="suggestion-dot"></span><span><b>${esc(item.title)}</b><small>${esc(item.reason)} · ${item.participants.length} participants${item.paths.length ? ` · ${item.paths.length} paths` : ''}</small></span><button type="button" data-suggestion="${index}" aria-label="Start chatter">＋</button></div>`).join("") || '<p class="nav-empty">No chatter suggested.</p>';
  container.querySelectorAll("[data-suggestion]").forEach(node => { node.onclick = () => prepareChatter(suggestions[Number(node.dataset.suggestion)]); });
}

function prepareChatter(suggestion) {
  document.querySelector("#thread-title").value = suggestion.title;
  document.querySelector("#thread-reason").value = suggestion.reason;
  document.querySelector("#thread-origin").value = "observed activity";
  document.querySelector("#thread-lifetime").value = "temporary";
  document.querySelector("#thread-participants").value = suggestion.participants.join(" ");
  document.querySelector("#thread-paths").value = suggestion.paths.join(" ");
  document.querySelector("#thread-form").hidden = false;
  document.querySelector("#thread-title").focus();
}

function renderSnapshot(data) {
  roomData = data;
  document.querySelector("#room-title").textContent = data.status.display_name || data.status.room_id;
  document.querySelector("#authority").textContent = `${data.status.project_identity} • advisory-only`;
  renderSuggestions(data.alerts || []);
  renderThreads(data.threads || []);
  renderHumanThreads(data.threads || []);
  const online = data.targets.agents.filter(agent => agent.state === "online");
  const idle = data.targets.agents.filter(agent => agent.state === "idle");
  const offline = data.targets.agents.filter(agent => agent.state === "offline");
  document.querySelector("#combined-count").textContent = online.length;
  document.querySelector("#counts").textContent = `${online.length} active • ${idle.length} idle • ${data.threads.length} open threads`;
  if (changed("active-agents", online)) document.querySelector("#active-agents").innerHTML = online.length ? online.map(agent => `<button class="active-agent" data-target="${esc(agent.target)}"><span class="dot"></span><b>${esc(agent.target)}</b><small>${esc(String(agent.role || 'agent').split(':')[0])}</small></button>`).join('') : '<p class="nav-empty">No active agents in this room.</p>';
  if (changed("members", { online, idle, offline })) document.querySelector("#members").innerHTML = `<div class="group">⌄ Online — ${online.length}</div>${online.map(buddy).join('')}<div class="group">⌄ Idle — ${idle.length}</div>${idle.map(buddy).join('')}<div class="group">⌄ Offline — ${offline.length}</div>${offline.map(buddy).join('')}`;
  attachTargetHandlers();
  renderRouting();
  connectEvents(data.events_url);
  if (currentView.kind === "room" || currentView.kind === "thread" || currentView.kind === "room-log") renderRoomView();
}

function renderRoomView() {
  if (!roomData) return;
  if (currentView.kind === "room-log") {
    document.querySelector("#chat-pane").classList.remove("history-mode");
    document.querySelector("#view-title").textContent = "Room Log";
    document.querySelector("#identity").textContent = `${roomData.status.project_identity} · opened deliberately`;
    document.querySelector("#view-status").textContent = "● read-only log";
    document.querySelector("#close-thread").hidden = true;
    document.querySelector("#rename-view").hidden = true;
    document.querySelector("#composer").hidden = true;
    document.querySelector("#combined-room").classList.remove("active");
    renderRoomLog(roomData);
    return;
  }
  const thread = currentView.kind === "thread" ? roomData.threads.find(item => item.id === currentView.id) : null;
  if (currentView.kind === "thread" && !thread) currentView = { kind: "room", id: "room" };
  const activeThread = currentView.kind === "thread" ? roomData.threads.find(item => item.id === currentView.id) : null;
  document.querySelector("#chat-pane").classList.remove("history-mode");
  const agentOnly = activeThread?.audience === "agents";
  document.querySelector("#view-title").textContent = activeThread?.title || "Command Console";
  document.querySelector("#identity").textContent = activeThread ? `${activeThread.audience === 'human-loop' ? 'human in the loop' : 'agent-only chatter'} · ${activeThread.reason} · ${activeThread.id}` : `${roomData.status.project_identity} · choose an action`;
  document.querySelector("#view-status").textContent = activeThread ? "● connected locally" : "● ready";
  document.querySelector("#close-thread").hidden = !activeThread;
  document.querySelector("#close-thread").textContent = activeThread?.lifetime === "temporary" ? "Resolve" : "Archive";
  document.querySelector("#rename-view").hidden = false;
  document.querySelector("#compose-label").textContent = agentOnly ? "agent-only chatter · visible to @human" : activeThread ? `human question · response returns to ${activeThread.origin}` : "message as @human · route to all or tag one worker";
  document.querySelector("#composer").hidden = activeThread ? agentOnly : !consoleComposerActive;
  document.querySelector("#room-routing").hidden = Boolean(activeThread);
  document.querySelector("#message").disabled = agentOnly;
  document.querySelector("#send-message").disabled = agentOnly;
  document.querySelector("#attach-image").disabled = true;
  document.querySelector("#message").placeholder = agentOnly ? "Agent-only chatter is read-only here" : activeThread ? "Answer this question…" : "Message the command console. Tag an active @actor or #worktree.";
  document.querySelector("#combined-room").classList.toggle("active", !activeThread);
  renderRouting();
  renderThreads(roomData.threads || []);
  renderHumanThreads(roomData.threads || []);
  if (activeThread) renderRoomMessages(roomData); else renderConsoleLanding(roomData);
}

function openRoom() {
  clearImages();
  consoleComposerActive = false;
  currentView = { kind: "room", id: "room" };
  renderSignatures.delete("console-landing");
  renderRoomView();
}

function openThread(threadId) {
  clearImages();
  consoleComposerActive = false;
  currentView = { kind: "thread", id: threadId };
  renderRoomView();
}

function openRoomLog() {
  clearImages();
  consoleComposerActive = false;
  currentView = { kind: "room-log", id: "room-log" };
  renderSignatures.delete("room-log");
  renderRoomView();
}

function renderCatalog(data) {
  catalogData = data;
  const query = document.querySelector("#chat-filter").value.trim().toLowerCase();
  const statusFilter = document.querySelector("#chat-status-filter").value;
  const liveSessions = new Set((roomData?.targets?.agents || []).map(agent => agent.session_id).filter(Boolean));
  const state = chat => liveSessions.has(chat.id) ? "live" : chat.recency;
  const visibleChats = data.chats.filter(chat => (!query || `${chat.client} ${chat.title} ${chat.worktree}`.toLowerCase().includes(query)) && (statusFilter === "all" || state(chat) === statusFilter));
  const groups = new Map();
  for (const chat of visibleChats) {
    if (!groups.has(chat.client)) groups.set(chat.client, []);
    groups.get(chat.client).push(chat);
  }
  document.querySelector("#chat-count").textContent = query ? `${visibleChats.length}/${data.chats.length}` : data.chats.length;
  if (!changed("catalog", { selected: currentView.kind === "history" ? currentView.id : null, query, statusFilter, visibleChats })) return;
  document.querySelector("#chats").innerHTML = [...groups.entries()].map(([client, chats]) => `<section class="chat-group"><div class="chat-group-title">${esc(client)} — ${chats.length}</div>${chats.map(chat => `<div class="history-row"><button class="history-item ${currentView.kind === 'history' && currentView.id === chat.id ? 'selected' : ''}" data-client="${esc(chat.client)}" data-chat="${esc(chat.id)}"><span class="history-mark">${esc(chat.client.slice(0, 1))}</span><span><b>${esc(chat.title)}</b><small>${esc(chat.worktree)} · ${time(chat.updated_at)}</small></span><em class="activity-badge ${state(chat)}">${state(chat)}</em></button><button class="history-action" data-client="${esc(chat.client)}" data-chat="${esc(chat.id)}">${state(chat) === 'inactive' || state(chat) === 'stale' ? 'Review' : 'Open'}</button></div>`).join('')}</section>`).join('') || '<p class="nav-empty">No matching local chat histories.</p>';
  document.querySelectorAll("[data-chat]").forEach(node => { node.onclick = () => openHistory(node.dataset.client, node.dataset.chat); });
}

function openInactivePanel() {
  clearImages();
  consoleComposerActive = false;
  const liveSessions = new Set((roomData?.targets?.agents || []).map(agent => agent.session_id).filter(Boolean));
  const inactive = catalogData.chats.filter(chat => !liveSessions.has(chat.id) && chat.recency === "inactive");
  currentView = { kind: "inactive", id: "inactive" };
  document.querySelector("#chat-pane").classList.add("history-mode");
  document.querySelector("#view-title").textContent = `Inactive chats — ${inactive.length}`;
  document.querySelector("#identity").textContent = "No live session and no activity for at least 30 days";
  document.querySelector("#view-status").textContent = "● review queue";
  document.querySelector("#close-thread").hidden = true;
  document.querySelector("#rename-view").hidden = true;
  document.querySelector("#combined-room").classList.remove("active");
  document.querySelector("#composer").hidden = true;
  document.querySelector("#room-routing").hidden = true;
  document.querySelector("#message").disabled = true;
  document.querySelector("#send-message").disabled = true;
  document.querySelector("#attach-image").disabled = true;
  document.querySelector("#room-routing").hidden = true;
  const messages = resetMessages("Live means attached now. Recent means under 7 days. Stale means 7–29 days. Inactive means 30+ days without a live session. Review does not delete vendor-owned history.");
  for (const chat of inactive) {
    const card = document.createElement("div");
    card.className = "inactive-card";
    card.innerHTML = `<span class="history-mark">${esc(chat.client.slice(0, 1))}</span><div><b>${esc(chat.title)}</b><small>${esc(chat.client)} · ${esc(chat.worktree)} · ${time(chat.updated_at)}</small></div><button data-client="${esc(chat.client)}" data-chat="${esc(chat.id)}">Review</button>`;
    messages.append(card);
  }
  messages.querySelectorAll("[data-chat]").forEach(node => { node.onclick = () => openHistory(node.dataset.client, node.dataset.chat); });
}

async function openHistory(client, sessionId) {
  try {
    const data = await request(`/api/chat?client=${encodeURIComponent(client)}&id=${encodeURIComponent(sessionId)}`);
    currentView = { kind: "history", id: sessionId, client };
    consoleComposerActive = false;
    clearImages();
    renderSignatures.delete("history");
    renderHistory(data);
    await refreshCatalog();
    clearError();
  } catch (error) { showError(error); }
}

function renderHistory(data) {
  if (currentView.kind !== "history" || currentView.id !== data.chat.id) return;
  const pane = document.querySelector("#messages");
  const stayAtBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 80;
  document.querySelector("#chat-pane").classList.add("history-mode");
  document.querySelector("#view-title").textContent = data.chat.title;
  document.querySelector("#identity").textContent = `${data.chat.client} · ${data.chat.worktree} · ${data.delivery.label}`;
  document.querySelector("#view-status").textContent = data.delivery.mode === "running" ? "● responding" : data.delivery.mode === "active-unattached" ? "● active elsewhere" : data.delivery.ready ? "● ready" : "● view only";
  document.querySelector("#close-thread").hidden = true;
  document.querySelector("#rename-view").hidden = false;
  document.querySelector("#combined-room").classList.remove("active");
  document.querySelector("#composer").hidden = false;
  const input = document.querySelector("#message");
  const send = document.querySelector("#send-message");
  input.disabled = !data.delivery.ready;
  send.disabled = !data.delivery.ready;
  document.querySelector("#attach-image").disabled = !data.delivery.ready;
  input.placeholder = data.delivery.ready ? `Continue this ${data.chat.client} chat…` : data.delivery.label;
  document.querySelector("#compose-label").textContent = data.delivery.detail;
  const signature = { title: data.chat.title, delivery: data.delivery, messages: data.messages.map(item => [item.role, item.timestamp, item.body]) };
  if (!changed("history", signature)) return;
  const previousTop = pane.scrollTop;
  const messages = pane;
  messages.innerHTML = `<p class="history-notice">${esc(`Synced from local conversation history. Tool calls, hidden instructions, and reasoning stay hidden. ${data.delivery.detail}`)}</p>`;
  for (const message of data.messages) {
    const row = document.createElement("div");
    row.className = "msg";
    row.innerHTML = `<div class="meta"><b>${message.role === 'user' ? '@human' : esc(data.chat.client)}</b>${time(message.timestamp)}</div><div>${rich(message.body)}</div>`;
    messages.append(row);
  }
  messages.scrollTop = stayAtBottom ? messages.scrollHeight : previousTop;
}

async function refreshCurrentHistory() {
  if (currentView.kind !== "history") return;
  try { renderHistory(await request(`/api/chat?client=${encodeURIComponent(currentView.client)}&id=${encodeURIComponent(currentView.id)}`)); clearError(); }
  catch (error) { showError(error); }
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
document.querySelector("#chat-status-filter").onchange = () => renderCatalog(catalogData);
document.querySelector("#list-inactive").onclick = openInactivePanel;
document.querySelector("#attach-image").onclick = () => document.querySelector("#image-input").click();
document.querySelector("#send-scope").onchange = renderRouting;
document.querySelector("#image-input").onchange = event => { addImages(event.target.files || []); event.target.value = ""; };
document.querySelector("#message").addEventListener("paste", event => {
  if (currentView.kind !== "history") return;
  const files = [...(event.clipboardData?.items || [])].filter(item => item.kind === "file").map(item => item.getAsFile()).filter(Boolean);
  if (files.length) addImages(files);
});
document.querySelector("#message").addEventListener("dragover", event => { if (currentView.kind === "history") event.preventDefault(); });
document.querySelector("#message").addEventListener("drop", event => { if (currentView.kind === "history") { event.preventDefault(); addImages(event.dataTransfer?.files || []); } });
document.querySelector("#toggle-sidebar").onclick = () => {
  const collapsed = document.querySelector("#layout").classList.toggle("sidebar-collapsed");
  document.querySelector("#toggle-sidebar").textContent = collapsed ? "Show sidebar" : "Hide sidebar";
  localStorage.setItem("chat-room-sidebar", collapsed ? "collapsed" : "open");
};
if (localStorage.getItem("chat-room-sidebar") === "collapsed") {
  document.querySelector("#layout").classList.add("sidebar-collapsed");
  document.querySelector("#toggle-sidebar").textContent = "Show sidebar";
}
document.querySelector("#new-thread").onclick = () => { consoleComposerActive = false; document.querySelector("#composer").hidden = true; document.querySelector("#thread-form").hidden = false; document.querySelector("#thread-title").focus(); };
document.querySelector("#cancel-thread").onclick = () => { document.querySelector("#thread-form").hidden = true; };
document.querySelector("#new-human-thread").onclick = () => { consoleComposerActive = false; document.querySelector("#composer").hidden = true; document.querySelector("#human-thread-form").hidden = false; document.querySelector("#human-thread-title").focus(); };
document.querySelector("#cancel-human-thread").onclick = () => { document.querySelector("#human-thread-form").hidden = true; };
document.querySelector("#human-thread-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const participants = document.querySelector("#human-thread-participants").value.trim().split(/\s+/).filter(Boolean);
    const paths = document.querySelector("#human-thread-paths").value.trim().split(/\s+/).filter(Boolean);
    const thread = await request("/api/threads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: document.querySelector("#human-thread-title").value, reason: document.querySelector("#human-thread-reason").value, lifetime: "durable", audience: "human-loop", origin: "human question", participants, paths }) });
    event.target.reset(); event.target.hidden = true;
    await refreshRoom(); openThread(thread.id); clearError();
  } catch (error) { showError(error); }
});
document.querySelector("#thread-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const participants = document.querySelector("#thread-participants").value.trim().split(/\s+/).filter(Boolean);
    const paths = document.querySelector("#thread-paths").value.trim().split(/\s+/).filter(Boolean);
    const thread = await request("/api/threads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: document.querySelector("#thread-title").value, reason: document.querySelector("#thread-reason").value, origin: document.querySelector("#thread-origin").value, lifetime: document.querySelector("#thread-lifetime").value, audience: "agents", participants, paths }) });
    event.target.reset(); event.target.hidden = true;
    await refreshRoom(); openThread(thread.id); clearError();
  } catch (error) { showError(error); }
});
document.querySelector("#close-thread").onclick = async () => {
  try { await request("/api/thread-close", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ thread_id: currentView.id }) }); openRoom(); await refreshRoom(); }
  catch (error) { showError(error); }
};
document.querySelector("#rename-view").onclick = async () => {
  const current = currentView.kind === "history" || currentView.kind === "thread" ? document.querySelector("#view-title").textContent : roomData.status.display_name;
  const prompt = currentView.kind === "history" ? "Rename this local chat label" : currentView.kind === "thread" ? "Rename this channel" : "Rename this Chat Room";
  const label = window.prompt(prompt, current);
  if (!label || label.trim() === current) return;
  try {
    const kind = currentView.kind === "history" ? "chat" : currentView.kind === "thread" ? "channel" : "room";
    await request("/api/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind, reference: kind === "room" ? roomData.status.room_id : currentView.id, client: currentView.client || "", label: label.trim() }) });
    await Promise.all([refreshRoom(), refreshCatalog()]);
    if (currentView.kind === "history") await refreshCurrentHistory(); else renderRoomView();
  } catch (error) { showError(error); }
};
document.querySelector("#composer").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.querySelector("#message");
  const message = input.value.trim();
  if (!message && pendingImages.length === 0) return;
  try {
    if (currentView.kind === "history") {
      const attachments = await Promise.all(pendingImages.map(item => fileData(item.file)));
      await request("/api/chat-send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, attachments, client: currentView.client, session_id: currentView.id }) });
      input.value = ""; clearImages(); await refreshCurrentHistory();
    } else {
      const scope = document.querySelector("#send-scope").value;
      const recipients = scope === "all" ? (roomData?.targets?.agents || []).map(agent => agent.target) : scope === "tag" ? [document.querySelector("#send-target").value].filter(Boolean) : [];
      await request("/api/messages", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, recipients, kind: "message", topic: scope === "channel" ? "general" : scope, thread_id: scope === "channel" && currentView.kind === "thread" ? currentView.id : undefined }) });
      input.value = ""; consoleComposerActive = false; renderSignatures.delete("console-landing"); await refreshRoom();
    }
  } catch (error) { showError(error); }
});

function renderRouting() {
  if (!roomData) return;
  const scope = document.querySelector("#send-scope");
  const target = document.querySelector("#send-target");
  const thread = currentView.kind === "thread" ? roomData.threads.find(item => item.id === currentView.id) : null;
  scope.options[0].textContent = thread ? `Thread · ${thread.title}` : "Console · all activity";
  const agents = roomData.targets?.agents || [];
  const selected = target.value;
  if (changed("routing-agents", agents.map(agent => [agent.target, agent.worktree_target]))) target.innerHTML = agents.map(agent => `<option value="${esc(agent.target)}">${esc(agent.target)} · ${esc(agent.worktree_target)}</option>`).join("");
  if (agents.some(agent => agent.target === selected)) target.value = selected;
  if ((scope.value === "all" || scope.value === "tag") && !agents.length) scope.value = "channel";
  target.hidden = scope.value !== "tag";
}

function queueRefresh(includeCatalog = false) {
  eventCount += 1;
  if (refreshQueued) return;
  refreshQueued = true;
  setTimeout(async () => {
    refreshQueued = false;
    await Promise.all([refreshRoom(), refreshCurrentHistory(), ...(includeCatalog || eventCount % 10 === 0 ? [refreshCatalog()] : [])]);
  }, 40);
}

function connectEvents(url) {
  if (!url || (eventsSocket && eventsUrl === url && eventsSocket.readyState < 2)) return;
  eventsUrl = url;
  if (eventsSocket) eventsSocket.close();
  clearTimeout(eventsRetry);
  try { eventsSocket = new WebSocket(url); }
  catch { eventsSocket = null; }
  if (!eventsSocket) return;
  eventsSocket.onopen = () => { clearInterval(eventsFallback); eventsFallback = null; };
  eventsSocket.onmessage = event => {
    let payload = {};
    try { payload = JSON.parse(event.data); } catch { /* reconciliation still runs */ }
    queueRefresh(payload.type === "workspace.changed");
  };
  eventsSocket.onclose = () => {
    eventsSocket = null;
    if (!eventsFallback) eventsFallback = setInterval(() => queueRefresh(true), 3000);
    eventsRetry = setTimeout(() => connectEvents(eventsUrl), 1200);
  };
}

refreshRoom();
refreshCatalog();
