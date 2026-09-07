// Floating AI assistant widget (chatbot bubble).
//
// Self-contained IIFE, independent of app.js. Structure:
//   1. API layer   — the only place that talks to the backend.
//   2. State       — in-memory conversation history + pending attachment.
//   3. Rendering   — DOM/markdown output helpers.
//   4. Logic       — conversation flow (send, reset, attach file).
//   5. UI events   — wiring buttons/inputs to the logic above.
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
  };

  // ------------------------------------------------------------------
  // DOM refs
  // ------------------------------------------------------------------
  const el = {
    widget: document.getElementById("chatbot-widget"),
    toggle: document.getElementById("chatbot-toggle"),
    panel: document.getElementById("chatbot-panel"),
    closeBtn: document.getElementById("chatbot-close"),
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
    state.history = [];
    state.pendingAttachment = null;
    clearAttachmentChip();
    el.messages.innerHTML = "";
    appendMessage("assistant", state.greeting);
    renderSuggestions();
  }

  async function loadGreetingAndAvailability() {
    // Show the bubble optimistically so it never depends on a network round
    // trip; hide it again only if the server explicitly says it's disabled.
    el.widget.hidden = false;
    try {
      const data = await ChatbotAPI.getGreeting();
      if (data && data.enabled === false) {
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
    resetConversation();
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
    const displayText = attachment ? `${messageText}\n\n📎 Đã đính kèm: ${attachment.filename}` : messageText;

    removeSuggestions();
    appendMessage("user", displayText);
    state.history.push({ role: "user", content: historyContent });

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
    state.isOpen = true;
    el.widget.classList.add("is-open");
    el.panel.hidden = false;
    el.toggle.setAttribute("aria-expanded", "true");
    el.input.focus();
  }

  function closePanel() {
    state.isOpen = false;
    el.widget.classList.remove("is-open");
    el.panel.hidden = true;
    el.toggle.setAttribute("aria-expanded", "false");
  }

  el.toggle.addEventListener("click", () => {
    if (state.isOpen) closePanel();
    else openPanel();
  });

  el.closeBtn.addEventListener("click", closePanel);

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
    if (event.key === "Escape" && state.isOpen) closePanel();
  });

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------
  loadGreetingAndAvailability();
})();
