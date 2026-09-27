"use strict";

const MF = (() => {
  let csrf = null;
  let session = null;

  async function loadSession() {
    const response = await fetch("/api/session", { credentials: "same-origin" });
    session = await response.json();
    csrf = session.csrf;
    return session;
  }

  async function api(path, { method = "GET", json, form, redirectOn401 = true } = {}) {
    if (method !== "GET" && csrf === null) await loadSession();
    const headers = {};
    let body;
    if (json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(json);
    } else if (form !== undefined) {
      body = form;
    }
    if (method !== "GET") headers["X-MFlux-CSRF"] = csrf;
    const response = await fetch(path, { method, headers, body, credentials: "same-origin" });
    if (response.status === 401 && redirectOn401) {
      window.location.href = "/login";
      throw new Error("Authentication required");
    }
    const type = response.headers.get("content-type") || "";
    const data = type.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      const detail = data && data.detail;
      throw new Error(typeof detail === "string" ? detail : `Request failed (${response.status})`);
    }
    return data;
  }

  function toast(message, ms = 2600) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, ms);
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (value === true) node.setAttribute(key, "");
      else node.setAttribute(key, value);
    }
    for (const child of children) {
      if (child !== null && child !== undefined) node.append(child);
    }
    return node;
  }

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied to clipboard");
    } catch {
      window.prompt("Copy this command:", text);
    }
  }

  async function pollStatus() {
    const pill = document.getElementById("worker-status");
    if (!pill || document.hidden) return;
    try {
      const status = await api("/api/status");
      const memory = status.memory || {};
      const model = status.cached_models.length ? status.cached_models[0].model : null;
      let label = "idle";
      if (status.loading) label = `loading ${status.loading}`;
      else if (status.current_job) label = status.queued ? `busy · ${status.queued} queued` : "busy";
      else if (model) label = `ready · ${model}`;
      if (memory.active_gb !== undefined) label += ` · ${memory.active_gb.toFixed(1)} GB`;
      pill.textContent = label;
      const details = [`MLX memory in use: ${memory.active_gb} GB`, `MLX buffer cache: ${memory.cache_gb} GB`];
      details.push(`Active memory budget: ${status.max_memory_gb} GB`, `Buffer cache cap: ${status.max_cache_gb} GB`);
      if (model && status.unload_in !== null) details.push(`Unloads after ${Math.ceil(status.unload_in / 60)} more idle min`);
      pill.title = details.join("\n");
      pill.classList.toggle("busy", Boolean(status.loading || status.current_job));
      const unload = document.getElementById("unload-model");
      if (unload) unload.hidden = !model || Boolean(status.loading || status.current_job);
    } catch { /* the next poll retries */ }
  }

  async function unloadModel() {
    try {
      await api("/api/models/unload", { method: "POST" });
      toast("Unloading model…");
      setTimeout(pollStatus, 800);
    } catch (exc) {
      toast(exc.message);
    }
  }

  async function init() {
    const current = await loadSession();
    const logout = document.getElementById("logout");
    if (logout && current.auth_required && current.authenticated) {
      logout.hidden = false;
      logout.addEventListener("click", async () => {
        await api("/api/logout", { method: "POST" });
        window.location.href = "/login";
      });
    }
    const unload = document.getElementById("unload-model");
    if (unload) unload.addEventListener("click", unloadModel);
    if (document.getElementById("worker-status") && current.authenticated) {
      pollStatus();
      setInterval(pollStatus, 3000);
    }
    return current;
  }

  const ready = init();
  return { api, toast, el, copy, ready, loadSession };
})();
