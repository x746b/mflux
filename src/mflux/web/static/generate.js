"use strict";

(() => {
  const STORAGE_PREFIX = "mflux-web:";
  const REUSE_KEY = `${STORAGE_PREFIX}reuse`;
  const LAST_COMMAND_KEY = `${STORAGE_PREFIX}last-command`;
  const SPECIAL_FLAGS = new Set(["--model", "--lora", "--image", "--image-paths", "--seed"]);
  const SIZE_PRESETS = [
    ["1:1", 1024, 1024], ["4:3", 1152, 864], ["3:4", 864, 1152],
    ["3:2", 1216, 832], ["2:3", 832, 1216], ["16:9", 1344, 768], ["9:16", 768, 1344],
  ];
  const ASPECT_RATIOS = {
    Square: [["1:1", 1, 1]],
    Landscape: [
      ["5:4", 5, 4], ["4:3", 4, 3], ["3:2", 3, 2], ["16:10", 16, 10], ["16:9", 16, 9],
      ["1.85:1 · cinema", 1.85, 1], ["2:1", 2, 1], ["21:9 · ultrawide", 21, 9],
      ["2.39:1 · anamorphic", 2.39, 1], ["3:1 · panorama", 3, 1], ["32:9 · super ultrawide", 32, 9],
    ],
    Portrait: [
      ["4:5", 4, 5], ["3:4", 3, 4], ["2:3", 2, 3], ["10:16", 10, 16], ["9:16", 9, 16],
      ["1:2", 1, 2], ["9:21", 9, 21],
    ],
  };
  const MEGAPIXELS = [0.5, 1, 1.5, 2, 3, 4];
  const BASIC_ORDER = ["--prompt", "--negative-prompt", "--width", "--height", "--steps", "--guidance"];
  const MAX_DIMENSION = 8192;
  const $ = (id) => document.getElementById(id);
  const { el } = MF;

  const state = { commands: [], localModels: [], localLoras: [], command: null, upload: null, references: [], uploading: false, uploadEpoch: 0, activeJob: null, source: null, lastSeq: -1 };

  function storage(action, key, value) {
    try {
      if (action === "get") return JSON.parse(localStorage.getItem(key) || "null");
      if (action === "set") localStorage.setItem(key, JSON.stringify(value));
      if (action === "remove") localStorage.removeItem(key);
    } catch { /* storage may be unavailable */ }
    return null;
  }

  function commandSpec(name) {
    return state.commands.find((c) => c.command === name);
  }

  // ---------- rendering ----------

  function renderCommandSelect() {
    const select = $("command");
    select.replaceChildren(...state.commands.filter((c) => c.available).map((c) => el("option", { value: c.command, text: prettyCommand(c.command) })));
  }

  function prettyCommand(name) {
    return name.replace(/^mflux-generate-?/, "").replace(/-/g, " ").trim() || "flux (dev / schnell)";
  }

  function renderCommand(name) {
    const spec = commandSpec(name);
    state.command = spec;
    $("command").value = name;
    $("command-description").textContent = spec.description || "";

    $("model-builtin").replaceChildren(...spec.models.map((m) => el("option", { value: m.name, text: m.name })));
    $("model-local").replaceChildren(...state.localModels.map((m) => el("option", { value: m.path, text: m.name })));
    $("model-local-empty").hidden = state.localModels.length > 0;

    for (const group of ["basic", "model", "memory", "advanced"]) $(`group-${group}`).replaceChildren();
    const rank = (flag) => (BASIC_ORDER.includes(flag) ? BASIC_ORDER.indexOf(flag) : BASIC_ORDER.length);
    const fields = [...spec.fields].sort((a, b) => rank(a.flag) - rank(b.flag));
    for (const field of fields) {
      if (SPECIAL_FLAGS.has(field.flag)) continue;
      const container = $(`group-${field.group}`);
      if (container) container.append(renderField(field));
      if (field.flag === "--height") container.append(renderSizePicker());
    }
    const hasImage = spec.fields.some((f) => f.flag === "--image");
    const hasLora = spec.fields.some((f) => f.flag === "--lora");
    $("img2img-section").hidden = !hasImage;
    $("references-section").hidden = !spec.fields.some((f) => f.flag === "--image-paths");
    $("lora-section").hidden = !hasLora;
    $("lora-rows").replaceChildren();
    $("lora-options").replaceChildren(...state.localLoras.map((l) => el("option", { value: l.path, text: l.name })));
    updateModelSource();
    updateStepsPlaceholder();
    updatePresetHighlight();
  }

  function renderField(field) {
    const id = `field-${field.flag.replace(/[^a-z0-9]/gi, "-")}`;
    const status = field.status === "conditional" ? el("span", { class: "status", title: field.condition, text: "conditional" }) : null;
    const hint = field.help ? el("small", { class: "hint", text: field.help }) : null;
    const common = { id, "data-flag": field.flag, "data-widget": field.widget, title: field.condition || "" };

    if (field.widget === "checkbox") {
      const input = el("input", { ...common, type: "checkbox" });
      input.checked = field.default === true;
      return el("label", { class: "check wide", title: field.help }, input, el("span", { text: field.label }), status);
    }

    let input;
    if (field.widget === "textarea") {
      input = el("textarea", { ...common, placeholder: field.flag === "--prompt" ? "Describe the image" : "" });
    } else if (field.widget === "select") {
      const defaultLabel = field.default === null || field.default === undefined ? "Default" : `Default (${field.default})`;
      input = el("select", common, el("option", { value: "", text: defaultLabel }), ...field.choices.map((c) => el("option", { value: c, text: c })));
    } else if (field.widget === "number") {
      input = el("input", { ...common, type: "number", step: field.type === "int" ? "1" : "any", placeholder: field.default ?? "auto" });
    } else {
      input = el("input", { ...common, placeholder: field.default ?? "auto" });
    }
    const wide = field.widget === "textarea" || field.flag === "--base-model" ? " wide" : "";
    return el("label", { class: `field${wide}`, "data-field": field.flag }, el("span", { class: "label" }, el("span", { text: field.label }), status), input, field.widget === "textarea" ? null : hint);
  }

  function renderSizePicker() {
    const chips = el("div", { class: "presets", id: "size-presets" });
    for (const [label, w, h] of SIZE_PRESETS) {
      chips.append(el("button", { type: "button", "data-w": w, "data-h": h, text: `${label} · ${w}×${h}`, onclick: () => applySize(w, h) }));
    }
    const ratio = el("select", { id: "aspect-ratio", "aria-label": "Aspect ratio" });
    const megapixels = el("select", { id: "megapixels", "aria-label": "Resolution" },
      ...MEGAPIXELS.map((mp) => el("option", { value: mp, text: `~${mp} MP`, selected: mp === 1 })));
    ratio.addEventListener("change", applyRatio);
    megapixels.addEventListener("change", () => {
      renderRatioOptions();
      applyRatio();
    });
    const picker = el("div", { class: "size-picker" },
      chips,
      el("div", { class: "size-selects" },
        el("label", { class: "field" }, el("span", { class: "label", text: "More sizes" }), ratio),
        el("label", { class: "field" }, el("span", { class: "label", text: "Resolution" }), megapixels),
      ),
    );
    requestAnimationFrame(renderRatioOptions);
    return picker;
  }

  function ratioDimensions(ratioW, ratioH, megapixels) {
    const step = state.command.dimension_step || 16;
    const snap = (value) => Math.min(MAX_DIMENSION, Math.max(step, Math.round(value / step) * step));
    const width = Math.sqrt((megapixels * 1e6 * ratioW) / ratioH);
    return [snap(width), snap((width * ratioH) / ratioW)];
  }

  function renderRatioOptions() {
    const select = $("aspect-ratio");
    if (!select) return;
    const mp = Number($("megapixels").value);
    const current = select.value;
    select.replaceChildren(el("option", { value: "", text: "Custom / choose a ratio…" }));
    for (const [group, ratios] of Object.entries(ASPECT_RATIOS)) {
      select.append(el("optgroup", { label: group }, ...ratios.map(([label, rw, rh]) => {
        const [w, h] = ratioDimensions(rw, rh, mp);
        return el("option", { value: `${rw}:${rh}`, text: `${label} — ${w}×${h}` });
      })));
    }
    select.value = current;
    updatePresetHighlight();
  }

  function applyRatio() {
    const value = $("aspect-ratio").value;
    if (!value) return;
    const [rw, rh] = value.split(":").map(Number);
    const [w, h] = ratioDimensions(rw, rh, Number($("megapixels").value));
    applySize(w, h, value);
  }

  function applySize(width, height, ratio = "") {
    setValue("--width", String(width));
    setValue("--height", String(height));
    if ($("aspect-ratio")) $("aspect-ratio").value = ratio;
    updatePresetHighlight();
  }

  function updatePresetHighlight() {
    const w = getValue("--width"), h = getValue("--height");
    document.querySelectorAll("#size-presets button").forEach((b) => b.classList.toggle("active", b.dataset.w === w && b.dataset.h === h));
    const ratio = $("aspect-ratio");
    if (ratio && ratio.value) {
      const [rw, rh] = ratio.value.split(":").map(Number);
      const [ew, eh] = ratioDimensions(rw, rh, Number($("megapixels").value));
      if (String(ew) !== w || String(eh) !== h) ratio.value = "";
    }
  }

  function fieldInput(flag) {
    return document.querySelector(`[data-flag="${CSS.escape(flag)}"]`);
  }

  function getValue(flag) {
    const input = fieldInput(flag);
    if (!input) return undefined;
    return input.type === "checkbox" ? input.checked : input.value;
  }

  function setValue(flag, value) {
    const input = fieldInput(flag);
    if (!input) return;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value === null || value === undefined ? "" : String(value);
  }

  function modelSource() {
    return document.querySelector('input[name="model-source"]:checked').value;
  }

  function updateModelSource() {
    const source = modelSource();
    document.querySelectorAll(".source-input").forEach((node) => { node.hidden = node.dataset.source !== source; });
    $("model-local-empty").hidden = source !== "local" || state.localModels.length > 0;
    const baseField = document.querySelector('[data-field="--base-model"]');
    if (baseField) baseField.hidden = source === "builtin";
    updateStepsPlaceholder();
  }

  function updateStepsPlaceholder() {
    const steps = fieldInput("--steps");
    if (!steps || !state.command) return;
    const source = modelSource();
    const name = source === "builtin" ? $("model-builtin").value : (getValue("--base-model") || state.command.models[0]?.name);
    const model = state.command.models.find((m) => m.name === name) || state.command.models[0];
    steps.placeholder = model ? `${model.default_steps} (model default)` : "auto";
  }

  function addLoraRow(path = "", scale = 1.0) {
    const row = el("div", { class: "lora-row" },
      el("input", { class: "lora-path", list: "lora-options", placeholder: "path, org/repo, or name", value: path }),
      el("input", { class: "lora-scale", type: "number", step: "0.05", min: "-4", max: "4", value: scale }),
      el("button", { type: "button", class: "ghost small", text: "Remove", onclick: () => { row.remove(); updateNotes(); } }),
    );
    $("lora-rows").append(row);
    updateNotes();
  }

  function updateNotes() {
    const count = document.querySelectorAll(".lora-row").length;
    $("lora-note").textContent = count ? `${count} active` : "";
    $("img2img-note").textContent = state.upload ? "image set" : "";
  }

  // ---------- payload ----------

  function collectPayload() {
    const options = {};
    document.querySelectorAll("[data-flag]").forEach((input) => {
      const flag = input.dataset.flag;
      if (input.closest("[hidden]")) return;
      const widget = input.dataset.widget;
      if (widget === "checkbox") options[flag] = input.checked;
      else if (widget === "number") { if (input.value !== "") options[flag] = Number(input.value); }
      else if (input.value !== "") options[flag] = input.value;
    });
    const source = modelSource();
    const modelValue = { builtin: $("model-builtin").value, local: $("model-local").value, hf: $("model-hf").value.trim() }[source];
    const seedsText = $("seeds").value.trim();
    const seeds = seedsText ? seedsText.split(/[\s,]+/).filter(Boolean) : [];
    const loras = [...document.querySelectorAll(".lora-row")]
      .map((row) => ({ path: row.querySelector(".lora-path").value.trim(), scale: Number(row.querySelector(".lora-scale").value || 1) }))
      .filter((l) => l.path);
    return {
      command: state.command.command,
      model: modelValue ? { source, value: modelValue } : null,
      options,
      seeds,
      loras: $("lora-section").hidden ? [] : loras,
      image: state.upload && !$("img2img-section").hidden ? { upload: state.upload, strength: Number($("init-strength").value) } : null,
      references: $("references-section").hidden ? [] : state.references.map((ref) => ref.id),
      preview_every: Number($("preview-every").value),
    };
  }

  function applyPayload(payload) {
    if (!commandSpec(payload.command)) return;
    renderCommand(payload.command);
    if (payload.model) {
      const radio = document.querySelector(`input[name="model-source"][value="${payload.model.source}"]`);
      if (radio) radio.checked = true;
      ({ builtin: $("model-builtin"), local: $("model-local"), hf: $("model-hf") })[payload.model.source].value = payload.model.value;
    }
    for (const [flag, value] of Object.entries(payload.options || {})) setValue(flag, value);
    $("seeds").value = (payload.seeds || []).join(", ");
    for (const lora of payload.loras || []) addLoraRow(lora.path, lora.scale);
    $("preview-every").value = String(payload.preview_every || 0);
    updateModelSource();
    updatePresetHighlight();
  }

  function saveForm() {
    if (!state.command || state.skipSave) return;
    const payload = collectPayload();
    delete payload.image;
    delete payload.references;
    storage("set", `${STORAGE_PREFIX}form:${payload.command}`, payload);
    storage("set", LAST_COMMAND_KEY, payload.command);
  }

  // ---------- init image ----------

  async function postUpload(file) {
    const form = new FormData();
    form.append("file", file);
    return MF.api("/api/uploads", { method: "POST", form });
  }

  async function uploadImage(file) {
    if (!file || state.uploading) return;
    const epoch = state.uploadEpoch;
    state.uploading = true;
    $("dropzone-text").textContent = "Uploading…";
    try {
      const result = await postUpload(file);
      if (epoch === state.uploadEpoch) setUpload(result.id, URL.createObjectURL(file));
    } catch (exc) {
      showError(exc.message);
    } finally {
      state.uploading = false;
      $("dropzone-text").textContent = "Drop an image or click to choose";
    }
  }

  function setUpload(id, previewUrl) {
    state.upload = id;
    const img = $("init-preview");
    if (img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
    img.hidden = !id;
    if (previewUrl) img.src = previewUrl;
    else img.removeAttribute("src");
    $("dropzone-text").hidden = Boolean(id);
    $("dropzone-text").textContent = "Drop an image or click to choose";
    if (!id) $("init-image").value = "";
    updateNotes();
  }

  async function uploadReferences(files) {
    if (state.uploading || !files.length) return;
    const limit = state.command.max_references;
    if (state.references.length + files.length > limit) {
      showError(`Choose at most ${limit} reference images in total.`);
      $("reference-images").value = "";
      return;
    }
    const epoch = state.uploadEpoch;
    state.uploading = true;
    $("references-upload-text").textContent = "Uploading…";
    showError("");
    try {
      for (const file of files) {
        if (epoch !== state.uploadEpoch) break;
        const result = await postUpload(file);
        if (epoch !== state.uploadEpoch) break;
        state.references.push({ id: result.id, name: file.name, url: URL.createObjectURL(file) });
        renderReferences();
      }
    } catch (exc) {
      showError(exc.message);
    } finally {
      state.uploading = false;
      $("reference-images").value = "";
      $("references-upload-text").textContent = "Drop images or click to choose";
    }
  }

  function renderReferences() {
    $("reference-list").replaceChildren(...state.references.map((ref, index) => el("div", { class: "reference-card" },
      el("img", { src: ref.url, alt: ref.name }),
      el("span", { text: `Image ${index + 1}`, title: ref.name }),
      el("button", { type: "button", class: "ghost small", text: "Remove", "aria-label": `Remove image ${index + 1}`, onclick: () => {
        URL.revokeObjectURL(ref.url);
        state.references.splice(state.references.indexOf(ref), 1);
        renderReferences();
      } }),
    )));
    $("references-note").textContent = state.references.length ? `${state.references.length} / 10` : "";
  }

  function clearUploads() {
    state.uploadEpoch += 1;
    setUpload(null);
    state.references.forEach((ref) => URL.revokeObjectURL(ref.url));
    state.references = [];
    renderReferences();
    $("reference-images").value = "";
  }

  // ---------- jobs ----------

  function showError(message) {
    $("form-error").textContent = message;
    $("form-error").hidden = !message;
  }

  function showWarnings(messages) {
    $("form-warnings").textContent = (messages || []).join("\n");
    $("form-warnings").hidden = !(messages && messages.length);
  }

  async function submit(event) {
    event.preventDefault();
    if (state.uploading) { showError("Wait for image uploads to finish."); return; }
    showError("");
    showWarnings([]);
    saveForm();
    const buttons = [$("generate"), $("topbar-generate")].filter(Boolean);
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const job = await MF.api("/api/generate", { method: "POST", json: collectPayload() });
      if (job.status === "queued") MF.toast("Job added to the queue");
      track(job);
      refreshJobs();
    } catch (exc) {
      showError(exc.message);
      $("form-error").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
    }
  }

  function track(job) {
    if (state.source) state.source.close();
    state.activeJob = job;
    state.lastSeq = -1;
    $("stage-empty").hidden = true;
    $("stage-meta").hidden = false;
    $("job-results").replaceChildren();
    $("job-command").textContent = job.shell_command;
    $("cancel-job").hidden = ["done", "error", "cancelled"].includes(job.status);
    setStatus("Queued", "");
    setProgress(null);
    for (const name of job.images || []) addResult(name);
    if (job.images && job.images.length) showImage(`/api/images/${encodeURIComponent(job.images.at(-1))}`, false);
    if (["done", "error", "cancelled"].includes(job.status)) {
      setStatus(capitalize(job.status), job.error || "");
      setProgress(job.status === "done" ? 1 : 0);
      return;
    }
    const source = new EventSource(`/api/jobs/${job.id}/events`);
    state.source = source;
    source.onmessage = (message) => {
      const event = JSON.parse(message.data);
      if (event.seq <= state.lastSeq) return;
      state.lastSeq = event.seq;
      handleEvent(job, event);
    };
    source.onerror = () => {
      if (["done", "error", "cancelled"].includes(state.activeJob?.status)) source.close();
    };
  }

  function handleEvent(job, event) {
    switch (event.type) {
      case "queued":
        setStatus("Queued", event.position > 1 ? `position ${event.position}` : "");
        break;
      case "started":
        setStatus("Starting", "");
        break;
      case "loading":
        setStatus("Loading model", event.model?.model || "");
        setProgress(null);
        break;
      case "loaded":
        setStatus("Model loaded", `${event.seconds}s`);
        break;
      case "model_cached":
        setStatus("Model ready", "already in memory");
        break;
      case "seed_queued":
        job.imageLabel = event.count > 1 ? `image ${event.index + 1}/${event.count} · ` : "";
        job.seed = event.seed;
        break;
      case "progress": {
        const rate = event.rate ? (event.rate >= 1 ? `${event.rate.toFixed(2)} it/s` : `${(1 / event.rate).toFixed(1)} s/it`) : "";
        const remaining = event.rate ? Math.max(0, (event.total - event.step) / event.rate) : null;
        const eta = remaining !== null ? ` · ${formatSeconds(remaining)} left` : "";
        setStatus("Generating", `${job.imageLabel || ""}step ${event.step}/${event.total}${rate ? ` · ${rate}` : ""}${eta} · seed ${event.seed}`);
        setProgress(event.step / event.total);
        break;
      }
      case "preview":
        showImage(`/api/jobs/${job.id}/preview.jpg?step=${event.seed}-${event.step}`, true);
        break;
      case "decoding":
        setStatus("Decoding", `seed ${event.seed}`);
        break;
      case "image":
        addResult(event.name);
        showImage(`/api/images/${encodeURIComponent(event.name)}`, false);
        break;
      case "done":
        finish(job, "done");
        setStatus("Done", `${event.images.length} image${event.images.length === 1 ? "" : "s"} saved`);
        setProgress(1);
        break;
      case "cancelled":
        finish(job, "cancelled");
        setStatus("Cancelled", event.message || "");
        break;
      case "error":
        finish(job, "error");
        setStatus("Failed", "");
        showError(event.message);
        setProgress(0);
        break;
    }
  }

  function finish(job, status) {
    job.status = status;
    $("cancel-job").hidden = true;
    if (state.source) state.source.close();
    refreshJobs();
  }

  function setStatus(title, detail) {
    $("job-status").textContent = title;
    $("job-detail").textContent = detail;
  }

  function setProgress(fraction) {
    const bar = $("progress-bar");
    bar.classList.toggle("indeterminate", fraction === null);
    bar.style.width = fraction === null ? "" : `${Math.round(fraction * 100)}%`;
  }

  function showImage(src, isPreview) {
    const img = $("stage-image");
    img.hidden = false;
    img.src = src;
    img.classList.toggle("preview", isPreview);
  }

  function addResult(name) {
    const src = `/api/images/${encodeURIComponent(name)}`;
    const thumb = el("img", { src, alt: name, title: name, onclick: () => {
      showImage(src, false);
      document.querySelectorAll("#job-results img").forEach((i) => i.classList.toggle("active", i === thumb));
    } });
    $("job-results").append(thumb);
  }

  async function refreshJobs() {
    try {
      const { jobs } = await MF.api("/api/jobs");
      $("job-list").replaceChildren(...jobs.slice(0, 20).map((job) => {
        const thumb = job.images.length
          ? el("img", { src: `/api/images/${encodeURIComponent(job.images[0])}`, alt: "", onerror: (e) => e.target.classList.add("thumb-missing") })
          : el("div", { class: "thumb-placeholder" });
        return el("li", { onclick: () => track(job), title: job.prompt },
          thumb,
          el("div", {}, el("div", { class: "job-prompt", text: job.prompt || "(no prompt)" }), el("div", { class: "job-sub", text: `${prettyCommand(job.command)} · ${new Date(job.created_at * 1000).toLocaleTimeString()}` })),
          el("span", { class: `state ${job.status}`, text: job.status }),
        );
      }));
      if (!jobs.length) $("job-list").replaceChildren(el("li", { class: "muted empty", text: "No jobs yet." }));
    } catch { /* shown on next refresh */ }
  }

  async function clearHistory() {
    if (state.uploading) { showError("Wait for image uploads to finish."); return; }
    if (!window.confirm("Clear job history? This forgets finished jobs, deletes unused uploaded images and removes saved form drafts from this browser. Images in the gallery are kept.")) return;
    try {
      const result = await MF.api("/api/jobs", { method: "DELETE" });
      clearStoredDrafts();
      clearUploads();
      if (state.activeJob && ["done", "error", "cancelled"].includes(state.activeJob.status)) resetStage();
      refreshJobs();
      MF.toast(`Cleared ${result.cleared_jobs} job${result.cleared_jobs === 1 ? "" : "s"}`);
    } catch (exc) {
      showError(exc.message);
    }
  }

  function clearStoredDrafts() {
    try {
      Object.keys(localStorage).filter((key) => key.startsWith(STORAGE_PREFIX)).forEach((key) => localStorage.removeItem(key));
    } catch { /* storage may be unavailable */ }
    state.skipSave = true;
  }

  function resetStage() {
    if (state.source) state.source.close();
    state.activeJob = null;
    $("stage-image").hidden = true;
    $("stage-image").removeAttribute("src");
    $("stage-meta").hidden = true;
    $("stage-empty").hidden = false;
    showError("");
    showWarnings([]);
  }

  function capitalize(text) {
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function formatSeconds(seconds) {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  }

  // ---------- wiring ----------

  function wire() {
    $("command").addEventListener("change", (e) => {
      saveForm();
      const saved = storage("get", `${STORAGE_PREFIX}form:${e.target.value}`);
      if (saved) applyPayload(saved); else renderCommand(e.target.value);
      storage("set", LAST_COMMAND_KEY, e.target.value);
    });
    document.querySelectorAll('input[name="model-source"]').forEach((r) => r.addEventListener("change", updateModelSource));
    $("model-builtin").addEventListener("change", updateStepsPlaceholder);
    document.addEventListener("change", (e) => {
      if (e.target.dataset?.flag === "--base-model") updateStepsPlaceholder();
      if (["--width", "--height"].includes(e.target.dataset?.flag)) updatePresetHighlight();
    });
    $("generate-form").addEventListener("submit", submit);
    $("generate-form").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("generate-form").requestSubmit();
    });
    if ($("topbar-generate")) $("topbar-generate").addEventListener("click", () => $("generate-form").requestSubmit());
    $("clear-history").addEventListener("click", clearHistory);
    $("add-lora").addEventListener("click", () => addLoraRow());
    $("init-image").addEventListener("change", (e) => uploadImage(e.target.files[0]));
    $("init-strength").addEventListener("input", (e) => { $("strength-value").textContent = e.target.value; });
    $("clear-image").addEventListener("click", () => setUpload(null));
    $("reference-images").addEventListener("change", (e) => uploadReferences([...e.target.files]));
    const referencesZone = $("references-dropzone");
    referencesZone.addEventListener("dragover", (e) => { e.preventDefault(); referencesZone.classList.add("drag"); });
    referencesZone.addEventListener("dragleave", () => referencesZone.classList.remove("drag"));
    referencesZone.addEventListener("drop", (e) => { e.preventDefault(); referencesZone.classList.remove("drag"); uploadReferences([...e.dataTransfer.files]); });
    const zone = $("dropzone");
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
    zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("drag"); uploadImage(e.dataTransfer.files[0]); });
    $("cancel-job").addEventListener("click", async () => {
      if (!state.activeJob) return;
      await MF.api(`/api/jobs/${state.activeJob.id}/cancel`, { method: "POST" });
      setStatus("Cancelling", "stops after the current step");
    });
    $("copy-command").addEventListener("click", async () => {
      if (state.uploading) { showError("Wait for image uploads to finish."); return; }
      showError("");
      try {
        const result = await MF.api("/api/validate", { method: "POST", json: collectPayload() });
        showWarnings(result.messages);
        MF.copy(result.shell_command);
      } catch (exc) {
        showError(exc.message);
      }
    });
    $("reset-form").addEventListener("click", () => {
      storage("remove", `${STORAGE_PREFIX}form:${state.command.command}`);
      renderCommand(state.command.command);
      $("seeds").value = "";
      clearUploads();
    });
    $("generate-form").addEventListener("input", () => { state.skipSave = false; });
    window.addEventListener("beforeunload", saveForm);
  }

  async function start() {
    await MF.ready;
    const data = await MF.api("/api/commands");
    state.commands = data.commands;
    state.localModels = data.local_models;
    state.localLoras = data.local_loras;
    renderCommandSelect();
    wire();

    const reuse = storage("get", REUSE_KEY);
    storage("remove", REUSE_KEY);
    const lastCommand = storage("get", LAST_COMMAND_KEY);
    const initial = reuse?.command && commandSpec(reuse.command) ? reuse
      : storage("get", `${STORAGE_PREFIX}form:${lastCommand}`) || null;
    if (initial && commandSpec(initial.command)) applyPayload(initial);
    else renderCommand((commandSpec("mflux-generate-qwen-2.1") || state.commands.find((c) => c.available)).command);
    if (reuse) MF.toast("Settings loaded from gallery");

    refreshJobs();
    const running = (await MF.api("/api/jobs")).jobs.find((j) => j.status === "running" || j.status === "queued");
    if (running) track(running);
  }

  start().catch((exc) => showError(exc.message));
})();
