"use strict";

const MFAppearance = (() => {
  const KEY = "mflux-web:appearance";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let current = { theme: "system", accent: "orange" };

  function apply(value) {
    current = {
      theme: ["system", "light", "dark"].includes(value?.theme) ? value.theme : "system",
      accent: ["orange", "blue", "teal"].includes(value?.accent) ? value.accent : "orange",
    };
    document.documentElement.dataset.theme = current.theme === "system" ? (system.matches ? "dark" : "light") : current.theme;
    document.documentElement.dataset.accent = current.accent;
    window.dispatchEvent(new Event("mflux-appearance"));
  }

  function read() {
    try { return JSON.parse(localStorage.getItem(KEY) || "null"); }
    catch { return null; }
  }

  function save(value) {
    apply(value);
    try {
      localStorage.setItem(KEY, JSON.stringify(current));
      return true;
    } catch { return false; }
  }

  apply(read());
  system.addEventListener("change", () => apply(current));
  window.addEventListener("storage", (event) => { if (event.key === KEY || event.key === null) apply(read()); });
  return { get: () => ({ ...current }), save };
})();
