const $ = (selector) => document.querySelector(selector);
const form = $("#chat-form");
const input = $("#message");
const send = $("#send");
const messages = $("#messages");
const status = $("#status");
const recallInfo = $("#recall-info");
const thinkingEnabled = $("#thinking-enabled");
const toolsEnabled = $("#tools-enabled");
const conversationList = $("#conversation-list");
const welcomeTemplate = $("#welcome").cloneNode(true);

let conversationId = localStorage.getItem("hiyoriConversationId");
let sending = false;
let currentMemories = [];
thinkingEnabled.checked = localStorage.getItem("hiyoriThinkingEnabled") !== "false";
thinkingEnabled.addEventListener("change", () => {
  localStorage.setItem("hiyoriThinkingEnabled", String(thinkingEnabled.checked));
});
toolsEnabled.checked = localStorage.getItem("hiyoriToolsEnabled") !== "false";
toolsEnabled.addEventListener("change", () => {
  localStorage.setItem("hiyoriToolsEnabled", String(toolsEnabled.checked));
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
  button.title = "将全部台词按原意译成日语，再用本机模型配音";
  const sourceDetails = document.createElement("details");
  sourceDetails.className = "speech-source";
  sourceDetails.hidden = true;
  const sourceSummary = document.createElement("summary");
  sourceSummary.textContent = "查看中文配音原文";
  const sourceCaption = document.createElement("small");
  sourceCaption.className = "speech-caption";
  sourceDetails.append(sourceSummary, sourceCaption);
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
      sourceCaption.textContent = data.source_text;
      sourceDetails.hidden = false;
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
  controls.append(button, sourceDetails, caption, audio);
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

const toolLabels = {
  get_current_time: "查询当前时间",
  search_user_memory: "查找长期记忆",
  search_character_knowledge: "查找原作资料",
  search_chat_history: "查找旧聊天",
  search_web: "联网搜索",
};

function addToolLog() {
  const box = document.createElement("details");
  box.className = "reasoning-box tool-log";
  box.hidden = true;
  const summary = document.createElement("summary");
  box.append(summary);
  messages.append(box);
  const rows = new Map();
  return {
    update(record) {
      box.hidden = false;
      box.open = true;
      const key = `${record.round}:${record.id}`;
      let row = rows.get(key);
      if (!row) {
        row = document.createElement("div");
        const body = document.createElement("pre");
        const sources = document.createElement("div");
        sources.className = "tool-sources";
        row.append(body, sources);
        rows.set(key, row);
        box.append(row);
      }
      const label = toolLabels[record.name] || record.name;
      const state = !record.result ? "查询中" : record.result.error ? "失败" : record.cached ? "已复用" : "完成";
      row.querySelector("pre").textContent = `第 ${record.round} 轮 · ${label} · ${state}\n参数：${JSON.stringify(record.arguments)}\n`
        + (record.result ? `结果：${JSON.stringify(record.result, null, 2)}` : "");
      const sources = row.querySelector(".tool-sources");
      sources.replaceChildren();
      if (record.name === "search_web") {
        for (const source of record.result?.results || []) {
          try {
            const url = new URL(source.url);
            if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) continue;
            const link = document.createElement("a");
            link.href = url.href;
            link.textContent = source.title || url.hostname;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            sources.append(link);
          } catch { /* 忽略无效来源链接。 */ }
        }
      }
      sources.hidden = sources.childElementCount === 0;
      summary.textContent = `工具调用（${rows.size} 次，展开查看）`;
      messages.scrollTop = messages.scrollHeight;
    },
    finish(failed = false) {
      if (box.hidden) box.remove();
      else {
        box.open = false;
        summary.textContent = `工具调用（${rows.size} 次${failed ? "，本轮未保存" : ""}，展开查看）`;
      }
    },
  };
}

function showSavedTools(protocol) {
  if (!protocol?.some((item) => item.tool_calls?.length)) return;
  const log = addToolLog();
  const calls = new Map();
  let round = 0;
  for (const item of protocol) {
    if (item.role === "assistant") {
      round += 1;
      for (const call of item.tool_calls || []) {
        let argumentsValue;
        try { argumentsValue = JSON.parse(call.function.arguments); }
        catch { argumentsValue = call.function.arguments; }
        const record = { id: call.id, name: call.function.name, arguments: argumentsValue, round };
        calls.set(call.id, record);
        log.update(record);
      }
    } else if (item.role === "tool" && calls.has(item.tool_call_id)) {
      let result;
      try { result = JSON.parse(item.content); }
      catch { result = { text: item.content }; }
      log.update({ ...calls.get(item.tool_call_id), result });
    }
  }
  log.finish();
}

function showWelcome() {
  messages.replaceChildren(welcomeTemplate.cloneNode(true));
}

const emptyStoryProgress = () => ({ route: "common", script_name: null, entry_no: 0,
  completed_scripts: [], relationship: "兄妹", scene: "" });
let storyProgress = emptyStoryProgress();
let storyChapters = [];
let storyEditingConversation = null;

function showStoryProgress() {
  $("#story-progress-label").textContent = storyProgress.script_name
    ? `${storyProgress.route === "common" ? "共通线" : "妃爱线"} · ${storyProgress.script_name} #${storyProgress.entry_no} · ${storyProgress.relationship}`
    : "剧情进度未设置，暂不召回原作剧情。";
}

function renderStoryChapter(remembered = null) {
  const selected = storyChapters.find((item) => item.script_name === $("#story-chapter").value);
  const entry = $("#story-entry");
  entry.max = selected?.max_entry ?? 0;
  entry.value = Math.min(Number(entry.value) || 0, Number(entry.max));
  const branches = $("#story-branches");
  const checked = new Set(remembered ?? [...branches.querySelectorAll("input:checked")].map((item) => item.value));
  branches.replaceChildren();
  $("#story-boundary-info").textContent = !selected
    ? "未设置进度时不召回原作片段或剧情摘要。"
    : selected.linked
      ? `本章序号范围 0～${selected.max_entry}；必经前置按实际跳转确定。可选分支只在下方确认后加入记忆。`
      : `这是未与主线连接的独立场景；前置顺序不能自动确认，请只勾选已经经历的章节。序号范围 0～${selected.max_entry}。`;
  if (selected?.uncertain_from != null) {
    $("#story-boundary-info").textContent += ` 本章内部选择尚未核对，#${selected.uncertain_from} 及之后暂不召回。`;
  }
  const optional = selected?.linked ? selected.optional_predecessors : storyChapters
    .filter((item) => item.script_name !== selected?.script_name &&
      ($("#story-route").value === "hiyori" || item.scope === "common"))
    .map((item) => item.script_name);
  for (const name of selected ? optional : []) {
    const label = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = name;
    checkbox.checked = checked.has(name);
    label.append(checkbox, document.createTextNode(name));
    branches.append(label);
  }
  if (!branches.childElementCount) branches.textContent = "当前阶段没有需要确认的可选前置。";
}

function renderStoryRoute(selectedName = null, remembered = null) {
  const select = $("#story-chapter");
  select.replaceChildren(new Option("未设置", ""));
  for (const item of storyChapters) {
    if ($("#story-route").value === "common" && item.scope !== "common") continue;
    select.append(new Option(item.script_name + (item.linked ? "" : "（独立场景）"), item.script_name));
  }
  select.value = selectedName || "";
  if (select.selectedIndex < 0) select.value = "";
  renderStoryChapter(remembered);
}

$("#story-button").addEventListener("click", async () => {
  if (sending) return;
  try {
    clearError();
    storyEditingConversation = conversationId;
    const timeline = await api("/story/timeline");
    if (conversationId !== storyEditingConversation || sending) return;
    storyChapters = timeline.chapters;
    $("#story-route").value = storyProgress.route;
    $("#story-entry").value = storyProgress.entry_no;
    $("#story-relationship").value = storyProgress.relationship;
    $("#story-scene").value = storyProgress.scene;
    $("#story-branches").replaceChildren();
    renderStoryRoute(storyProgress.script_name, storyProgress.completed_scripts);
    clearError($("#story-status"));
    $("#story-dialog").showModal();
  } catch (error) { showError(error); }
});
$("#story-route").addEventListener("change", () => { $("#story-entry").value = 0; renderStoryRoute(); });
$("#story-chapter").addEventListener("change", () => { $("#story-entry").value = 0; renderStoryChapter(); });
$("#story-complete-chapter").addEventListener("click", () => { $("#story-entry").value = $("#story-entry").max; });
$("#story-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    if (sending || conversationId !== storyEditingConversation) throw new Error("会话已切换或正在回复，请重新打开进度窗口。");
    const state = { route: $("#story-route").value, script_name: $("#story-chapter").value || null,
      entry_no: Number($("#story-entry").value),
      completed_scripts: [...$("#story-branches").querySelectorAll("input:checked")].map((item) => item.value),
      relationship: $("#story-relationship").value.trim(), scene: $("#story-scene").value.trim() };
    if (conversationId) {
      const data = await api(`/conversations/${conversationId}/story-progress`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state) });
      storyProgress = data.story_progress;
    } else storyProgress = await api("/story/validate-progress", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state) });
    showStoryProgress();
    closeDialog("story-dialog");
  } catch (error) { showError(error, $("#story-status")); }
});

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
    storyProgress = data.conversation.story_progress || emptyStoryProgress();
    showStoryProgress();
    localStorage.setItem("hiyoriConversationId", id);
    $("#chat-title").textContent = data.conversation.title;
    recallInfo.textContent = "发送新消息时会重新检索相关记忆。";
    messages.replaceChildren();
    for (const item of data.messages) {
      if (item.role === "assistant") showSavedTools(item.agent_messages);
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
  storyProgress = emptyStoryProgress();
  showStoryProgress();
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
  const toolLog = addToolLog();
  const thought = addReasoning();
  thought.box.hidden = true;
  const pending = addBubble("assistant", "正在回复…", "pending");
  let received = false;

  try {
    const response = await fetch("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, conversation_id: conversationId,
        thinking: thinkingEnabled.checked, tools_enabled: toolsEnabled.checked,
        ...(conversationId ? {} : { story_progress: storyProgress }) }),
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
      } else if (name === "agent_round" && data.round > 1) {
        pending.textContent = `正在继续思考（第 ${data.round} 轮）…`;
        pending.classList.add("pending");
        received = false;
        if (thought.body.textContent) thought.body.textContent += `\n\n—— 第 ${data.round} 轮 ——\n`;
      } else if (name === "tool_start") {
        toolLog.update(data);
        pending.textContent = "正在查询资料…";
        pending.classList.add("pending");
        received = false;
      } else if (name === "tool_result") {
        toolLog.update(data);
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
        toolLog.finish();
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
    toolLog.finish(true);
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
    $(".subtitle").textContent = `V0.9 · ${character.name}`;
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
    const modelStatus = await api("/model-status");
    if (!modelStatus.thinking_supported) {
      thinkingEnabled.checked = false;
      thinkingEnabled.closest("label").hidden = true;
      $("footer .hint").textContent = "Enter 发送，Shift + Enter 换行 · 本地模型，工具调用记录可展开查看";
    }
    const character = await api("/character");
    $(".subtitle").textContent = `V0.9 · ${character.name} · ${modelStatus.provider === "local" ? "本地模型" : "DeepSeek"}`;
    const conversations = await refreshConversations();
    const chosen = conversations.find((item) => item.id === conversationId) || conversations[0];
    if (chosen) await loadHistory(chosen.id);
  } catch (error) { showError(error); }
}

initialize();
