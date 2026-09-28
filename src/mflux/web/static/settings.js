"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const theme = $("appearance-theme"), accent = $("appearance-accent");

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
      $("openai-status").textContent = chat.configured ? "API key detected on server · ready to chat" : "No API key detected. See setup instructions below.";
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
      $("openai-status").textContent = "Status unavailable";
      $("settings-error").textContent = error.message;
      $("settings-error").hidden = false;
    } finally { button.disabled = false; }
  }

  syncAppearance();
  const preferences = MFChatPreferences.read();
  $("assistant-model").value = preferences.model;
  $("assistant-instructions").value = preferences.instructions;
  $("assistant-save").addEventListener("click", () => {
    const saved = MFChatPreferences.save({ model: $("assistant-model").value, instructions: $("assistant-instructions").value });
    $("assistant-feedback").textContent = saved ? "Assistant preferences saved." : "Browser storage is unavailable; preferences could not be saved.";
  });
  theme.addEventListener("change", changeAppearance);
  accent.addEventListener("change", changeAppearance);
  window.addEventListener("mflux-appearance", syncAppearance);
  $("settings-refresh").addEventListener("click", refresh);
  refresh();
})();
