# ClickyTabs backend

The self-hosted half of [ClickyTabs](../README.md): a FastAPI server the extension connects to over a WebSocket
(`/v1/ws`), with a model-agnostic capability layer behind it. Every capability has a paid-API and an open-source
provider; open models come from Ollama first, Hugging Face runtimes second. Browser tasks run in the separate
[automation worker](automation-worker/README.md).

Design: [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md). Choosing models: [docs/MODELS.md](../docs/MODELS.md).

## Setup

```bash
cd backend
uv sync --extra local --extra kokoro        # + --extra anthropic for Claude, --extra local-hf for HF speech fallbacks
uv run clicky doctor                        # GPU, RAM, Ollama, models: the tier and what the profile still needs
uv run clicky models pull --profile local   # the local models (several GB; --dry-run first)
uv run clicky serve                         # ws://127.0.0.1:8765/v1/ws
uv run clicky token show                    # paste into the extension's welcome page
```

For the `cloud` or `hybrid` profile, copy `.env.example` to `.env` and add the keys you need. For browser tasks,
`uv sync --directory automation-worker` once.

## Commands

| Command | What it does |
|---|---|
| `clicky serve [--profile P] [--port N] [--reload]` | Run the server |
| `clicky doctor [--bench]` | Hardware, Ollama (including its effective settings), models, ffmpeg, Chrome; `--bench` measures the speech providers |
| `clicky config show` | The provider chain of every capability, where each value came from, and its health |
| `clicky models list` / `pull [--dry-run] [--include-fallbacks] [--only P]` | What the profile needs, and fetching it |
| `clicky chat "text" [--provider P --model M] [--image PNG]` | One streamed answer, with time to first token, tokens/s and the Ollama CPU/GPU split |
| `clicky eval brain` | The pointing eval ([evals/brain](evals/brain/README.md)) |
| `clicky token show` / `rotate` | The pairing token; rotating it disconnects the extension until it's paired again |
| `clicky schema export [--check]` | Write or check `protocol.schema.json` (then `npm run gen:protocol` in `extension/`) |

## Configuration

Everything lives under one directory, `CLICKY_HOME`: this `backend/` folder when running from source, otherwise the
user data folder (`%LOCALAPPDATA%\ClickyTabs` on Windows, `~/.local/share/ClickyTabs` on Linux,
`~/Library/Application Support/ClickyTabs` on macOS), or wherever `CLICKY_HOME` points. It holds:

- `.env`: API keys and `CLICKY_*` settings (from `.env.example`);
- `clicky.toml`: profile, hardware tier, per-capability overrides, Ollama host and runner options (from
  `clicky.toml.example`);
- `.clicky/`: the pairing token, logs, settings chosen on the options page, downloaded model files.

Precedence: environment > `.env` > `clicky.toml`. `[profiles]` and `[ollama]` come from `clicky.toml` only, and a
per-profile capability override replaces the preset as a whole.

## Tests

```bash
uv run ruff check .
uv run pytest -q            # hermetic: temporary CLICKY_HOME, fake providers, no network
uv run pytest -m live -q    # needs a running Ollama with qwen2.5vl:3b; skipped otherwise
```
