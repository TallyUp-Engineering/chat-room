const token = new URL(location.href).hash.match(/token=([a-f0-9]+)/)?.[1] || "";
const authHeaders = token ? { "X-Chat-Room-Token": token } : {};
let currentView = { kind: "room", id: "room" };
let roomData = null;
let catalogData = { chats: [] };
let routedAlert = null;
let pendingImages = [];
const renderSignatures = new Map();
let eventsSocket = null;
let eventsUrl = "";
let eventsRetry = null;
let eventsFallback = null;
let refreshQueued = false;
let eventCount = 0;
let alertsExpanded = false;
let connectionFailures = 0;

function changed(key, value) {
  const signature = JSON.stringify(value);
  if (renderSignatures.get(key) === signature) return false;
  renderSignatures.set(key, signature);
  return true;
}

const esc = value => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" })[character]);
const rich = value => esc(value).replace(/(^|\s)([@#][a-z0-9-]+)/gi, '$1<span class="tag">$2</span>');
const time = value => value ? new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
const icon = name => `<svg class="alert-icon" aria-hidden="true"><use href="/icons.svg#${esc(name)}"></use></svg>`;

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
  const items = thread ? data.messages.filter(item => item.metadata?.thread_id === thread.id) : data.messages;
  if (!changed("room-messages", { view: currentView, thread: thread?.updated_at, items: items.map(item => [item.id, item.status, item.message]) })) return;
  const pane = document.querySelector("#messages");
  const stayAtBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 80;
  const previousTop = pane.scrollTop;
  const messages = resetMessages(thread ? `${thread.reason} · ${thread.participants.join(' ') || 'no participants yet'}` : "All Chat Room activity is shown as read. Tag an actor or worktree directly, or open a coordination thread.");
  for (const message of items) {
    const row = document.createElement("div");
    row.className = "msg";
    row.innerHTML = `<div class="meta"><b>${esc(message.sender)}</b>${time(message.timestamp)}</div><div><span class="kind">${esc(message.kind)}</span>${rich(message.message)}</div>`;
    messages.append(row);
  }
  messages.scrollTop = stayAtBottom ? messages.scrollHeight : previousTop;
}

function renderThreads(threads) {
  const container = document.querySelector("#threads");
  document.querySelector("#thread-count").textContent = threads.length;
  if (!changed("threads", { selected: currentView.kind === "thread" ? currentView.id : null, threads })) return;
  const group = (label, lifetime) => {
    const items = threads.filter(thread => thread.lifetime === lifetime);
    if (!items.length) return "";
    return `<div class="channel-group-title">${esc(label)} — ${items.length}</div>${items.map(thread => `<button class="thread-item ${currentView.kind === 'thread' && currentView.id === thread.id ? 'selected' : ''}" data-thread="${esc(thread.id)}"><span class="thread-mark">#</span><span><b>${esc(thread.title)}</b><small>${esc(thread.reason)} · ${thread.participants.length} involved</small></span></button>`).join("")}`;
  };
  container.innerHTML = group("TEAM CHANNELS", "durable") + group("TEMP CHANNELS", "temporary");
  container.querySelectorAll("[data-thread]").forEach(node => { node.onclick = () => openThread(node.dataset.thread); });
}

function renderAlerts(alerts) {
  document.querySelector("#alert-count").textContent = alerts.length;
  if (!changed("alerts", { alerts, alertsExpanded })) return;
  const visible = alertsExpanded ? alerts : alerts.slice(0, 3);
  const more = alerts.length > 3 ? `<button class="more-alerts" data-alert-toggle>${alertsExpanded ? "Show less" : `Show ${alerts.length - 3} more`}</button>` : "";
  document.querySelector("#alerts").innerHTML = visible.map((alert, index) => `<article class="alert-item ${esc(alert.severity)}">${icon(alert.icon)}<div class="alert-copy"><b>${esc(alert.title)}</b><small>${esc(alert.detail)}</small></div><button data-alert="${index}">${alert.thread_id ? 'Open' : 'Route'}</button></article>`).join("") + more || '<p class="nav-empty">No coordination alerts.</p>';
  document.querySelectorAll("[data-alert]").forEach(node => { node.onclick = () => settleAlert(alerts[Number(node.dataset.alert)]); });
  const toggle = document.querySelector("[data-alert-toggle]");
  if (toggle) toggle.onclick = () => { alertsExpanded = !alertsExpanded; renderSignatures.delete("alerts"); renderAlerts(alerts); };
}

async function settleAlert(alert) {
  if (alert.thread_id) { openThread(alert.thread_id); return; }
  routedAlert = alert;
  const actor = document.querySelector("#alert-actor");
  const action = document.querySelector("#alert-action");
  const agents = roomData?.targets?.agents || [];
  actor.innerHTML = [`<option value="@human">Human · @human</option>`, ...agents.map(item => `<option value="${esc(item.target)}">${esc(String(item.role || 'agent').split(':')[0])} · ${esc(item.target)} · ${esc(item.worktree_target)}</option>`)].join("");
  action.innerHTML = [...(roomData?.options?.worktree_action || [])].sort((left, right) => Number(left.metadata?.order || 999) - Number(right.metadata?.order || 999)).map(item => `<option value="${esc(item.key)}">${esc(item.value)}</option>`).join("");
  document.querySelector("#alert-router-title").textContent = alert.title;
  document.querySelector("#alert-router").hidden = false;
  actor.focus();
}

function renderSnapshot(data) {
  roomData = data;
  document.querySelector("#room-title").textContent = data.status.display_name || data.status.room_id;
  document.querySelector("#authority").textContent = `${data.status.project_identity} • advisory-only`;
  document.querySelector("#counts").textContent = `${data.messages.length} messages • ${data.status.members_online} active`;
  document.querySelector("#combined-count").textContent = data.messages.length;
  renderAlerts(data.alerts || []);
  renderThreads(data.threads || []);
  const online = data.targets.agents.filter(agent => agent.state === "online");
  const idle = data.targets.agents.filter(agent => agent.state === "idle");
  const offline = data.targets.agents.filter(agent => agent.state === "offline");
  if (changed("active-agents", online)) document.querySelector("#active-agents").innerHTML = online.length ? online.map(agent => `<button class="active-agent" data-target="${esc(agent.target)}"><span class="dot"></span><b>${esc(agent.target)}</b><small>${esc(String(agent.role || 'agent').split(':')[0])}</small></button>`).join('') : '<p class="nav-empty">No active agents in this room.</p>';
  if (changed("members", { online, idle, offline })) document.querySelector("#members").innerHTML = `<div class="group">⌄ Online — ${online.length}</div>${online.map(buddy).join('')}<div class="group">⌄ Idle — ${idle.length}</div>${idle.map(buddy).join('')}<div class="group">⌄ Offline — ${offline.length}</div>${offline.map(buddy).join('')}`;
  attachTargetHandlers();
  renderRouting();
  connectEvents(data.events_url);
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
  document.querySelector("#close-thread").textContent = activeThread?.lifetime === "temporary" ? "Resolve" : "Archive";
  document.querySelector("#rename-view").hidden = false;
  document.querySelector("#compose-label").textContent = activeThread ? `thread ${activeThread.id} · routes to every participant` : "message as @human · ad hoc tagging is built in";
  document.querySelector("#room-routing").hidden = false;
  document.querySelector("#message").disabled = false;
  document.querySelector("#send-message").disabled = false;
  document.querySelector("#attach-image").disabled = true;
  document.querySelector("#message").placeholder = activeThread ? "Message this coordination room…" : "Message the combined room. Tag an active @actor or #worktree.";
  document.querySelector("#combined-room").classList.toggle("active", !activeThread);
  renderRouting();
  renderThreads(roomData.threads || []);
  renderRoomMessages(roomData);
}

function openRoom() {
  clearImages();
  currentView = { kind: "room", id: "room" };
  renderRoomView();
}

function openThread(threadId) {
  clearImages();
  currentView = { kind: "thread", id: threadId };
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
  document.querySelector("#view-status").textContent = data.delivery.mode === "running" ? "● turn running" : data.delivery.ready ? "● synced locally" : "● history mirror";
  document.querySelector("#close-thread").hidden = true;
  document.querySelector("#rename-view").hidden = false;
  document.querySelector("#combined-room").classList.remove("active");
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
  messages.innerHTML = `<p class="history-notice">${esc(`Synced from the local CLI transcript. Tool calls, hidden instructions, and reasoning stay hidden. ${data.delivery.detail}`)}</p>`;
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
document.querySelector("#new-thread").onclick = () => { document.querySelector("#thread-form").hidden = false; document.querySelector("#thread-title").focus(); };
document.querySelector("#cancel-thread").onclick = () => { document.querySelector("#thread-form").hidden = true; };
document.querySelector("#cancel-alert").onclick = () => { document.querySelector("#alert-router").hidden = true; routedAlert = null; };
document.querySelector("#alert-router").addEventListener("submit", async event => {
  event.preventDefault();
  if (!routedAlert) return;
  try {
    const thread = await request("/api/route-alert", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: routedAlert.title, alert_type: routedAlert.type, actor: document.querySelector("#alert-actor").value, action: document.querySelector("#alert-action").value, participants: routedAlert.participants, paths: routedAlert.paths }) });
    event.target.hidden = true; routedAlert = null;
    await refreshRoom(); openThread(thread.id); clearError();
  } catch (error) { showError(error); }
});
document.querySelector("#thread-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const participants = document.querySelector("#thread-participants").value.trim().split(/\s+/).filter(Boolean);
    const paths = document.querySelector("#thread-paths").value.trim().split(/\s+/).filter(Boolean);
    const thread = await request("/api/threads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: document.querySelector("#thread-title").value, reason: document.querySelector("#thread-reason").value, lifetime: document.querySelector("#thread-lifetime").value, participants, paths }) });
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
      input.value = ""; await refreshRoom();
    }
  } catch (error) { showError(error); }
});

function renderRouting() {
  if (!roomData) return;
  const scope = document.querySelector("#send-scope");
  const target = document.querySelector("#send-target");
  const thread = currentView.kind === "thread" ? roomData.threads.find(item => item.id === currentView.id) : null;
  scope.options[0].textContent = thread ? `Channel · ${thread.title}` : "Channel · All activity";
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
