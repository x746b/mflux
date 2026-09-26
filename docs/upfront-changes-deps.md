# Upstream changes and dependencies of the mflux-web fork

This note records how the `mflux-web` fork (`x746b/mflux`) depends on the original repository
(`mflux-community/mflux`, called *upstream* below), where upstream changes can cause merge
conflicts, and what can break silently even without a conflict. It is meant as a checklist for
each sync with upstream.

State at the time of writing (2026-09-26):

- Merge base: `cdc2916` "Add PEFT LoRA support for Qwen Image 2.1 (#756)", upstream `main` HEAD.
- The fork is 4 commits ahead of upstream and 0 behind.
- 39 files changed, +5196 / −2 lines, most of them new files.

## 1. What the fork adds (new files, cannot conflict)

These paths do not exist upstream, so a merge never conflicts on them:

| Path | Purpose |
|---|---|
| `src/mflux/web/` | The web UI: FastAPI app, auth, job runner, form schema, command adapters, templates, static JS/CSS |
| `tests/web/` | Tests for the web UI (app, auth, memory, network, paths, runner, schema/invocation, security) |
| `docs/Screenshot-3.png`, `docs/Screenshot-4.png` | README screenshots |
| `docs/upfront-changes-deps.md` | This document |
| `.cursor/plans/2026-09-26-mflux-web.md` | Design plan for the web UI |
| `NOTICE` | Attribution for `src/mflux/web/auth.py`, adapted from oMLX (Apache-2.0) |

The only risk is upstream adding a file or directory with the same name (for example its own
`src/mflux/web/`). That is unlikely, but check before merging if upstream ever announces a web UI.

## 2. Changes to upstream files (these can conflict)

Four upstream files are modified. They are listed from the most to the least important.

### 2.1 `src/mflux/models/qwen21/model/qwen21_transformer/qwen21_transformer.py` (+6 lines)

This is the **only change to upstream model code**. `Qwen21Transformer` keeps a `_geometry_cache`
keyed by `(text_len, latent_height, latent_width)`. Each entry holds RoPE tables and, when the
prompt is padded, a `seq x seq` attention mask. Upstream never evicts entries. That is fine for
the one-shot CLI, but `mflux-web` keeps the model loaded between runs, so every new prompt length
or image size would add memory that is never released.

The fork adds a class constant and a FIFO eviction:

```python
class Qwen21Transformer(nn.Module):
    GEOMETRY_CACHE_SIZE = 8
    ...
        if cache_key not in self._geometry_cache:
            # Bounded: every new prompt length or size adds an entry ...
            if len(self._geometry_cache) >= Qwen21Transformer.GEOMETRY_CACHE_SIZE:
                self._geometry_cache.pop(next(iter(self._geometry_cache)))
```

**Conflict risk: medium.** Qwen-Image-2.1 is under active development upstream. Three open PRs
add editing (reference images, RGBA, prefix KV cache):

- #741 (dreampuf) keeps its code in a separate `qwen21/reference/` package, so it is less likely
  to touch this file.
- #766 is #741's work repackaged by a maintainer.
- #749 (flyingtimes) extends the existing qwen21 classes, including this transformer, so it is
  likely to conflict here.

The comparison is in upstream issue #762. Whichever one lands, it may touch the geometry-cache
method.

**How to resolve:** take upstream's version of the method, then add the eviction again right
before the new entry is created. If upstream restructures the cache (for example keying it
differently or adding a KV cache next to it), keep the same idea: whatever is cached per prompt
length or image size must be bounded in a long-running process. No test covers this eviction
(`tests/web/test_memory.py` uses fake adapters and checks the runner, not the transformer), so
check the resolved method by reading it.

**How to remove the risk completely:** send this change upstream as a small PR. It is a real fix
for anyone who runs mflux as a server or in a loop, not something specific to the web UI.

### 2.2 `pyproject.toml` (+11 / −1)

Three changes:

1. `version = "0.20.0+webui"`. The local version suffix marks fork builds.
2. A new script entry at the end of `[project.scripts]`:
   `mflux-web = "mflux.web.cli:main"`.
3. A new `[project.optional-dependencies]` table with the `web` extra (fastapi, itsdangerous,
   jinja2, python-multipart, uvicorn). The web dependencies are optional, so plain
   `pip install mflux` users are not affected.

**Conflict risk: low to medium.** Upstream changes the version on every release and adds new
commands (for example a future `mflux-generate-qwen-2.1-edit`) at the end of the same scripts
list, right where `mflux-web` sits. Both are trivial textual conflicts:

- Version: take upstream's new number and add `+webui` again (e.g. `0.21.0+webui`).
- Scripts: keep both upstream's new lines and the `mflux-web` line.
- If upstream ever adds its own `[project.optional-dependencies]` table, merge the `web` extra
  into it. TOML does not allow the same table to be defined twice.

### 2.3 `README.md` (+66 lines)

A "Web UI" section added near the top, after the intro sentence. **Conflict risk: low.** A
conflict only happens if upstream edits the same few intro lines. Resolve by keeping upstream's
text and adding the section back in the same place.

### 2.4 `uv.lock`

Regenerated to include the web extra's dependencies. **Conflict risk: high, but harmless.**
Upstream changes the lock file often, so it will conflict on almost every sync. Never merge it by
hand. Take upstream's version and regenerate it:

```bash
git checkout --theirs uv.lock
uv lock
```

## 3. Dependencies without conflicts (silent breakage)

Git only reports conflicts on lines both sides changed. The web UI also *imports* upstream
internals, and if upstream renames or refactors them the merge succeeds but `mflux-web` fails at
runtime. These are the imports, grouped by how likely they are to change.

**Most likely to change: model classes and their constructor / `generate_image` signatures**
(`src/mflux/web/adapters.py`). One adapter per CLI command:

| Web adapter command | Upstream class |
|---|---|
| `mflux-generate` | `mflux.models.flux.variants.txt2img.flux.Flux1` |
| `mflux-generate-flux2` | `mflux.models.flux2.variants.Flux2Klein` |
| `mflux-generate-qwen` | `mflux.models.qwen.variants.txt2img.qwen_image.QwenImage` |
| `mflux-generate-qwen-2.1` | `mflux.models.qwen21.variants.txt2img.qwen_image_21.QwenImage21` |
| `mflux-generate-z-image`, `-z-image-turbo` | `mflux.models.z_image.variants.z_image.ZImage` |
| `mflux-generate-krea2` | `mflux.models.krea2.variants.txt2img.krea2.Krea2` |
| `mflux-generate-ernie-image`, `-ernie-image-turbo` | `mflux.models.ernie_image.variants.txt2img.ernie_image.ErnieImage` |

The adapters also mirror each CLI's model-construction logic, so if upstream changes how a CLI
`main()` builds its model (new required argument, renamed keyword), update the matching adapter.

**Shared CLI and config helpers:**

- `mflux.cli.capabilities.describe_command` (`schema.py`) builds the form from each command's
  own argparse options. This is the main reason the UI stays in sync with the CLI automatically:
  new CLI options appear in the form without changes to the web code.
- `mflux.cli.defaults.defaults` (`schema.py`, `adapters.py`)
- `mflux.cli.parser.parsers.lora_init_kwargs_from_args` (`adapters.py`)
- `mflux.models.common.config.ModelConfig` and
  `mflux.models.common.resolution.config_resolution.ConfigResolution` (`adapters.py`)
- `mflux.models.common.schedulers.SCHEDULER_REGISTRY` (`schema.py`)
- `mflux.models.common.vae.tiling_config.TilingConfig` (`runner.py`)

**Utilities, least likely to change:**

- `mflux.utils.dimension_resolver.DimensionResolver` (`adapters.py`)
- `mflux.utils.generated_image.GeneratedImage`, `mflux.utils.image_util.ImageUtil`
  (`invocation.py`, `runner.py`)
- `mflux.utils.prompt_util.PromptUtil`, `mflux.utils.exceptions.*` (`runner.py`)
- `mflux.callbacks.callback_manager.CallbackManager`,
  `mflux.callbacks.instances.battery_saver.BatterySaver` (`runner.py`)

`tests/web/` covers most of these paths, so a failing test after a sync usually points straight
at the upstream change that caused it.

## 4. New upstream commands are not picked up automatically

The form fields come from each command's argparse options, but the **list of commands** comes from
`ADAPTERS` in `src/mflux/web/adapters.py`. A new upstream command, for example the Qwen-Image-2.1
edit command from #741 / #749 once merged, will not appear in the UI until an adapter is added.
Editing commands also need multi-image upload (`--image-paths`, up to 10 reference images for
Qwen-Image-2.1), which the current form does not support.

## 5. Sync procedure

```bash
# once
git remote add up https://github.com/mflux-community/mflux

# every sync
git fetch up
git merge up/main
# resolve conflicts as described in section 2:
#   qwen21_transformer.py  -> take upstream, add the cache eviction back
#   pyproject.toml         -> upstream version + "+webui", keep mflux-web script and web extra
#   README.md              -> keep upstream text, add the Web UI section back
#   uv.lock                -> git checkout --theirs uv.lock && uv lock
uv sync --extra web
uv run pytest tests/web
uv run pytest -m "not slow and not high_memory_requirement"   # upstream's own fast suite
```

Then start `mflux-web` and generate one image with each model you use. The tests do not load real
weights, so this is the only check that the model constructors and `generate_image` calls still
match upstream.
