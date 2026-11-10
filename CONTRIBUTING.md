# Contributing to ClickyTabs

Thanks for wanting to help. Bug reports, hardware reports, new model providers, fixes and docs are all welcome.

## Ways to help

- **Report a bug.** Open an issue with the bug template. The output of `uv run clicky doctor` and
  `uv run clicky config show` (in `backend/`) answers most questions up front; neither prints keys or the token.
- **Report how it runs on your hardware.** ClickyTabs was tuned on a 4 GB laptop GPU. Numbers from other machines
  (bigger NVIDIA cards, Apple Silicon, AMD, CPU-only) help everyone pick models: which tier and models you used, and
  the latency timeline (options page > Developer) for a few questions.
- **Add or improve a provider.** A new speech model, an Ollama model that points better, a cloud API. See "Adding a
  provider" in [AGENTS.md](AGENTS.md#conventions).
- **Improve the docs.** If something in the docs didn't match what you saw, that's a bug.

For a larger change (a new feature, a protocol change, anything security-related), please open an issue first so we
can agree on the approach before you spend time on it.

## Development setup

Follow [Run from source](README.md#run-from-source) and [Development](README.md#development) in the README. In short:
`uv sync --extra local --extra kokoro --extra anthropic` in `backend/`, `npm ci` in `extension/`, then
`uv run clicky serve --reload` and `npm run dev`. The `fake` profile (`clicky serve --profile fake`) needs no models
and no keys, which is handy for UI work.

[AGENTS.md](AGENTS.md) is the map of the codebase and its conventions; it's written for coding agents but works for
people too.

## Before you open a pull request

1. **Run the checks** for what you changed (or everything, with `scripts/ci.ps1`):
   - backend: `uv run ruff check .` and `uv run pytest -q`;
   - extension: `npm run check`, `npm run test`, and `npm run test:e2e` for anything the user sees;
   - protocol: if you changed `backend/src/clicky/protocol/`, regenerate and commit the schema and the TypeScript
     types (`scripts/gen-protocol.ps1`, or the two commands in AGENTS.md).
2. **Add tests.** New behavior gets a test; a bug fix gets a test that fails without it. Backend tests must stay
   hermetic (no network, no real models); real-model checks belong in `-m live` tests or opt-in Playwright specs.
3. **Update the docs** that describe what you changed.
4. **Keep the pull request focused**: one change, with a description of what and why, and how you tested it. Mention
   it if the change touches anything in [docs/SECURITY.md](docs/SECURITY.md).

Commit messages: a short summary line in the imperative ("Add a Piper TTS provider"), then details if useful.

## Security

Please don't open public issues for vulnerabilities; see [docs/SECURITY.md](docs/SECURITY.md#reporting-a-vulnerability).

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE), the same as the
project.
