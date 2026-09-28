# OpenAI prompt assistant

User approved implementing documented Variant B on 2026-09-28: OpenAI Responses API, GPT-6 Luna default, GPT-6 Sol selectable. Local oMLX remains a future option.

- Read OPENAI_API_KEY from the server environment only. Do not read or execute shell startup files. Explain exporting the variable in ~/.zshrc and restarting from that shell; never return or persist the key in the UI.
- Collapsible chat drawer opened beside Prompt, with streaming, Stop, Clear, quick prompt actions and explicit insertion of proposed prompts. Use plain text rendering and distinct prompt blocks.
- Settings exposes key-presence status, default model, and custom instructions. Store these nonsecret preferences per browser; retain chat in memory for the current page only.
- Backend validates models, roles, message sizes/counts and instruction length; bounds output and concurrency. Use fixed OpenAI HTTPS endpoint, low reasoning, store=false, no tools, no automatic retries of paid requests.
- Reuse web authentication/CSRF and add a streaming client helper. Close upstream connections when cancelled. Report controlled errors without forwarding provider payloads or credentials.
- Files: new web/chat.py and static chat scripts/template, app.py routes, common.js streaming, generate.js launcher, Settings controls, styles, web dependency declaration/lock, docs and focused tests.
- Verify with mocked HTTP streaming, access/CSRF tests, limits, provider errors, cancellation, JavaScript checks, existing Settings tests and browser checks if available. Do not make a paid API call during implementation.
