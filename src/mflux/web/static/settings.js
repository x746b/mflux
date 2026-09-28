"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const theme = $("appearance-theme"), accent = $("appearance-accent");
  let chatConfig = null;

  function showAssistant() {
    const provider = $("assistant-provider").value;
    const enabled = $("assistant-enabled").checked;
    const config = chatConfig?.providers[provider];
    $("assistant-openai-model").hidden = provider !== "openai";
    $("assistant-omlx-model").hidden = provider !== "omlx";
    $("assistant-omlx-model").textContent = config?.default_model ? `oMLX model: ${config.default_model}` : "Set OMLX_MFLUX_MODEL on the server.";
    $("assistant-provider-status").textContent = config?.configuration_error || (config
      ? config.configured ? "Configuration detected on server" : "API key missing. See setup instructions below."
      : "Configuration status unavailable");
    for (const id of ["assistant-provider", "assistant-model", "assistant-instructions", "assistant-save"]) $(id).disabled = !enabled;
  }

  function syncAssistant() {
    const preferences = MFChatPreferences.read();
    $("assistant-enabled").checked = preferences.enabled;
    $("assistant-provider").value = preferences.provider;
    $("assistant-model").value = preferences.model;
    $("assistant-instructions").value = preferences.instructions;
    showAssistant();
  }

  function saveAssistant() {
    const saved = MFChatPreferences.save({
      enabled: $("assistant-enabled").checked,
      provider: $("assistant-provider").value,
      model: $("assistant-model").value,
      instructions: $("assistant-instructions").value,
    });
    $("assistant-feedback").textContent = saved ? "Assistant preferences saved." : "Applied for this page; browser storage is unavailable.";
  }

  function syncAppearance() {
    const current = MFAppearance.get();
    theme.value = current.theme;
    accent.value = current.accent;
  }

  function changeAppearance() {
    const saved = MFAppearance.save({ theme: theme.value, accent: accent.value });
    $("appearance-feedback").textContent = saved ? "Appearance saved." : "Applied for this page. Browser storage is unavailable, so this preference could not be saved.";
  }

  async function refresh() {
    const button = $("settings-refresh");
    button.disabled = true;
    $("settings-error").hidden = true;
    try {
      await MF.ready;
      const [{ huggingface: hf, runtime }, chat] = await Promise.all([MF.api("/api/settings"), MF.api("/api/chat/config")]);
      chatConfig = chat;
      showAssistant();
      $("hf-status").textContent = hf.status === "detected"
        ? `Token detected · ${hf.source === "environment" ? "environment variable" : "saved Hugging Face login"}`
        : hf.status === "unreadable" ? "Unable to read the saved Hugging Face token. Check its file permissions on the server."
          : "No token detected";
      $("hf-setup").open = hf.status !== "detected";
      const paths = (values) => values.length ? values.join("\n") : "Not configured";
      const gb = (value) => `${Number(value).toFixed(1)} GiB`;
      const items = [
        ["Loaded models", runtime.cached_models.map((model) => model.model).join("\n") || "None loaded"],
        ["Model directories", paths(runtime.models_dirs)],
        ["LoRA directories", paths(runtime.lora_dirs)],
        ["Output directory", runtime.output_dir],
        ["Active memory budget", gb(runtime.max_memory_gb)],
        ["Buffer cache cap", gb(runtime.max_cache_gb)],
        ["Idle unload", runtime.idle_unload_minutes === 0 ? "Disabled" : `${runtime.idle_unload_minutes} minutes`],
      ];
      $("runtime-info").replaceChildren(...items.flatMap(([label, value]) => [MF.el("dt", { text: label }), MF.el("dd", { text: value })]));
    } catch (error) {
      $("hf-status").textContent = "Status unavailable";
      $("assistant-provider-status").textContent = "Status unavailable";
      $("settings-error").textContent = error.message;
      $("settings-error").hidden = false;
    } finally { button.disabled = false; }
  }

  syncAppearance();
  syncAssistant();
  $("assistant-save").addEventListener("click", saveAssistant);
  $("assistant-enabled").addEventListener("change", saveAssistant);
  $("assistant-provider").addEventListener("change", saveAssistant);
  window.addEventListener("mflux-chat-preferences", syncAssistant);
  theme.addEventListener("change", changeAppearance);
  accent.addEventListener("change", changeAppearance);
  window.addEventListener("mflux-appearance", syncAppearance);
  $("settings-refresh").addEventListener("click", refresh);
  refresh();
})();
