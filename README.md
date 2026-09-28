# Archived — Web UI development has moved

This fork is archived and no longer maintained.

## Active project: mflux-webui

My Web UI is now maintained as **a standalone browser UI for mflux**:

**https://github.com/x746b/mflux-webui**

Please use that repository for current installation instructions, releases, issues,
and feature requests. It contains the UI, gallery, prompt assistants, and settings,
with image generation provided by upstream mflux.

## Why the split?

Following the [discussion with the mflux-community maintainers](https://github.com/orgs/mflux-community/discussions/771),
the Web UI was separated from this combined fork into an independently maintained
package. This follows the community's approach to
[building independent UIs under `mflux.web`](https://github.com/mflux-community/mflux#build-a-ui-under-mfluxweb).

For related community packaging work, see [`mflux-web` on PyPI](https://pypi.org/project/mflux-web/).
Its package description distinguishes the standalone `mflux_web` implementation
from the thin `mflux.web` facade. That package is separate from `mflux-webui`;
refer to the upstream guide for current UI packaging guidance.

For the inference library and CLI, use the official
[mflux-community/mflux repository](https://github.com/mflux-community/mflux).

## Historical screenshots

The [screenshots in `docs/`](docs/) are preserved so links in earlier discussions
continue to work. Previous code remains in Git history and existing tags for
historical reference; those versions are no longer supported.
