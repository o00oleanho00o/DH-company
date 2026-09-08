// Floating AI assistant widget (chatbot bubble).
//
// Self-contained IIFE, independent of app.js. Structure:
//   1. API layer   — the only place that talks to the backend.
//   2. State       — in-memory conversation history + pending attachment.
//   3. Persistence — mirrors history to localStorage so a reload shows the
//                    conversation immediately, before any network call.
//   4. Rendering   — DOM/markdown output helpers.
//   5. Logic       — conversation flow (send, reset, attach file).
//   6. UI events   — wiring buttons/inputs to the logic above.
//
// The assistant answers from the server-side knowledge base
// (prompt_system.txt) and can also call real backend tools (see
// app/chatbot.py) to look up or — after the user explicitly confirms in
// chat — change data. This file never talks to the AI provider directly and
// never sees the provider API key; it only calls this app's own endpoints.
(() => {
  "use strict";

  const FALLBACK_GREETING =
    "Xin chào! 👋 Tôi là trợ lý AI của DH M&E Pricing Hub, có thể giúp bạn:\n\n" +
    "- **Kho dữ liệu** — xem/import các nguồn dữ liệu Excel\n" +
    "- **Danh mục & Giá** — tra cứu vật tư, nhân công\n" +
    "- **Tạo báo giá** — tạo và chạy pricing từ file BOQ\n" +
    "- **Bàn rà soát** — xem và xử lý các dòng cần rà soát\n" +
    "- **Xuất Excel** — xuất báo giá ra file\n\n" +
    "Bạn cần hỗ trợ việc gì hôm nay? 😊";

  // Quick-prompt chips shown under the greeting so users can start with a
  // click instead of typing from scratch. Clicking one only fills the input
  // box — it never auto-sends, so the user can still edit before Enter.
  const SUGGESTIONS = [
    "App này giúp tôi làm những việc gì?",
    "Xem danh sách nguồn dữ liệu đã import",
    "Có bao nhiêu báo giá đã tạo?",
    "Hướng dẫn tạo báo giá mới từ file BOQ",
  ];

  const STORAGE_KEY = "dh-chatbot-history-v1";
  // Matches MAX_TOTAL_HISTORY_MESSAGES in app/chatbot.py: the server folds
  // anything past its verbatim window into a recap instead of dropping it, so
  // keeping (and resending) this much is what lets a long chat stay coherent.
  const MAX_STORED_MESSAGES = 120;

  // Matches the "[Tệp đính kèm]" note handleSend() bakes into a user
  // message's persisted content (see there for why it must live in the
  // content string itself). Stripped back out only for display, both live
  // and when restoring from localStorage, so the bubble always shows the
  // friendly "📎 Đã đính kèm: ..." form instead of the raw note.
  const ATTACHMENT_NOTE_RE = /\n\n\[Tệp đính kèm\]\n- "([^"]+)" \(upload_id: [^)]*\)$/;

  function toDisplayText(content) {
    const match = ATTACHMENT_NOTE_RE.exec(content);
    if (!match) return content;
    return `${content.slice(0, match.index)}\n\n📎 Đã đính kèm: ${match[1]}`;
  }

  // ------------------------------------------------------------------
  // 1. API layer
  // ------------------------------------------------------------------
  const ChatbotAPI = {
    async getGreeting() {
      const res = await fetch("/api/chatbot/greeting");
      if (!res.ok) throw new Error("greeting_unavailable");
      return res.json();
    },

    async sendMessage(history) {
      const res = await fetch("/api/chatbot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: history }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || "Không thể kết nối tới trợ lý AI lúc này.");
      }
      return data.reply;
    },

    async uploadFile(file) {
      const formData = new FormData();
      formData.append("file", file);
      const res = await fetch("/api/chatbot/upload", { method: "POST", body: formData });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || "Không tải được tệp lên.");
      }
      return data; // {upload_id, filename, size_bytes}
    },
  };

  // ------------------------------------------------------------------
  // 2. State
  // ------------------------------------------------------------------
  const state = {
    history: [], // [{role: 'user'|'assistant', content: string}]
    isOpen: false,
    isSending: false,
    greeting: FALLBACK_GREETING,
    pendingAttachment: null, // {upload_id, filename} | null
    mode: "quick",
    returnRoute: "#dashboard",
    enabled: true,
  };

  // ------------------------------------------------------------------
  // 3. Persistence — per-browser only (localStorage never reaches the
  // server or other viewers). Wrapped in try/catch throughout: private
  // browsing, blocked site data, or a full quota must degrade to a
  // session-only conversation, never break the widget.
  // ------------------------------------------------------------------
  function saveHistory() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state.history.slice(-MAX_STORED_MESSAGES)));
    } catch (err) {
      // Conversation still works for this session; it just won't survive a reload.
    }
  }

  function loadStoredHistory() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return null;
      const cleaned = parsed.filter(
        (m) => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string"
      );
      return cleaned.length ? cleaned : null;
    } catch (err) {
      return null;
    }
  }

  function clearStoredHistory() {
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch (err) {
      // ignore
    }
  }

  // ------------------------------------------------------------------
  // DOM refs
  // ------------------------------------------------------------------
  const el = {
    widget: document.getElementById("chatbot-widget"),
    toggle: document.getElementById("chatbot-toggle"),
    panel: document.getElementById("chatbot-panel"),
    closeBtn: document.getElementById("chatbot-close"),
    expandBtn: document.getElementById("chatbot-expand"),
    refreshBtn: document.getElementById("chatbot-refresh"),
    messages: document.getElementById("chatbot-messages"),
    typing: document.getElementById("chatbot-typing"),
    form: document.getElementById("chatbot-form"),
    input: document.getElementById("chatbot-input"),
    sendBtn: document.getElementById("chatbot-send"),
    attachBtn: document.getElementById("chatbot-attach"),
    fileInput: document.getElementById("chatbot-file-input"),
    attachmentBox: document.getElementById("chatbot-attachment"),
    attachmentName: document.getElementById("chatbot-attachment-name"),
    attachmentRemoveBtn: document.getElementById("chatbot-attachment-remove"),
  };

  // Widget markup may be absent (e.g. a page that intentionally omits it).
  if (!el.widget || !el.toggle || !el.panel || !el.form || !el.input) return;

  // ------------------------------------------------------------------
  // 3. Rendering
  // ------------------------------------------------------------------
  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function renderMarkdown(text) {
    if (window.marked && typeof window.marked.parse === "function") {
      try {
        return window.marked.parse(text, { breaks: true });
      } catch (err) {
        // Fall through to safe plain-text rendering below.
      }
    }
    return escapeHtml(text).replace(/\n/g, "<br>");
  }

  function scrollToBottom() {
    el.messages.scrollTop = el.messages.scrollHeight;
  }

  function appendMessage(role, content, { isError = false } = {}) {
    const wrap = document.createElement("div");
    wrap.className = `chatbot-msg is-${isError ? "error" : role}`;

    const bubble = document.createElement("div");
    bubble.className = "chatbot-bubble";
    if (role === "assistant" && !isError) {
      const markdown = document.createElement("div");
      markdown.className = "chat-markdown";
      markdown.innerHTML = renderMarkdown(content);
      bubble.appendChild(markdown);
    } else {
      bubble.textContent = content;
    }

    wrap.appendChild(bubble);
    el.messages.appendChild(wrap);
    scrollToBottom();
  }

  function removeSuggestions() {
    const existing = el.messages.querySelector(".chatbot-suggestions");
    if (existing) existing.remove();
  }

  function renderSuggestions() {
    removeSuggestions();
    const wrap = document.createElement("div");
    wrap.className = "chatbot-suggestions";

    const label = document.createElement("span");
    label.className = "chatbot-suggestions-label";
    label.textContent = "Gợi ý nhanh — bấm để điền vào ô chat:";
    wrap.appendChild(label);

    const list = document.createElement("div");
    list.className = "chatbot-suggestions-list";
    SUGGESTIONS.forEach((text) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chatbot-suggestion-chip";
      chip.textContent = text;
      chip.addEventListener("click", () => {
        el.input.value = text;
        autoGrowInput();
        el.input.focus();
        // Put the cursor at the end so the user can keep typing/editing.
        el.input.setSelectionRange(text.length, text.length);
      });
      list.appendChild(chip);
    });
    wrap.appendChild(list);

    el.messages.appendChild(wrap);
    scrollToBottom();
  }

  function setTyping(isTyping) {
    el.typing.hidden = !isTyping;
    if (isTyping) scrollToBottom();
  }

  function setSending(isSending) {
    state.isSending = isSending;
    el.sendBtn.disabled = isSending;
    el.input.disabled = isSending;
    el.attachBtn.disabled = isSending;
  }

  function autoGrowInput() {
    el.input.style.height = "auto";
    el.input.style.height = `${Math.min(el.input.scrollHeight, 96)}px`;
  }

  function showAttachmentChip(filename, isUploading) {
    el.attachmentBox.hidden = false;
    el.attachmentBox.classList.toggle("is-uploading", isUploading);
    el.attachmentName.textContent = isUploading ? `Đang tải "${filename}"...` : filename;
  }

  function clearAttachmentChip() {
    el.attachmentBox.hidden = true;
    el.attachmentBox.classList.remove("is-uploading");
    el.attachmentName.textContent = "";
  }

  // ------------------------------------------------------------------
  // 4. Conversation logic
  // ------------------------------------------------------------------
  function resetConversation() {
    clearStoredHistory();
    state.history = [];
    state.pendingAttachment = null;
    clearAttachmentChip();
    el.messages.innerHTML = "";
    appendMessage("assistant", state.greeting);
    renderSuggestions();
  }

  // Replays a conversation restored from localStorage: the greeting bubble
  // still opens the panel (reminds the user what the bot can do), followed
  // by every saved turn. No renderSuggestions() here — the quick-start chips
  // are only for a conversation that hasn't started yet.
  function restoreConversation(history) {
    state.history = history;
    state.pendingAttachment = null;
    clearAttachmentChip();
    el.messages.innerHTML = "";
    appendMessage("assistant", state.greeting);
    for (const message of history) {
      const text = message.role === "user" ? toDisplayText(message.content) : message.content;
      appendMessage(message.role, text);
    }
    scrollToBottom();
  }

  async function loadGreetingAndAvailability() {
    try {
      const data = await ChatbotAPI.getGreeting();
      if (data && data.enabled === false) {
        state.enabled = false;
        el.widget.hidden = true;
        return;
      }
      if (data && typeof data.greeting === "string" && data.greeting.trim()) {
        state.greeting = data.greeting;
      }
    } catch (err) {
      // Keep the fallback greeting; the widget still opens and a real error
      // (if any) surfaces the first time the user actually sends a message.
    }
  }

  async function handleAttachFile(file) {
    if (!file) return;
    showAttachmentChip(file.name, true);
    try {
      const data = await ChatbotAPI.uploadFile(file);
      state.pendingAttachment = { upload_id: data.upload_id, filename: data.filename };
      showAttachmentChip(data.filename, false);
    } catch (err) {
      state.pendingAttachment = null;
      clearAttachmentChip();
      appendMessage("assistant", err.message || "Không tải được tệp lên.", { isError: true });
    }
  }

  async function handleSend(rawText) {
    const text = rawText.trim();
    const attachment = state.pendingAttachment;
    if (!text && !attachment) return;
    if (state.isSending) return;

    const messageText = text || `Xem giúp file mình vừa đính kèm: "${attachment.filename}".`;
    // The upload_id note is baked into the persisted history content itself
    // (not sent as a side-channel field) so it survives into later turns —
    // the model gets the full message list fresh on every request and would
    // otherwise "forget" the upload_id by the time the user confirms an
    // action in a follow-up message.
    const attachmentNote = attachment
      ? `\n\n[Tệp đính kèm]\n- "${attachment.filename}" (upload_id: ${attachment.upload_id})`
      : "";
    const historyContent = messageText + attachmentNote;

    removeSuggestions();
    appendMessage("user", toDisplayText(historyContent));
    state.history.push({ role: "user", content: historyContent });
    saveHistory();

    el.input.value = "";
    autoGrowInput();
    clearAttachmentChip();
    state.pendingAttachment = null;
    setSending(true);
    setTyping(true);

    try {
      const reply = await ChatbotAPI.sendMessage(state.history);
      setTyping(false);
      appendMessage("assistant", reply);
      state.history.push({ role: "assistant", content: reply });
      saveHistory();
    } catch (err) {
      setTyping(false);
      appendMessage("assistant", err.message || "Đã có lỗi xảy ra. Vui lòng thử lại.", {
        isError: true,
      });
    } finally {
      setSending(false);
      el.input.focus();
    }
  }

  // ------------------------------------------------------------------
  // 5. UI events
  // ------------------------------------------------------------------
  function openPanel() {
    if (state.mode === "workspace") return;
    state.isOpen = true;
    el.widget.classList.add("is-open");
    el.panel.hidden = false;
    el.toggle.setAttribute("aria-expanded", "true");
    el.input.focus();
  }

  function closePanel() {
    if (state.mode === "workspace") {
      window.location.hash = state.returnRoute || "#dashboard";
      return;
    }
    state.isOpen = false;
    el.widget.classList.remove("is-open");
    el.panel.hidden = true;
    el.toggle.setAttribute("aria-expanded", "false");
  }

  function enterWorkspace() {
    const host = document.getElementById("chatbot-page-host");
    if (!host) return;
    if (state.mode !== "workspace") {
      const currentRoute = window.location.hash || "#dashboard";
      if (!currentRoute.replace(/^#\/?/, "").startsWith("chatbot")) {
        state.returnRoute = currentRoute;
      }
    }
    state.mode = "workspace";
    state.isOpen = true;
    host.appendChild(el.panel);
    el.panel.hidden = false;
    el.panel.classList.add("is-workspace");
    el.widget.classList.add("is-page-mode");
    el.toggle.setAttribute("aria-expanded", "true");
    el.closeBtn.setAttribute("aria-label", "Thu nhỏ chatbot");
    el.closeBtn.setAttribute("title", "Thu nhỏ");
    el.input.focus({ preventScroll: true });
  }

  function leaveWorkspace() {
    if (state.mode !== "workspace") return;
    state.mode = "quick";
    state.isOpen = false;
    el.widget.appendChild(el.panel);
    el.panel.hidden = true;
    el.panel.classList.remove("is-workspace");
    el.widget.classList.remove("is-page-mode", "is-open");
    el.widget.hidden = !state.enabled;
    el.toggle.setAttribute("aria-expanded", "false");
    el.closeBtn.setAttribute("aria-label", "Đóng trợ lý");
    el.closeBtn.setAttribute("title", "Đóng");
  }

  function openWorkspace() {
    const currentRoute = window.location.hash || "#dashboard";
    if (!currentRoute.replace(/^#\/?/, "").startsWith("chatbot")) {
      state.returnRoute = currentRoute;
    }
    window.location.hash = "#chatbot";
  }

  el.toggle.addEventListener("click", () => {
    if (state.isOpen) closePanel();
    else openPanel();
  });

  el.closeBtn.addEventListener("click", closePanel);

  if (el.expandBtn) el.expandBtn.addEventListener("click", openWorkspace);

  el.refreshBtn.addEventListener("click", () => {
    el.refreshBtn.classList.add("is-spinning");
    resetConversation();
    setTimeout(() => el.refreshBtn.classList.remove("is-spinning"), 500);
  });

  el.form.addEventListener("submit", (event) => {
    event.preventDefault();
    handleSend(el.input.value);
  });

  el.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      handleSend(el.input.value);
    }
  });

  el.input.addEventListener("input", autoGrowInput);

  if (el.attachBtn && el.fileInput) {
    el.attachBtn.addEventListener("click", () => el.fileInput.click());
    el.fileInput.addEventListener("change", () => {
      const file = el.fileInput.files && el.fileInput.files[0];
      el.fileInput.value = ""; // allow re-selecting the same file later
      handleAttachFile(file);
    });
  }

  if (el.attachmentRemoveBtn) {
    el.attachmentRemoveBtn.addEventListener("click", () => {
      state.pendingAttachment = null;
      clearAttachmentChip();
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.isOpen && state.mode === "quick") closePanel();
  });

  window.addEventListener("dh:chatbot-route", (event) => {
    if (event.detail?.mode === "workspace") enterWorkspace();
    else leaveWorkspace();
  });

  window.addEventListener("hashchange", (event) => {
    const nextHash = new URL(event.newURL).hash.replace(/^#\/?/, "");
    const previousHash = new URL(event.oldURL).hash || "#dashboard";
    if (nextHash.startsWith("chatbot") && !previousHash.replace(/^#\/?/, "").startsWith("chatbot")) {
      state.returnRoute = previousHash;
    }
  });

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------
  async function init() {
    // Show the bubble optimistically so it never depends on a network round
    // trip; hidden again only if the server explicitly says it's disabled.
    el.widget.hidden = false;
    const storedHistory = loadStoredHistory();
    if (storedHistory) {
      // A saved conversation renders instantly, with no network wait — the
      // greeting/enabled check below still runs, but only in the background
      // to refine state.greeting for next time and to hide the widget if the
      // server reports it disabled; it never re-renders what's already shown.
      restoreConversation(storedHistory);
      loadGreetingAndAvailability();
    } else {
      await loadGreetingAndAvailability();
      resetConversation();
    }
    if (window.location.hash.replace(/^#\/?/, "").startsWith("chatbot")) {
      enterWorkspace();
    }
  }
  init();
})();
