# AGENTS.md

Guidance for coding agents (Claude Code, Codex, Cursor, ...) and for humans who like a short map. The user-facing
docs are [README.md](README.md) and [docs/](docs/); this file is about working on the code.

## What this is

ClickyTabs is a voice companion inside the active Chrome tab. Two halves talk over one authenticated WebSocket:

- **`extension/`**: Chrome MV3 extension (WXT + Svelte 5 + TypeScript, npm). UI, overlay, microphone and playback,
  screenshot and DOM capture, consent, cookie export.
- **`backend/`**: FastAPI orchestrator (Python 3.12, uv, package `clicky`, CLI `clicky`). Speech-to-text, the brain,
  text-to-speech, scene codegen, browser tasks, all behind a model-agnostic capability layer.
- **`backend/automation-worker/`**: browser-use in its **own** uv environment (its pins clash with the backend's),
  spawned per task and driven over JSON lines on stdio.

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing how parts interact.

## Setup and the commands that must pass

```bash
# backend/
uv sync --extra local --extra kokoro --extra anthropic
uv run ruff check .
uv run pytest -q                                   # hermetic; ~1 min
uv run python -m clicky.protocol.export --check    # protocol schema is current

# extension/
npm ci
npm run gen:protocol:check                         # generated TS types are current
npm run check                                      # svelte-check (warnings fail) + tsc, strict
npm run test                                       # Vitest
npm run build && npm run check:manifest
npx playwright install chromium && npx playwright test   # e2e, ~3 min
```

`scripts/ci.ps1` runs all of it in order. Before you say a change is done, run the checks for the half you touched;
for anything crossing the socket, run both.

## Where things live

| To change... | Look at |
|---|---|
| A message between extension and backend | `backend/src/clicky/protocol/events.py` (source of truth), then regenerate (below) |
| What the brain is told | `backend/src/clicky/brain/prompts/` (`full.md` for big models, `compact.md` for small ones) |
| How tags in the answer are parsed and validated | `backend/src/clicky/brain/directives.py` |
| How page context is rendered for the model | `backend/src/clicky/brain/context.py`, budgets in `brain/budget.py` |
| A voice or text turn | `backend/src/clicky/session/voice_turn.py`, `text_turn.py`, `brain_turn.py` |
| Providers, presets, fallbacks | `backend/src/clicky/capabilities/` (`catalog.py`, `profiles.py`, `registry.py`, `options.py`) |
| Browser tasks (backend side) | `backend/src/clicky/automation/` (`tasks.py`, `session_bridge.py`, `worker_client.py`) |
| The agent and its click gate | `backend/automation-worker/src/clicky_worker/` (`runner.py`, `guard.py`, `hitl.py`) |
| The service worker (socket, routing, push-to-talk, capture) | `extension/src/entrypoints/background.ts`, `src/lib/socket.ts`, `src/lib/ptt.ts` |
| What a page may ask the service worker | `extension/src/lib/messages.ts` (message types and sender rules) |
| The orb, captions and HUD | `extension/src/components/`, `src/lib/companion-controller.ts` |
| Pointer and annotations | `extension/src/lib/overlay/` |
| Screenshot + element map | `extension/src/lib/page-context/` |
| Scenes (sandbox runtime) | `extension/public/scene.html`, `src/entrypoints/scene-harness.ts`, `src/lib/scene/` |
| Microphone and playback | `extension/src/entrypoints/offscreen/` |

## Conventions

- **The protocol is defined once.** Edit the Pydantic models in `backend/src/clicky/protocol/`, then run
  `uv run clicky schema export` (backend) and `npm run gen:protocol` (extension), and commit
  `backend/protocol.schema.json` and `extension/src/lib/protocol.gen.ts` with the change. Never edit the generated
  files by hand. Keep changes backward compatible: add optional fields, don't rename or remove.
- **Adding a provider:** an adapter in `capabilities/<kind>/`, an options model in `capabilities/options.py` (with
  `extra='forbid'`), a catalog row in `catalog.py` (its uv extra, the key it needs, the modules it imports), and tests.
  Every capability must keep at least one paid and one open provider (`tests/test_invariants.py`). Heavy imports stay
  lazy: reading the catalog must not import SDKs.
- **Tests are hermetic.** Backend tests run with a temporary `CLICKY_HOME`, a scrubbed environment and the `fake`
  profile; mock HTTP with `httpx.MockTransport`. Anything that needs Ollama is marked `live`. E2E runs the real
  backend on port 18765 with `CLICKY_PROFILE=fake` and Playwright's bundled Chromium (branded Chrome ignores
  `--load-extension`). The fake brain reacts to markers in the question or page text (`[point]`, `[scene:svg|Title]`,
  `[task]`, `[page]`, `[history]`; see `capabilities/fake.py`).
- **Closed shadow root.** E2E tests can't query the companion's DOM; they use the `debug.*` messages and screenshots.
- **Style.** Python: ruff (line length 120), type hints, `from __future__ import annotations`. TypeScript: strict,
  svelte-check with warnings as errors. Comments explain why, not what. LF line endings (`.ps1` files are CRLF).
- **Keep the docs true.** If behavior a doc describes changes, update the doc in the same change
  ([docs/PROTOCOL.md](docs/PROTOCOL.md) for messages, [docs/MODELS.md](docs/MODELS.md) for presets).

## Security invariants

These are load-bearing; [docs/SECURITY.md](docs/SECURITY.md) explains each. Don't weaken them without discussing it
in an issue first.

- Only the **service worker** talks to the backend. Content scripts never open sockets or fetch the backend (a unit
  test enforces it).
- `chrome.storage.local` is restricted to trusted contexts; the token, backend URL and grants live there.
- Every runtime message passes the **sender rules** in `messages.ts`: page data and consent only from the top frame
  of the turn's tab; settings only from extension pages.
- The **cookie scope comes from the tab's URL**, never from the model. The model writes only a goal and at most a
  start URL; the backend rejects start URLs off the approved sites.
- The agent's **click gate** (`guard.py`) asks before irreversible clicks and Enter submits, whatever the model does.
- Scene code runs only in the sandboxed page, in a Worker, with `connect-src 'none'`; no page data enters it.
- Page text is fenced as untrusted data in the prompt (`brain/context.py`); keep the defang when changing the fence.
- Never log the pairing token, API keys, cookie values or user content at INFO or above. The redaction filter is a
  safety net, not a license.
- The socket can switch providers and models but never hosts, base URLs or keys (`config_service.py`).

## Gotchas

- `uv sync` installs exactly the extras you list and removes the others; stop a running `clicky serve` first on
  Windows (the `clicky.exe` shim is locked while it runs).
- The extension ID is pinned by `extension/keys/dev-public-key.b64`; the backend accepts that ID by default.
- MV3 can stop the service worker and the offscreen document at any time; state that must survive lives in
  `chrome.storage.session`. After an extension reload, an old content script stays in open tabs: check
  `ctx.isInvalid` after awaits in `content/index.ts`, or two copies end up handling the same tab.
- Ollama reloads a model whenever its runner options change, so they're defined once per model
  (`capabilities/profiles.py`, `[ollama.runner."<model>"]`).
- Playwright tests need a fresh `npm run build` (`npm run test:e2e` does it). A stopped Playwright run can leave the
  e2e backend (port 18765) and its Chromium running; stop them before the next run.
