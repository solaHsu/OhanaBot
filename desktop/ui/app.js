"use strict";

const element = (id) => document.getElementById(id);
const tauri = window.__TAURI__?.core;
const apps = {
  qq: { name: "QQ", icon: "Q", placeholder: "例如 123456789" },
  feishu: { name: "飞书", icon: "飞", placeholder: "例如 oc_群聊ID" },
};
const names = { chat: "主对话", activity: "活动记录", qq: "QQ", feishu: "飞书" };
const dateFormat = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const editor = element("chat-input");
const groups = element("app-groups");
let workspace = null;
let page = "chat";
let filter = "all";
let busy = false;
let pendingMessage = "";

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

async function request(action, payload = {}) {
  const result = tauri
    ? await tauri.invoke("desktop_request", { action, payload })
    : await previewRequest(action, payload);
  if (result.error) throw new Error(result.error);
  return result.data;
}

async function previewRequest(action, payload) {
  const response = await fetch("/api/desktop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, payload }),
  });
  if (!response.ok) throw new Error("无法连接本地预览服务");
  return response.json();
}

function showNotice(text) {
  const notice = element("notice");
  notice.hidden = !text;
  notice.textContent = text || "";
}

function updateControls() {
  for (const button of document.querySelectorAll("[data-operation]")) {
    button.disabled = busy || !workspace;
  }
  element("send").disabled = busy || !workspace || !editor.value.trim();
}

async function perform(action, payload, errorTarget = showNotice) {
  if (busy) return false;
  busy = true;
  showNotice("");
  updateControls();
  try {
    workspace = await request(action, payload);
    showNotice(workspace.notice);
    return true;
  } catch (error) {
    errorTarget(error.message || String(error));
    return false;
  } finally {
    busy = false;
    if (action === "send") pendingMessage = "";
    render();
    updateControls();
  }
}

function selectPage(next) {
  page = next;
  for (const button of document.querySelectorAll(".nav-item")) {
    if (button.dataset.page === page) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  element("section-name").textContent = apps[page] ? "外部应用" : "工作台";
  element("page-name").textContent = names[page];
  element("page-chat").hidden = page !== "chat";
  element("page-activity").hidden = page !== "activity";
  element("page-app").hidden = !apps[page];
  if (apps[page]) loadAppForm();
  if (page === "chat") scrollToEnd();
}

function groupLines() {
  return [...new Set(groups.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean))];
}

function updateGroupCount() {
  element("group-count").textContent = `${groupLines().length} 个群`;
}

function loadAppForm() {
  const app = apps[page];
  const config = workspace?.apps[page];
  const icon = element("app-icon");
  icon.className = `app-mark large ${page}-mark`;
  icon.textContent = app.icon;
  element("app-title").textContent = app.name;
  element("app-description").textContent = `管理 Ohana 的${app.name}群聊和私聊配置。`;
  groups.placeholder = app.placeholder;
  groups.value = config?.groups.join("\n") || "";
  element("allow-private").checked = config?.accept_private || false;
  element("settings-note").textContent = "";
  updateGroupCount();
}

function messageNode(role, text, pending = false) {
  const user = role === "input";
  const node = make("article", `message${user ? " user" : ""}${pending ? " pending" : ""}`);
  node.append(make("span", "message-address", user ? "你" : "Ohana"), make("p", "bubble", text));
  return node;
}

function scrollToEnd() {
  const canvas = element("conversation-scroll");
  canvas.scrollTop = canvas.scrollHeight;
}

function renderMessages() {
  const messages = workspace?.messages || [];
  const list = element("messages");
  const hasMessages = messages.length > 0 || Boolean(pendingMessage);
  element("welcome").hidden = hasMessages;
  list.hidden = !hasMessages;
  list.replaceChildren(...messages.map((item) => messageNode(item.role, item.text)));
  if (pendingMessage) {
    list.append(messageNode("input", pendingMessage), messageNode("output", "正在生成回复…", true));
  }
  if (hasMessages) scrollToEnd();
}

function renderActivity(target, items, full = false) {
  target.replaceChildren();
  if (!items.length) {
    target.append(make("p", "activity-empty", filter === "all" || !full ? "还没有活动记录。" : "这个分类还没有活动。"));
    return;
  }
  for (const item of items) {
    const row = make("article", "activity-row");
    row.dataset.status = item.status;
    const dot = make("span", "activity-dot");
    dot.setAttribute("aria-hidden", "true");
    const label = make("div", "activity-text");
    label.append(make("strong", "", item.title), make("p", "", item.detail));
    const time = make("time", "activity-time", dateFormat.format(new Date(item.time)));
    time.dateTime = item.time;
    row.append(dot, label);
    if (full) row.append(time);
    else label.append(time);
    target.append(row);
  }
}

function render() {
  if (workspace) {
    const model = workspace.model;
    element("model-label").textContent = model || "离线回显";
    element("model-button").toggleAttribute("data-connected", Boolean(model));
    element("chat-mode-note").textContent = model ? "会话保存在本地" : "离线回显 · 用于调试";
    element("model-description").textContent = model ? `当前配置的模型是 ${model}。` : "当前为离线回显模式。";
    element("conversation-label").textContent = workspace.conversation ? `会话 ${workspace.conversation.slice(0, 8)}` : "";
    for (const name of Object.keys(apps)) {
      const config = workspace.apps[name];
      element(`${name}-nav-state`).textContent = config.groups.length || config.accept_private ? "已配置" : "未连接";
    }
  } else {
    element("model-label").textContent = "本地模型";
    element("chat-mode-note").textContent = "等待本地工作区";
  }
  renderMessages();
  const items = workspace?.activities || [];
  renderActivity(element("recent-activity"), items.slice(0, 7));
  renderActivity(element("all-activity"), filter === "all" ? items : items.filter((item) => item.category === filter), true);
}

for (const button of document.querySelectorAll("[data-page]")) {
  button.addEventListener("click", () => selectPage(button.dataset.page));
}
for (const button of document.querySelectorAll("[data-filter]")) {
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    for (const option of document.querySelectorAll("[data-filter]")) {
      option.setAttribute("aria-pressed", String(option === button));
    }
    render();
  });
}
for (const button of document.querySelectorAll("[data-prompt]")) {
  button.addEventListener("click", () => {
    editor.value = button.dataset.prompt;
    updateControls();
    editor.focus();
  });
}
for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => element(button.dataset.close).close());
}

element("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = editor.value.trim();
  if (!text || busy || !workspace) return;
  pendingMessage = text;
  editor.value = "";
  renderMessages();
  const sent = await perform("send", { text, conversation: workspace.conversation });
  if ((!sent || workspace.reply_failed) && !editor.value) editor.value = text;
  updateControls();
  if (page === "chat") editor.focus();
});
editor.addEventListener("input", updateControls);
editor.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    element("chat-form").requestSubmit();
  }
});
element("new-chat").addEventListener("click", async () => {
  if (await perform("open_chat")) {
    editor.value = "";
    selectPage("chat");
    updateControls();
    editor.focus();
  }
});

groups.addEventListener("input", updateGroupCount);
element("app-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const app = page;
  const privateSwitch = element("allow-private");
  const payload = { app, groups: groupLines(), accept_private: privateSwitch.checked };
  element("settings-note").textContent = "";
  groups.disabled = privateSwitch.disabled = true;
  try {
    if (await perform("save_app", payload) && page === app) {
      loadAppForm();
      element("settings-note").textContent = "已保存本地配置";
    }
  } finally {
    groups.disabled = privateSwitch.disabled = false;
  }
});
element("session-button").addEventListener("click", () => {
  element("restore-id").value = workspace?.conversation || "";
  element("restore-error").textContent = "";
  element("session-dialog").showModal();
});
element("restore-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = element("restore-error");
  error.textContent = "";
  const conversation = element("restore-id").value.trim();
  if (await perform("restore_chat", { conversation }, (message) => { error.textContent = message; })) {
    element("session-dialog").close();
    editor.value = "";
    selectPage("chat");
    updateControls();
    editor.focus();
  }
});
element("model-button").addEventListener("click", () => element("model-dialog").showModal());
if (!tauri) element("surface-label").textContent = "浏览器预览";
perform("load").then(() => {
  if (apps[page]) loadAppForm();
});
