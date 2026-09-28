"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const panel = $("prompt-chat"), input = $("chat-input"), model = $("chat-model");
  const provider = $("chat-provider");
  let messages = [], config = null, controller = null, opener = null;
  let preferences = MFChatPreferences.read();
  let pendingPreferences = null;
  const conversations = {};

  function providerConfig() { return config?.providers[preferences.provider]; }

  function showProvider() {
    provider.value = preferences.provider;
    const selected = providerConfig();
    model.replaceChildren(...(selected?.models || []).map((m) => MF.el("option", { value: m.id, text: m.name })));
    model.value = preferences.provider === "openai" ? preferences.model : selected?.default_model || "";
    $("chat-key-status").textContent = selected?.configuration_error || (selected?.configured ? "API key detected on server" : "Configuration missing. See Settings → Prompt assistant for setup.");
    $("chat-provider-info").textContent = `${preferences.provider === "omlx" ? "oMLX server" : "OpenAI API · API charges apply"}. Messages are sent when you press Send. Each provider has its own chat; both clear on reload.`;
    busy(Boolean(controller));
  }

  function applyPreferences(next) {
    if (next.provider !== preferences.provider) {
      conversations[preferences.provider] = { messages, draft: input.value, nodes: [...$("chat-messages").childNodes] };
      const saved = conversations[next.provider];
      messages = saved?.messages || [];
      input.value = saved?.draft || "";
      $("chat-messages").replaceChildren(...(saved?.nodes || []));
      $("chat-status").textContent = "";
      error();
    }
    preferences = next;
    if (!next.enabled) panel.hidden = true;
    showProvider();
  }

  function syncPreferences() {
    const next = MFChatPreferences.read();
    document.querySelectorAll("[data-chat-launcher]").forEach((button) => { button.hidden = !next.enabled; });
    if (controller) {
      pendingPreferences = next;
      if (!next.enabled) panel.hidden = true;
      controller.abort();
    } else applyPreferences(next);
  }

  function error(message = "") {
    $("chat-error").textContent = message;
    $("chat-error").hidden = !message;
  }

  function busy(active) {
    $("chat-send").disabled = active || !preferences.enabled || !providerConfig()?.configured;
    $("chat-stop").hidden = !active;
    $("chat-clear").disabled = active;
    model.disabled = active || preferences.provider === "omlx";
    provider.disabled = active;
    input.disabled = active;
    panel.querySelectorAll("[data-chat-action], #chat-use-current").forEach((button) => { button.disabled = active; });
  }

  function addMessage(role, text, name = "") {
    const body = MF.el("div", { class: "chat-message-text", text });
    const article = MF.el("article", { class: `chat-message ${role}` }, MF.el("strong", { text: role === "user" ? "You" : name }), body);
    $("chat-messages").append(article);
    return { article, body };
  }

  function insertPrompt(text) {
    const target = document.querySelector('[data-flag="--prompt"]');
    if (!target) { error("The current form has no prompt field."); return; }
    if (target.value.trim() && !window.confirm("Replace the current generation prompt with this suggestion?")) return;
    target.value = text;
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.dispatchEvent(new Event("change", { bubbles: true }));
    MF.toast("Prompt inserted");
  }

  function renderReply(body, text) {
    body.replaceChildren();
    const blocks = /```prompt\s*\n([\s\S]*?)```/g;
    let position = 0;
    for (const match of text.matchAll(blocks)) {
      body.append(MF.el("span", { text: text.slice(position, match.index) }));
      const prompt = match[1].trim();
      body.append(MF.el("pre", { class: "chat-prompt", text: prompt }));
      if (prompt) body.append(MF.el("button", { type: "button", class: "ghost small", text: "Use this prompt", onclick: () => insertPrompt(prompt) }));
      position = match.index + match[0].length;
    }
    body.append(MF.el("span", { text: text.slice(position) }));
  }

  function useCurrent(action = "Let's discuss this prompt") {
    const prompt = document.querySelector('[data-flag="--prompt"]')?.value.trim();
    if (!prompt) { error("Write a generation prompt first, or describe an idea in the chat."); return; }
    const family = $("command").selectedOptions[0]?.textContent || "";
    const draft = `${action}.\nImage model family: ${family}\n\n${prompt}`;
    if (draft.length > input.maxLength) { error("The current prompt is too long for chat. Copy a shorter excerpt."); return; }
    if (input.value.trim() && !window.confirm("Replace the chat draft with the current generation prompt?")) return;
    input.value = draft;
    error();
    input.focus();
  }

  async function send(event) {
    event.preventDefault();
    if (controller || !preferences.enabled || !providerConfig()?.configured) return;
    const text = input.value.trim();
    if (!text) return;
    const pending = [...messages, { role: "user", content: text }];
    if (text.length > config.max_text || pending.length > config.max_messages || pending.reduce((sum, m) => sum + m.content.length, preferences.instructions.length) > config.max_total) {
      error("This conversation is too long. Clear chat or shorten the message before continuing."); return;
    }
    error();
    const user = addMessage("user", text);
    const reply = addMessage("assistant", "", model.selectedOptions[0].textContent);
    controller = new AbortController();
    busy(true);
    $("chat-status").textContent = "Thinking…";
    let response = "", completed = false;
    try {
      await MF.stream("/api/chat", { provider: preferences.provider, model: model.value, instructions: preferences.instructions, messages: pending }, controller.signal, (item) => {
        if (item.type === "error") throw new Error(item.message);
        if (item.type === "delta") {
          response += item.text;
          reply.body.textContent = response;
          $("chat-status").textContent = "Replying…";
          $("chat-messages").scrollTop = $("chat-messages").scrollHeight;
        }
        if (item.type === "done") {
          completed = true;
          $("chat-status").textContent = item.usage.input_tokens == null || item.usage.output_tokens == null
            ? "Reply complete." : `${item.usage.input_tokens} input · ${item.usage.output_tokens} output tokens`;
        }
      });
      if (!completed || !response.trim()) throw new Error("No complete reply was received. Please try again.");
      messages = [...pending, { role: "assistant", content: response }];
      input.value = "";
      renderReply(reply.body, response);
    } catch (exc) {
      $("chat-status").textContent = exc.name === "AbortError" ? "Stopped. Your message is ready to resend." : "Reply failed. Your message is ready to resend.";
      if (exc.name !== "AbortError") error(exc.message);
      if (!response) { user.article.remove(); reply.article.remove(); }
      else reply.article.append(MF.el("small", { class: "hint", text: "Incomplete reply · excluded from conversation context" }));
    } finally {
      controller = null;
      if (pendingPreferences) { applyPreferences(pendingPreferences); pendingPreferences = null; }
      busy(false);
      if (!panel.hidden) input.focus();
    }
  }

  async function open() {
    if (!MFChatPreferences.read().enabled) return;
    opener = document.activeElement;
    panel.hidden = false;
    input.focus();
    if (controller) return;
    try {
      await MF.ready;
      config = await MF.api("/api/chat/config");
      applyPreferences(MFChatPreferences.read());
    } catch (exc) { error(exc.message); }
  }

  function close() { panel.hidden = true; opener?.focus(); }
  window.addEventListener("mflux-open-chat", open);
  window.addEventListener("mflux-chat-preferences", syncPreferences);
  $("chat-close").addEventListener("click", close);
  panel.addEventListener("keydown", (event) => { if (event.key === "Escape") close(); });
  $("chat-form").addEventListener("submit", send);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); $("chat-form").requestSubmit(); }
  });
  $("chat-stop").addEventListener("click", () => controller?.abort());
  $("chat-clear").addEventListener("click", () => {
    messages = []; $("chat-messages").replaceChildren(); $("chat-status").textContent = "Chat cleared."; error();
  });
  $("chat-use-current").addEventListener("click", () => useCurrent());
  panel.querySelectorAll("[data-chat-action]").forEach((button) => button.addEventListener("click", () => useCurrent(button.dataset.chatAction)));
  model.addEventListener("change", () => {
    preferences.model = model.value;
    if (!MFChatPreferences.save(preferences)) MF.toast("Model selected for this page; browser storage is unavailable.");
  });
  provider.addEventListener("change", () => {
    if (!MFChatPreferences.save({ ...preferences, provider: provider.value })) MF.toast("Provider selected for this page; browser storage is unavailable.");
  });
  window.addEventListener("pagehide", () => controller?.abort());
})();
