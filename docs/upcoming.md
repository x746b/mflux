# mflux-web: suggested improvements (27 Sep 2026)

State of the fork: `x746b/mflux` `main` = `d85b079`, tag `v0.20.0-webui` = `cc1f1ff`, exactly as
before today. Nothing below is applied. Today's attempt is kept only as a local, unpushed branch
`backup/today-270926` in `/tmp/mflux` (commits `c6a3835`..`20e75e2`), for reference.

Suggested order: 1 → 2 → 3 → 4 → 5. Steps 1 and 2 make the server safe and upgrades reliable
before any new feature lands; each step is small enough to test on its own.

---

## 1. Syncing upstream (4 accepted PRs)

Upstream `mflux-community/mflux` `main` is 4 commits ahead of the fork:

| PR | What it does | Web UI impact |
|---|---|---|
| #741 | Qwen-Image-2.1 reference editing (up to 10 images), RGBA output, prefix KV cache; new command `mflux-generate-qwen-2.1-edit` | Needs a new adapter and a multi-image upload widget (step 5) |
| #768 | More Qwen-Image-2.1 LoRA formats; DoRA rejected in the common loader | None; the existing LoRA list simply accepts more files |
| #758 | Refuses checkpoints missing required weights | None; a wrong local folder now fails with a clear error |
| #747 | `--verbose` on every CLI; `parse_args()` configures root logging if nothing has yet | Hide `--verbose` from the form (`FormSchema.BLOCKED_FLAGS`); mflux-web configures logging first, so it does nothing there |

Findings:

- `git merge upstream/main` is conflict-free. #741 lives in a separate `qwen21/reference/`
  package and does not touch the fork's `qwen21_transformer.py` patch.
- After the merge, `tests/web` plus upstream's CLI, weights, LoRA and arg-parser suites passed
  (906 tests on the Linux VM).
- The merge alone is safe to push; the web UI works unchanged, the edit command just does not
  appear in it yet.

Suggestion: merge and push it as its own step, then add the UI features one at a time.

## 2. Memory guard (do this before anything else; it protects the Mac)

What happened today: three queued Qwen-Image-2.1 jobs pushed the server to a 116 GB footprint and
25 GB of swap on the 128 GB Mac. The model was loaded only once (every job logged
`model_cached`), but:

- MLX memory still in use after each run grew by about 13 GB (idle: 41 → 81 GB);
- the MLX buffer cache (43 GB seen) comes on top during a run;
- `JobRunner._after_job` resets the cache limit to MLX's default, which on Metal is the whole
  memory limit (1.5 × the recommended working set), so the cache is effectively unbounded.

The root cause of the 13 GB per run was not found. On the VM the Qwen21 transformer, with or
without `mx.compile`, keeps only a few MB across runs of different shapes, so the growth needs
real weights on Metal to investigate.

Suggested implementation (new `src/mflux/web/memory.py`, used by `runner.py`):

1. `--max-memory-gb` (default 75% of RAM → 96 GB on the Mac), applied with
   `mx.set_memory_limit()` when the worker starts. Note this MLX limit is soft: past it MLX
   frees its cache and waits, but can still allocate.
2. Keep `mx.set_cache_limit()` capped (e.g. 25% of the budget) for the life of the server,
   including after a per-job `--mlx-cache-limit-gb`, instead of restoring MLX's default.
3. A check before the denoising loop and at every step (in `WebProgressCallback`): if active
   memory exceeds the budget, stop the job with a clear error and unload the model.
4. After every run, compare active memory with the model's first-run baseline; if it grew by
   more than max(2 GB, 10%), unload the model (the next run reloads it). Growth then cannot pile
   up across queued jobs, whatever causes it.
5. Show the limit in the startup banner and the top-bar tooltip; lower it when an LLM server runs
   on the same Mac.

Pitfalls found while building it:

- **argparse help strings:** write `75%%`, not `75%`. Python 3.14 validates help strings when an
  option is added, so an unescaped `%` crashes `mflux-web` at startup; Python 3.13 (the VM) only
  fails on `--help`. A test must render `parser.format_help()`.
- **Log the message, not the exception:** `logger.error("...", exc)` puts the exception into
  the log record; any handler that keeps records then holds its traceback → the job's frames →
  the model, so unloading frees nothing. Log `str(exc)` and set `exc.__traceback__ = None`, and
  unload after leaving the `except` block.

Tests: fake adapters that hoard memory during a run, or leak a little per run, plus checks that
the limits are installed. These run on the CPU VM without real weights.

Later investigation (only with explicit approval, small sizes and 4 steps, the guard active):
find what keeps ~13 GB per run alive on Metal (candidates: `mx.compile` traces per shape,
activations referenced from a cache or callback).

## 3. Browser cache: versioned script URLs (needed before any UI change)

Why today's edit UI "didn't upload" on the Mac: the server sends no `Cache-Control` for
`/static/*`. Firefox therefore guessed a freshness period (about 10% of the file's age) and kept
the **original** `generate.js` cached for hours after the reinstall. The new page showed the new
"Reference images" box, but the old script had no handler for it, so no `POST /api/uploads` was
ever sent. The init image worked because the old script handles it. This was reproduced in
Firefox with a persistent profile (old page first, then the new one: 0 uploads reached the
server); tests with a fresh profile had passed, which is how it slipped through.

Suggested implementation:

- `WebApp.static_version()`: a short hash of the static files, exposed to templates; link every
  asset as `/static/<file>?v={{ static_version }}`.
- `Cache-Control: no-cache` on pages and static files (revalidated with the ETag, cheap 304),
  `no-store` stays on `/api/*`.
- Test: the page links versioned URLs and the headers are set.

Until then, after every reinstall: **Ctrl+Shift+R** in the browser.

## 4. Web UI testing that would have caught today's failures

- **Browser tests with a warm cache:** a Selenium + Firefox (headless) script with a persistent
  profile: load the previous release first, then the new one, and drive the real widgets. Keep
  Selenium in its own venv (`uv venv`), not in the project.
- **Python 3.14:** run the web tests under 3.14 too (what the Mac uses), not only the VM's 3.13.
- **Real startup check:** start `mflux-web` from the built package (not only `TestClient`), fetch
  `/`, `/api/status`, `/api/commands`, stop with Ctrl+C.
- **Never** run model jobs on the Mac for testing without asking first.
- VM test environment notes: `mlx[cpu]==0.32.2`, CPU torch, and `numpy==2.3.5` (newer numpy
  wheels crash with "Illegal instruction" because the M5 advertises SME, which the VM traps).

## 5. Qwen-Image-2.1 editing in the web UI (after steps 1–4)

Design used in today's attempt (see `backup/today-270926`, commit `2fdb92c`):

- `Qwen21EditAdapter` in `adapters.py` for `mflux-generate-qwen-2.1-edit`, loading
  `mflux.models.qwen21.reference.QwenImage21Edit`. Its `validate()` must repeat the CLI
  `main()` checks, because the web UI never calls `main()`: linear scheduler only, guidance ≥ 1,
  at most 10 references, dimensions and `--output-resolution` multiples of 32.
- The edit model memoizes text-only prompt encodings in `prompt_cache` without a limit; trim it
  to the newest few after each run (a long-lived server would otherwise grow).
- Invocation: a `references` list of upload ids → `--image-paths p1 p2 …`, in order, max 10;
  `--image-paths` becomes a special flag in the schema; `JobRunner.active_uploads()` must include
  references so "Clear history" does not delete images a queued job still needs.
- UI: a "Reference images" section, shown only for commands with `--image-paths`, numbered
  thumbnails (the prompt refers to "image 1", "image 2"…), remove buttons. Reuse the init image's
  upload code (one shared `postUpload(file)` helper) rather than a second upload path.
- Keep uploads when switching model family (restoring another family's saved form used to clear
  them silently); "Clear history" should also reset the form's uploads, because the server
  deletes them.
- No LoRA for editing: upstream's edit command does not support LoRAs yet, so the LoRA section
  stays hidden for it.

Test material: `/tmp/mflux-edit-examples/` on the VM has three references (`1_cat.png`,
`2_armchair.png`, `3_room.png`, generated with Qwen-Image-2.1) and `edit_prompt.txt`.

## 6. Smaller items

- `docs/upstream-changes-deps.md`: after each sync, add a short log entry (which PRs, what they
  meant for the web UI) and add new adapters to the class table.
- Other edit commands (`mflux-generate-qwen-edit`, `-flux2-edit`, `-fibo-edit`, Kontext) take
  `--image-paths` too and could reuse the reference widget with one adapter each.
- Queuing: pressing Generate while a job runs queues the next one; consider showing that more
  clearly, or allowing only one queued job, so a series of clicks cannot stack up heavy runs.
