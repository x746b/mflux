# Web Settings page

## Proposed first version

- Add Settings beside Generate and Gallery, using the existing page layout and authentication.
- Hugging Face access: read-only presence/source status for environment tokens or the saved Hugging Face login, plus setup guidance. No token input, storage, network verification, or changes to credentials. Read the configured token file directly to avoid OAuth refresh/network side effects.
- Appearance: System, Light, and Dark themes, plus Orange (current), Blue, and Teal accents. Persist appearance per browser and apply across pages before rendering to avoid a theme flash.
- Read-only runtime information: model, LoRA, and output directories; memory budget and idle unload time. Keep command-line settings authoritative.
- Reserve editable generation defaults, gallery preferences, and runtime memory controls for a later pass after agreeing their scope.

## Files

- templates/base.html and new templates/settings.html: navigation and page.
- static/settings.js, common appearance initialization, and static/app.css: controls and themes.
- app.py and settings.py: authenticated settings endpoint and read-only token detection.
- Focused settings/authentication tests and usage documentation.

## Verification

- Existing authentication applies to settings reads; token and server secrets never appear in responses. No new writes or secret persistence.
- Missing, blank, unreadable and saved tokens, environment precedence, refresh and runtime values.
- Theme preferences persist across navigation/reload; System follows OS theme; current Generate/Gallery behavior remains intact.
- Targeted web tests, JavaScript checks, and browser checks if available. No image inference or publishing required.

## Approval

User approved implementation with read-only Hugging Face guidance, appearance controls, and runtime information. Earlier proposal to manage tokens was withdrawn.
