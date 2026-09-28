# Web UI ideas and plans

## Prompt assistant

Status: OpenAI Variant B implemented on 2026-09-28; local oMLX Variant A remains planned.

Add a small chat panel for discussing image ideas and developing prompts, with two selectable backends: local oMLX and the OpenAI API. Image generation continues to run through mflux.

### Interaction

- Open a collapsible **Prompt assistant** panel from a button beside the Prompt field.
- Discuss subjects, composition, lighting, mood, style, and variations in a continuing conversation.
- Offer **Expand**, **Shorten**, and **Suggest variations** actions.
- **Use current prompt** explicitly adds the current prompt and selected image-model context to the conversation.
- **Use this prompt** inserts a chosen suggestion into the generation form. Do not replace the current prompt automatically.
- Stream replies, with **Stop** and **Clear chat** controls.
- Make the assistant's instructions editable, including preferred style and guidance for the selected image-model family. Preserve the user's intent and avoid adding unwanted details.
- Keep the active provider/model visible. Switching providers must not silently send an existing local conversation to a hosted service.
- Possible later addition: **Discuss this image**, explicitly attaching a generated image to a vision-capable chat model.

### Variant A: local oMLX

- Connect to the user's existing oMLX server through its OpenAI-compatible chat API, with streaming responses and model discovery.
- Configure the server URL and select an available chat model in Settings; support server-side credentials if the endpoint requires them.
- Target setup: the user's **128 GB M5 Mac**, with a small language model alongside mflux. This makes local chat a practical option to try, without per-request API charges or sending prompts to a hosted provider.
- Choose the exact small model after checking what is already installed and comparing responses on representative prompts.
- Account for total memory used by image weights, the language model, and both caches. The mflux memory budget does not control oMLX allocations. Measure coexistence before deciding whether load/unload coordination is necessary; do not automatically unload either service's models by default.

Reference: [oMLX documentation](https://github.com/jundot/omlx).

### Variant B: OpenAI API

- The user already has OpenAI API access.
- Start with **GPT-6 Luna** (`gpt-6-luna`) as the default for brainstorming, prompt refinement, and variations.
- Offer a model selector, with **GPT-6 Sol** (`gpt-6-sol`) as an optional comparison for more demanding discussions. Evaluate quality on actual image-prompt conversations before recommending an upgrade.
- Use the Responses API with streaming. Start by comparing low reasoning with reasoning disabled for responsiveness and quality.
- Read `OPENAI_API_KEY` on the server. Do not expose it to the browser, save it in browser storage, or commit it to the repository.
- Bound conversation context and response length to keep latency and usage predictable. Consider displaying usage when the API supplies it.
- Hosted chat leaves local compute and memory available for mflux; the selected chat content is sent to OpenAI.

Pricing reference checked on 2026-09-28 (USD per million text tokens, Standard processing, short context):

| Model | Input | Output |
| --- | ---: | ---: |
| GPT-6 Luna | $0.10 | $0.50 |
| GPT-6 Sol | $2.00 | $10.00 |

For illustration, 2,000 input tokens and 1,000 output tokens cost about $0.0007 with Luna, excluding additional reasoning tokens and other billing adjustments. Longer conversations increase input usage. Recheck pricing and account model access before implementation.

References: [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), [GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol), [model guidance](https://developers.openai.com/api/docs/guides/latest-model).

### Implementation direction

- Share the chat UI and conversation handling across both variants, with separate adapters for oMLX chat completions and OpenAI Responses.
- Add a **Prompt assistant** section in Settings for provider, model, connection status, and assistant instructions.
- Make connection tests explicit. Opening Settings alone should not trigger a paid generation or load a local model.
- Handle unavailable servers, missing credentials, cancellation, and provider errors without losing the draft prompt or conversation.
- Decide chat-history persistence and retention before implementation; keep **Clear chat** independent of generation history and appearance preferences.
- Follow the existing Web UI authentication and request-validation conventions, with focused tests and browser checks when implemented.

Implemented first version: OpenAI-only collapsible panel beside Prompt, Luna/Sol selection, streaming, Stop/Clear, draft-producing quick actions, and explicit insertion of fenced prompt suggestions. Chat lives only in page memory; model and custom style preferences are saved per browser. Server credentials come only from OPENAI_API_KEY. No shell-file parsing, secret input/storage, automatic paid connection tests, or image attachments. Context and output are bounded, low reasoning is selected, and requests use store=false. Mocked API tests cover streaming, cancellation, errors, validation, authentication and CSRF; live API quality and visual browser checks remain for manual testing.

Next steps: try the OpenAI assistant on real prompt discussions, refine its behavior, and revisit the oMLX adapter when useful. Both backend variants remain part of the intended design.
