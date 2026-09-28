"use strict";

const MFChatPreferences = (() => {
  const KEY = "mflux-web:chat-preferences";
  function read() {
    let value;
    try { value = JSON.parse(localStorage.getItem(KEY) || "null"); } catch { /* defaults */ }
    return {
      model: ["gpt-6-luna", "gpt-6-sol"].includes(value?.model) ? value.model : "gpt-6-luna",
      instructions: typeof value?.instructions === "string" ? value.instructions.slice(0, 4000) : "",
    };
  }
  function save(value) {
    try { localStorage.setItem(KEY, JSON.stringify(value)); return true; }
    catch { return false; }
  }
  return { read, save };
})();
