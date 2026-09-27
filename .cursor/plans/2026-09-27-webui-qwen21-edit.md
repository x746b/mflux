# WebUI Qwen 2.1 editing adoption

User authorized implementation, commit and push with a 0.20.0 version prefix.
All modifications, environments, caches, test outputs and browser profiles stay under /tmp.
No real model jobs or model downloads on this Mac.

1. Review notes and merge upstream main (PRs 758, 768, 741, 747, 774), preserving fork patches.
2. Add worker memory budget, lifetime cache cap, step checks and retained-memory eviction;
   avoid retaining exception tracebacks; version assets and revalidate browser caches.
3. Add Qwen 2.1 edit adapter and validated ordered reference uploads, preserve uploads
   across family switches and protect references used by active jobs during history clearing.
4. Verify fake-model memory regressions, adapter/CLI parity, Python 3.14 help and web tests,
   relevant upstream tests, browser upload/cache behavior, and built-wheel server startup.
5. Update documentation, commit phases, push main and update v0.20.0-webui for the user's
   existing reinstall command. Do not trigger PyPI release workflows.
