# WebUI 0.20.0 maintenance update

Version: `0.20.0+webui.1`. Install target: `v0.20.0-webui`.

## Upstream adoption

Merged upstream `80bae91`: PRs [#758](https://github.com/mflux-community/mflux/pull/758),
[#768](https://github.com/mflux-community/mflux/pull/768),
[#741](https://github.com/mflux-community/mflux/pull/741),
[#747](https://github.com/mflux-community/mflux/pull/747), and
[#774](https://github.com/mflux-community/mflux/pull/774).
The existing Qwen text-generation geometry-cache eviction remains intact.

Qwen 2.1 editing uses the upstream edit model through a new WebUI adapter. It supports zero
to ten ordered reference images, RGBA PNG output, output resolution and the prefix KV cache
toggle. The adapter validates scheduler, guidance, steps and dimensions before model loading.
Editing uses the same Qwen-Image-2.1 checkpoint family as text generation, but a separate
model implementation; switching between them reloads the model with the default cache size.
LoRAs remain available for text generation, not editing.

The browser numbers references after upload/removal and preserves uploads while switching
families. It only submits uploads supported by the selected command. Clear history resets the
browser's uploads while protecting files still needed by running or queued jobs. Upload ids are
not saved in browser drafts: after a reload or gallery reuse, attach references again.

## Memory protection

The incident in [upcoming.md](upcoming.md) has **not** been reproduced with real weights in this
update. No real model loading, generation, or model downloads were performed on the Mac.
The root cause of the reported roughly 13 GB per-run Metal growth remains unconfirmed.

The server now:

- Applies `--max-memory-gb` to MLX at worker startup (default: 75% of physical RAM).
- Caps the buffer cache at one quarter of that budget for the server lifetime. Per-job limits
  and upstream low-RAM callbacks can lower this cap, but cannot raise it.
- Checks active MLX memory before loading/generation, before denoising, at each callback step,
  after previews, before decoding, and after generation. A breach stops the job, cancels queued
  jobs and unloads cached models.
- Compares retained active memory after completed jobs with the first completed run for the
  current set of cached models. Growth over the larger of 2 GiB or 10% unloads those models.
  The baseline does not move upward on each run.
- Keeps at most four prompt-cache entries per model, including text generation, and retains
  the existing eight-entry Qwen text-generation geometry cache.
- Clears exception traceback chains before unloading; log records contain error text only.
  Failed and cancelled jobs unload cached models too.

The UI calls the units GB; the server budget and meter use GiB (1024³ bytes). The upstream
per-job `--mlx-cache-limit-gb` option retains its original decimal-GB interpretation.

These are **soft safeguards, not a hard process-memory ceiling**. A single model load, encoding,
denoising step or decode can allocate beyond the budget before the next check; MLX's own limit
can also be exceeded. Python/Pillow/native allocations and other applications are outside the
active-MLX counter. Lower the budget when sharing the Mac with an LLM server.

For the first manual test on the 128 GB Mac, use one reference, a small image and four steps,
with model reuse disabled:

```bash
uv tool install --force --python 3.14 "mflux[web] @ git+https://github.com/x746b/mflux@v0.20.0-webui"
mflux-web --models-dir ~/AI/models --output-dir ~/AI/mflux-web/outputs \
  --max-memory-gb 64 --cache-size 0
```

The model reloads after every job with `--cache-size 0`. This is the conservative choice until
real-weight behavior has been observed. When testing editing, set Width and Height to 512,
Steps to 4, Output resolution to 512, and keep live previews off for the initial run.

## Browser upgrades

Every static asset URL includes a content-derived version. HTML and static files use
`Cache-Control: no-cache` for revalidation, while APIs retain `no-store`. This prevents a new
page from using the previous release's upload handlers after reinstalling. Reload the page
after restarting the server; already-open tabs keep the JavaScript they have loaded.

## Verification

Automated checks use Python 3.14.3, fake model adapters, simulated memory counters and small
MLX tensors. Memory tests cover the persistent cap, low-RAM callback clamping, exceptions
retaining model references, cumulative growth across queued jobs, and cancelling the queue
after a budget breach. Web tests cover CLI validation and actual upload/queue endpoints.

Results on 2026-09-27:

| Check | Result |
|---|---|
| WebUI tests, development environment (MLX 0.32.0) | 217 passed |
| WebUI tests, installed wheel (MLX 0.32.2, freshly resolved web dependencies) | 217 passed |
| Selected upstream CLI, argument-parser, weights, LoRA and Qwen 2.1 tests on CPU | 741 passed |
| Same upstream selection on Metal 0.32.0 | 712 passed; 29 upstream LoRA comparison failures, reproduced on untouched upstream |
| Warm-cache Firefox test against source and installed wheel | Passed |
| Built-wheel `mflux-web --help`, HTTP startup and Ctrl+C shutdown | Passed; exit 0, no traceback |
| Repository Ruff lint, changed-file formatting, WebUI type check, model manifest extraction | Passed |

FastAPI's test client emits a deprecation warning about its HTTPX transport; the tests still
pass. Actual HTTP startup and browser checks exercise the server independently of TestClient.

`scripts/web_browser_smoke.py` uses a persistent Firefox profile. It first serves the old
release's assets with a long cache lifetime, proves the old script is cached, then switches to
the new app at the same address. It drives real file inputs, thumbnail removal, family changes,
size selection and history clearing, and verifies the submitted references. Its worker is
disabled: requests are validated and queued but never load models.

Example using isolated environments (set cache/profile paths under your chosen work directory):

```bash
uv venv /tmp/mflux-browser/.venv
uv pip install --python /tmp/mflux-browser/.venv/bin/python3 playwright
PLAYWRIGHT_BROWSERS_PATH=/tmp/mflux-browser/browsers \
  /tmp/mflux-browser/.venv/bin/python3 -m playwright install firefox
PLAYWRIGHT_BROWSERS_PATH=/tmp/mflux-browser/browsers \
  /tmp/mflux-browser/.venv/bin/python3 scripts/web_browser_smoke.py \
  --server-python /tmp/mflux/.venv/bin/python3 --work-dir /tmp/mflux-browser/check
```

On this Mac with MLX/Metal 0.32.0 and 0.32.2, 29 Qwen LoRA numerical
comparisons fail at an absolute tolerance of 1e-6. The exact same 29 failures reproduce on
untouched upstream `80bae91`; all 70 tests in that LoRA file pass with MLX's CPU device. No
tolerances or model code were changed to hide these failures. This is separate from validating
the new WebUI's wiring and from real-weight image-quality testing, which remains manual.
