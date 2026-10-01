const $ = (selector) => document.querySelector(selector);
const form = $("#chat-form");
const input = $("#message");
const send = $("#send");
const messages = $("#messages");
const status = $("#status");
const recallInfo = $("#recall-info");
const thinkingEnabled = $("#thinking-enabled");
const conversationList = $("#conversation-list");
const welcomeTemplate = $("#welcome").cloneNode(true);

let conversationId = localStorage.getItem("hiyoriConversationId");
let sending = false;
let currentMemories = [];
thinkingEnabled.checked = localStorage.getItem("hiyoriThinkingEnabled") !== "false";
thinkingEnabled.addEventListener("change", () => {
  localStorage.setItem("hiyoriThinkingEnabled", String(thinkingEnabled.checked));
});

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const data = await response.json();
  if (!response.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : "请求失败");
  }
  return data;
}

function showError(error, target = status) {
  target.textContent = error instanceof Error ? error.message : String(error);
  target.hidden = false;
}

function clearError(target = status) {
  target.textContent = "";
  target.hidden = true;
}

function addBubble(role, text, extraClass = "") {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role} ${extraClass}`;
  bubble.textContent = text; // 模型输出按文本渲染，不执行其中的 HTML。
  messages.append(bubble);
  messages.scrollTop = messages.scrollHeight;
  return bubble;
}

function addSpeechControl(bubble, text) {
  const controls = document.createElement("div");
  controls.className = "speech-controls";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary";
  button.textContent = "▶ 妃爱配音";
  button.title = "提取台词并译成日语，再用本机模型配音";
  const caption = document.createElement("small");
  caption.className = "speech-caption";
  const audio = document.createElement("audio");
  audio.controls = true;
  audio.preload = "none";
  audio.hidden = true;

  button.addEventListener("click", async () => {
    if (audio.src) {
      audio.currentTime = 0;
      await audio.play().catch(() => {});
      return;
    }
    button.disabled = true;
    button.textContent = "正在生成配音…";
    caption.textContent = "";
    try {
      const data = await api("/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!bubble.isConnected) return;
      caption.textContent = `日语台词：${data.japanese}`;
      audio.src = data.audio_url;
      audio.hidden = false;
      button.textContent = "▶ 重新播放";
      await audio.play().catch(() => {}); // 浏览器拒绝自动播放时仍可用控件播放。
    } catch (error) {
      caption.textContent = error instanceof Error ? error.message : "配音生成失败";
      button.textContent = "重试配音";
    } finally {
      button.disabled = false;
    }
  });
  controls.append(button, caption, audio);
  bubble.append(controls);
}

function addReasoning(text = "") {
  const box = document.createElement("details");
  box.className = "reasoning-box";
  const summary = document.createElement("summary");
  summary.textContent = "模型思考（展开/收起）";
  const body = document.createElement("div");
  body.className = "reasoning-text";
  body.textContent = text; // 思考文本也只按纯文本显示。
  box.append(summary, body);
  messages.append(box);
  return { box, summary, body };
}

function showWelcome() {
  messages.replaceChildren(welcomeTemplate.cloneNode(true));
}

function renderConversations(items) {
  conversationList.replaceChildren();
  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `conversation-item${item.id === conversationId ? " active" : ""}`;
    button.textContent = item.title;
    button.title = item.title;
    button.addEventListener("click", () => loadHistory(item.id));
    conversationList.append(button);
  }
}

async function refreshConversations() {
  const items = await api("/conversations");
  renderConversations(items);
  return items;
}

async function loadHistory(id) {
  if (sending) return;
  try {
    clearError();
    const data = await api(`/conversations/${id}/messages`);
    conversationId = id;
    localStorage.setItem("hiyoriConversationId", id);
    $("#chat-title").textContent = data.conversation.title;
    recallInfo.textContent = "发送新消息时会重新检索相关记忆。";
    messages.replaceChildren();
    for (const item of data.messages) {
      if (item.role === "assistant" && item.reasoning) addReasoning(item.reasoning);
      const bubble = addBubble(item.role, item.content);
      if (item.role === "assistant") addSpeechControl(bubble, item.content);
    }
    if (data.messages.length === 0) showWelcome();
    await refreshConversations();
  } catch (error) {
    showError(error);
  }
}

function newChat() {
  if (sending) return;
  conversationId = null;
  localStorage.removeItem("hiyoriConversationId");
  $("#chat-title").textContent = "新对话";
  recallInfo.textContent = "每轮会带上最近消息，并按需召回长期记忆。";
  clearError();
  showWelcome();
  for (const button of conversationList.children) button.classList.remove("active");
  input.focus();
}

async function readSse(response, onEvent) {
  if (!response.body) throw new Error("浏览器不支持流式响应");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = false;

  while (!completed) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // 一个网络块可能含多个 SSE 事件，也可能只有半个事件。
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const raw = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const lines = raw.split("\n");
      const name = lines.find((line) => line.startsWith("event: "))?.slice(7);
      const dataLine = lines.find((line) => line.startsWith("data: "))?.slice(6);
      if (!name || !dataLine) continue;
      const data = JSON.parse(dataLine);
      if (name === "error") throw new Error(data.message || "模型服务调用失败");
      onEvent(name, data);
      if (name === "done") {
        completed = true;
        break;
      }
    }
  }
  if (!completed) throw new Error("流式回复意外中断");
}

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message || sending) return;

  sending = true;
  clearError();
  $("#welcome")?.remove();
  addBubble("user", message);
  input.value = "";
  input.disabled = true;
  send.disabled = true;
  const thought = addReasoning();
  thought.box.hidden = true;
  const pending = addBubble("assistant", "正在回复…", "pending");
  let received = false;

  try {
    const response = await fetch("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, conversation_id: conversationId,
        thinking: thinkingEnabled.checked }),
    });
    if (!response.ok) {
      const failure = await response.json();
      throw new Error(typeof failure.detail === "string" ? failure.detail : "请求失败");
    }

    await readSse(response, (name, data) => {
      if (name === "meta") {
        conversationId = data.conversation_id;
        localStorage.setItem("hiyoriConversationId", conversationId);
        const recalled = data.recalled_memories || [];
        recallInfo.textContent = recalled.length
          ? `本轮召回：${recalled.map((item) => item.content.slice(0, 40)).join("；")}`
          : "本轮未召回长期记忆。";
      } else if (name === "reasoning_delta") {
        thought.box.hidden = false;
        thought.box.open = true;
        thought.summary.textContent = "模型思考中…";
        thought.body.textContent += data.text;
        messages.scrollTop = messages.scrollHeight;
      } else if (name === "delta") {
        if (!received) {
          pending.textContent = "";
          pending.classList.remove("pending");
          received = true;
        }
        pending.textContent += data.text;
        messages.scrollTop = messages.scrollHeight;
      } else if (name === "done") {
        if (thought.box.hidden) thought.box.remove();
        else thought.summary.textContent = "模型思考（展开/收起）";
        addSpeechControl(pending, pending.textContent);
      }
    });
    await refreshConversations();
    if (conversationId && $("#chat-title").textContent === "新对话") {
      $("#chat-title").textContent = message.slice(0, 40);
    }
  } catch (error) {
    if (received) {
      pending.textContent += "\n\n（回复中断，未保存这轮聊天）";
      if (!thought.box.hidden) thought.summary.textContent = "模型思考（本轮未保存）";
    } else {
      pending.remove();
      thought.box.remove();
    }
    showError(error);
  } finally {
    sending = false;
    input.disabled = false;
    send.disabled = false;
    input.focus();
  }
});

function closeDialog(id) { $(`#${id}`).close(); }
for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => closeDialog(button.dataset.close));
}

$("#new-chat").addEventListener("click", newChat);
$("#character-button").addEventListener("click", async () => {
  const dialog = $("#character-dialog");
  dialog.showModal();
  clearError($("#character-status"));
  $("#role-knowledge-count").textContent = "原作角色资料：加载中…";
  try {
    const character = await api("/character");
    $("#character-name").value = character.name;
    $("#character-description").value = character.description;
    $("#character-personality").value = character.personality;
    $("#character-background").value = character.background;
    $("#character-speaking-style").value = character.speaking_style;
    $("#system-prompt").value = character.system_prompt;
    const knowledge = await api("/character/knowledge");
    $("#role-knowledge-count").textContent =
      `原作角色资料：${knowledge.count} 条摘要、${knowledge.dialogue_lines} 条中文对话，`
      + `${knowledge.dialogue_chunks} 个检索片段（与用户长期记忆分开）`;
  } catch (error) {
    showError(error, $("#character-status"));
  }
});

$("#character-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const character = await api("/character", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("#character-name").value.trim(),
        description: $("#character-description").value.trim(),
        personality: $("#character-personality").value.trim(),
        background: $("#character-background").value.trim(),
        speaking_style: $("#character-speaking-style").value.trim(),
        system_prompt: $("#system-prompt").value.trim(),
      }),
    });
    $(".subtitle").textContent = `V0.8 · ${character.name}`;
    closeDialog("character-dialog");
  } catch (error) {
    showError(error, $("#character-status"));
  }
});

function memoryRow(item) {
  const row = document.createElement("article");
  row.className = "memory-row";
  row.dataset.id = item.id;
  row.innerHTML = `<div class="memory-row-top"><input class="memory-select" type="checkbox" aria-label="选择合并" />
    <textarea class="memory-text" maxlength="300" rows="2" aria-label="记忆内容"></textarea></div>
    <div class="memory-row-bottom"><label>重要度 <input class="memory-importance" type="number" min="1" max="5" /></label>
    <button class="secondary memory-save" type="button">保存</button>
    <button class="secondary memory-delete" type="button">遗忘</button></div>`;
  row.querySelector(".memory-text").value = item.content;
  row.querySelector(".memory-importance").value = item.importance;
  return row;
}

async function refreshMemories() {
  currentMemories = await api("/memories");
  const list = $("#memory-list");
  list.replaceChildren();
  if (currentMemories.length === 0) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "还没有长期记忆。";
    list.append(empty);
  } else {
    for (const item of currentMemories) list.append(memoryRow(item));
  }
}

$("#memory-button").addEventListener("click", async () => {
  $("#memory-dialog").showModal();
  clearError($("#memory-status"));
  try { await refreshMemories(); }
  catch (error) { showError(error, $("#memory-status")); }
});

$("#refresh-memories").addEventListener("click", async () => {
  try { await refreshMemories(); clearError($("#memory-status")); }
  catch (error) { showError(error, $("#memory-status")); }
});

$("#memory-list").addEventListener("change", (event) => {
  if (!event.target.classList.contains("memory-select")) return;
  const ids = [...document.querySelectorAll(".memory-select:checked")]
    .map((input) => Number(input.closest(".memory-row").dataset.id));
  const selected = currentMemories.filter((item) => ids.includes(item.id));
  $("#merged-memory").value = selected.map((item) => item.content).join("；").slice(0, 300);
  $("#merged-importance").value = selected.length
    ? Math.max(...selected.map((item) => item.importance)) : 3;
});

$("#memory-list").addEventListener("click", async (event) => {
  const row = event.target.closest(".memory-row");
  if (!row) return;
  const id = Number(row.dataset.id);
  try {
    if (event.target.classList.contains("memory-save")) {
      await api(`/memories/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: row.querySelector(".memory-text").value.trim(),
          importance: Number(row.querySelector(".memory-importance").value),
        }),
      });
    } else if (event.target.classList.contains("memory-delete")) {
      if (!window.confirm("确定要遗忘这条记忆吗？")) return;
      await api(`/memories/${id}`, { method: "DELETE" });
    } else return;
    await refreshMemories();
    clearError($("#memory-status"));
  } catch (error) { showError(error, $("#memory-status")); }
});

$("#add-memory-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/memories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content: $("#new-memory").value.trim(),
        importance: Number($("#new-importance").value),
      }),
    });
    $("#new-memory").value = "";
    await refreshMemories();
    clearError($("#memory-status"));
  } catch (error) { showError(error, $("#memory-status")); }
});

$("#merge-memory-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const ids = [...document.querySelectorAll(".memory-select:checked")]
    .map((input) => Number(input.closest(".memory-row").dataset.id));
  if (ids.length < 2) {
    showError(new Error("请至少勾选两条记忆"), $("#memory-status"));
    return;
  }
  try {
    await api("/memories/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ids,
        content: $("#merged-memory").value.trim(),
        importance: Number($("#merged-importance").value),
      }),
    });
    $("#merged-memory").value = "";
    await refreshMemories();
    clearError($("#memory-status"));
  } catch (error) { showError(error, $("#memory-status")); }
});

async function initialize() {
  try {
    const character = await api("/character");
    $(".subtitle").textContent = `V0.8 · ${character.name}`;
    const conversations = await refreshConversations();
    const chosen = conversations.find((item) => item.id === conversationId) || conversations[0];
    if (chosen) await loadHistory(chosen.id);
  } catch (error) { showError(error); }
}

initialize();
