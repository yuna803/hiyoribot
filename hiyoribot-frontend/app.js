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
let activeGeneration = null;
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
      const task = await api("/tts/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const stages = { queued:"排队中",waiting_gpu:"等待显卡空闲",translating:"正在翻译完整台词",
        releasing_gpu:"正在释放聊天模型显存",synthesizing:"正在合成日语配音" };
      let data;
      while (bubble.isConnected) {
        const job = await api(`/tts/jobs/${task.id}`);
        if (job.stage === "failed") throw new Error(job.error || "配音失败，请重试");
        if (job.stage === "ready") { data = job.result; break; }
        caption.textContent = `${stages[job.stage] || "正在处理"} · 已等待 ${Math.round(job.elapsed_seconds)} 秒`;
        await new Promise((resolve) => setTimeout(resolve,1500));
      }
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

function addRecallTrace(saved = null) {
  const box = document.createElement("details");
  box.className = "reasoning-box recall-trace";
  box.hidden = true;
  const title = document.createElement("summary");
  title.textContent = "剧情召回对照（展开查看）";
  const body = document.createElement("div");
  box.append(title,body);
  messages.append(box);
  function append(record) {
    box.hidden = false;
    const section = document.createElement("section");
    const heading = document.createElement("p");
    heading.textContent = `第 ${record.round} 轮实际传入的参考 · 会话摘要${record.summary_used ? "已传入" : "未传入"}`;
    if (record.progress?.script_name) heading.textContent += ` · 当轮进度 ${record.progress.script_name} #${record.progress.entry_no}`;
    section.append(heading);
    for (const row of record.notes || []) {
      const text = document.createElement("pre");
      text.textContent = `角色资料 ${row.source}\n${row.content}`;
      section.append(text);
    }
    for (const row of record.dialogue || []) {
      const text = document.createElement("pre");
      text.textContent = `${row.script_name} #${row.first_entry}～${row.last_entry} · 相似度 ${Number(row.similarity).toFixed(2)}\n${row.content}`;
      section.append(text);
    }
    if (!(record.notes?.length || record.dialogue?.length)) {
      const hint = document.createElement("p");
      hint.textContent = "这一轮没有传入原作片段。工具查询结果可在工具记录中查看；传入资料不代表模型一定遵循。";
      section.append(hint);
    }
    body.append(section);
  }
  for (const record of saved?.rounds || []) append(record);
  return { append,box };
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
      if (item.role === "assistant" && item.recalled_context) addRecallTrace(item.recalled_context);
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
      if (name === "cancelled") { const error = new Error("已停止生成，本轮未保存"); error.name = "AbortError"; throw error; }
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
  const generation = { id:crypto.randomUUID(),controller:new AbortController(),stopped:false };
  activeGeneration = generation;
  $("#stop-generation").hidden = false;
  $("#stop-generation").disabled = false;
  clearError();
  $("#welcome")?.remove();
  addBubble("user", message);
  input.value = "";
  input.disabled = true;
  send.disabled = true;
  const toolLog = addToolLog();
  const recalled = addRecallTrace();
  const thought = addReasoning();
  thought.box.hidden = true;
  const pending = addBubble("assistant", "正在回复…", "pending");
  let received = false;

  try {
    const response = await fetch("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal:generation.controller.signal,
      body: JSON.stringify({ message, conversation_id: conversationId,generation_id:generation.id,
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
      } else if (name === "context_used") {
        recalled.append(data);
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
      pending.textContent += error.name === "AbortError" ? "\n\n（已停止，本轮未保存）" : "\n\n（回复中断，未保存这轮聊天）";
      if (!thought.box.hidden) thought.summary.textContent = "模型思考（本轮未保存）";
    } else {
      pending.remove();
      thought.box.remove();
    }
    showError(error.name === "AbortError" ? new Error("已停止生成，输入框保留原消息，可修改后重新发送。") : error);
    input.value = message;
  } finally {
    sending = false;
    input.disabled = false;
    send.disabled = false;
    activeGeneration = null;
    $("#stop-generation").hidden = true;
    input.focus();
  }
});

$("#stop-generation").addEventListener("click", async () => {
  const generation = activeGeneration;
  if (!generation) return;
  $("#stop-generation").disabled = true;
  try {
    const result = await api(`/generation/${generation.id}/cancel`, { method:"POST" });
    if (result.accepted) {
      generation.stopped = true;
      generation.controller.abort();
    } else showError(new Error("回复已生成，正在完成保存，请稍等。"));
  } catch (error) { showError(error); $("#stop-generation").disabled = false; }
});

function closeDialog(id) { $(`#${id}`).close(); }
for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => closeDialog(button.dataset.close));
}

$("#new-chat").addEventListener("click", newChat);
let summaryRevision = 0;
let summaryConversation = null;

async function loadSummary() {
  const data = await api(`/conversations/${summaryConversation}/summary`);
  $("#conversation-summary").value = data.summary;
  summaryRevision = data.revision;
  $("#summary-info").textContent = data.through_id ? `已整理至消息 #${data.through_id}；最近问答仍保留在上下文。` : "尚未生成摘要。旧问答积累后会自动整理，也可手动更新。";
  if (data.summary) $("#summary-info").textContent += data.origin === "manual" ? " 当前为手动修正版本。" : " 当前由模型整理，可核对修正。";
}
$("#summary-button").addEventListener("click", async () => {
  if (sending) return;
  if (!conversationId) { showError(new Error("请先开始一个会话，再查看摘要。")); return; }
  summaryConversation = conversationId;
  clearError($("#summary-status"));
  $("#summary-dialog").showModal();
  try { await loadSummary(); }
  catch (error) { showError(error,$("#summary-status")); }
});
$("#update-summary").addEventListener("click", async () => {
  const button = $("#update-summary");
  button.disabled = true;
  button.textContent = "正在整理问答…";
  try {
    await api(`/conversations/${summaryConversation}/summary`, { method:"POST" });
    await loadSummary();
    clearError($("#summary-status"));
  } catch (error) { showError(error,$("#summary-status")); }
  finally { button.disabled = false; button.textContent = "根据新问答更新"; }
});
$("#summary-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api(`/conversations/${summaryConversation}/summary`, { method:"PUT",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({summary:$("#conversation-summary").value,revision:summaryRevision}) });
    await loadSummary();
    closeDialog("summary-dialog");
  } catch (error) { showError(error,$("#summary-status")); }
});
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
    <button class="secondary memory-save" type="button">保存文字</button>
    <button class="secondary memory-confirm" type="button">确认分类</button>
    <button class="secondary memory-reject" type="button">不采用</button>
    <button class="secondary memory-delete" type="button">遗忘</button></div>
    <select class="memory-kind" aria-label="记忆分类"><option value="real">现实事实或偏好</option><option value="roleplay">角色扮演设定</option><option value="hypothetical">临时假设（不召回）</option></select>
    <p class="memory-meta"></p><details class="memory-source"><summary>查看来源用户原话</summary><p></p></details>
    <div class="memory-conflicts"></div>`;
  row.querySelector(".memory-text").value = item.content;
  row.querySelector(".memory-importance").value = item.importance;
  row.querySelector(".memory-kind").value = item.kind || "real";
  row.dataset.status = item.status || "pending";
  row.dataset.scope = item.conversation_id || item.source_conversation_id || conversationId || "";
  const statusNames = {pending:"待确认",confirmed:"已确认",rejected:"不采用",superseded:"已替换"};
  row.querySelector(".memory-meta").textContent = `${statusNames[item.status] || "旧版本待确认"} · ${item.conversation_title || "手动或旧版本条目"}${item.replaces_ids?.length ? ` · 替换了 #${item.replaces_ids.join("、#")}` : ""}`;
  row.querySelector(".memory-source p").textContent = item.source_text || "这条记忆没有来源原话，请核对后再确认。";
  row.querySelector(".memory-kind").addEventListener("change", () => loadMemoryConflicts(row));
  loadMemoryConflicts(row);
  return row;
}

async function loadMemoryConflicts(row) {
  const kind = row.querySelector(".memory-kind").value;
  const scope = kind === "roleplay" ? row.dataset.scope : "";
  const key = `${kind}:${scope}`;
  row.dataset.reviewKey = key;
  const button = row.querySelector(".memory-confirm");
  const container = row.querySelector(".memory-conflicts");
  button.disabled = true;
  container.textContent = "正在检查相关旧记忆…";
  try {
    if (kind === "roleplay" && !scope) throw new Error("角色扮演条目需要所属会话，请先开始会话。");
    const candidates = await api(`/memories/${row.dataset.id}/conflicts?kind=${kind}${scope ? `&conversation_id=${encodeURIComponent(scope)}` : ""}`);
    if (row.dataset.reviewKey !== key || !row.isConnected) return;
    container.replaceChildren();
    const hint = document.createElement("p");
    hint.textContent = candidates.length ? "可能相关的已确认旧记忆（仅勾选的条目会被替换）：" :
      row.dataset.status === "pending" ? "没有发现相关旧记忆；分类确认后才参与召回。" : "没有发现相关旧记忆；变更分类后请再次确认。";
    container.append(hint);
    for (const candidate of candidates) {
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox"; checkbox.className = "memory-conflict-select"; checkbox.value = candidate.id;
      label.append(checkbox,document.createTextNode(`#${candidate.id} ${candidate.content}`));
      container.append(label);
    }
    button.disabled = false;
  } catch (error) { if (row.dataset.reviewKey === key) container.textContent = error.message; }
}

async function refreshMemories() {
  currentMemories = await api("/memories");
  const list = $("#memory-list");
  list.replaceChildren();
  const filter = $("#memory-filter").value;
  const visible = currentMemories.filter((item) => filter === "all" || (filter === "active" && ["pending","confirmed"].includes(item.status)) ||
    (filter === "archive" && ["rejected","superseded"].includes(item.status)) || item.status === filter);
  if (visible.length === 0) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "这一分类下没有记忆。";
    list.append(empty);
  } else {
    for (const item of visible) list.append(memoryRow(item));
  }
}

$("#memory-button").addEventListener("click", async () => {
  $("#memory-dialog").showModal();
  clearError($("#memory-status"));
  try { await refreshMemories(); }
  catch (error) { showError(error, $("#memory-status")); }
});
$("#memory-filter").addEventListener("change", async () => {
  try { await refreshMemories(); }
  catch (error) { showError(error,$("#memory-status")); }
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
    } else if (event.target.classList.contains("memory-confirm") || event.target.classList.contains("memory-reject")) {
      const kind = row.querySelector(".memory-kind").value;
      await api(`/memories/${id}`, {method:"PUT",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({content:row.querySelector(".memory-text").value.trim(),importance:Number(row.querySelector(".memory-importance").value)})});
      await api(`/memories/${id}/review`, {method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({kind,status:event.target.classList.contains("memory-reject") ? "rejected" : "confirmed",
          conversation_id:kind === "roleplay" ? row.dataset.scope || null : null,
          replace_ids:[...row.querySelectorAll(".memory-conflict-select:checked")].map((input) => Number(input.value))})});
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
        kind:$("#new-memory-kind").value,
        conversation_id:$("#new-memory-kind").value === "roleplay" ? conversationId : null,
      }),
    });
    $("#new-memory").value = "";
    if ($("#new-memory-kind").value === "hypothetical") $("#memory-filter").value = "archive";
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
