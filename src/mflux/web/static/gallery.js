"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const { el } = MF;
  const PAGE = 60;
  let offset = 0;
  let current = null;

  async function load(reset = false) {
    if (reset) {
      offset = 0;
      $("gallery-grid").replaceChildren();
    }
    const data = await MF.api(`/api/gallery?offset=${offset}&limit=${PAGE}`);
    offset += data.items.length;
    $("gallery-count").textContent = `${data.total} image${data.total === 1 ? "" : "s"}`;
    $("gallery-more").hidden = offset >= data.total;
    for (const item of data.items) $("gallery-grid").append(card(item));
    if (!data.total) $("gallery-grid").replaceChildren(el("p", { class: "muted", text: "Nothing here yet. Generated images appear in this gallery." }));
  }

  function card(item) {
    return el("button", { class: "gallery-card", type: "button", title: item.prompt || item.name, onclick: () => open(item) },
      el("img", { src: `/api/images/${encodeURIComponent(item.name)}`, alt: item.prompt || item.name, loading: "lazy" }),
      el("span", { class: "caption", text: item.prompt || item.name }),
    );
  }

  async function open(item) {
    current = item;
    const src = `/api/images/${encodeURIComponent(item.name)}`;
    $("viewer-image").src = src;
    $("viewer-download").href = src;
    $("viewer-download").setAttribute("download", item.name);
    $("viewer-prompt").textContent = item.prompt || "";
    $("viewer-reuse").hidden = !item.reloadable;
    $("viewer-meta").replaceChildren();
    $("viewer").showModal();
    try {
      const { metadata, web } = await MF.api(`/api/images/${encodeURIComponent(item.name)}/metadata`);
      current.web = web;
      const rows = [["File", item.name], ["Command", web?.command]];
      if (metadata) {
        for (const key of ["model", "base_model", "seed", "steps", "guidance", "width", "height", "quantize", "scheduler", "lora_paths", "lora_scales", "image_path", "image_strength", "negative_prompt", "generation_time_seconds", "mflux_version"]) {
          const value = metadata[key];
          if (value !== undefined && value !== null && value !== "" && !(Array.isArray(value) && !value.length)) rows.push([key.replace(/_/g, " "), Array.isArray(value) ? value.join(", ") : String(value)]);
        }
      }
      $("viewer-meta").replaceChildren(...rows.filter(([, v]) => v).flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
    } catch (exc) {
      MF.toast(exc.message);
    }
  }

  function reuse() {
    if (!current?.web?.payload) return;
    const payload = { ...current.web.payload, seeds: [String(current.web.seed)], image: null };
    try { localStorage.setItem("mflux-web:reuse", JSON.stringify(payload)); } catch { /* ignore */ }
    window.location.href = "/";
  }

  async function remove() {
    if (!current || !window.confirm(`Delete ${current.name}? This removes the image and its metadata.`)) return;
    await MF.api(`/api/images/${encodeURIComponent(current.name)}`, { method: "DELETE" });
    $("viewer").close();
    load(true);
  }

  $("gallery-more").addEventListener("click", () => load());
  $("viewer-reuse").addEventListener("click", reuse);
  $("viewer-delete").addEventListener("click", remove);
  $("viewer").addEventListener("click", (e) => { if (e.target === $("viewer")) $("viewer").close(); });
  MF.ready.then(() => load(true)).catch((exc) => MF.toast(exc.message));
})();
