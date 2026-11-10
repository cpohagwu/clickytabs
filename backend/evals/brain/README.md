# Brain eval

`clicky eval brain` scores a brain model on 20 scripted pages: does it point at the right element, are all its tags
valid, does it say what it should, and how fast does the first speakable sentence arrive. Results for the reference
setup are in [docs/MODELS.md](../../../docs/MODELS.md#pointing-brain-eval).

- `cases.json`: one case per page - the spoken-style question, the acceptable targets (`role` + a case-insensitive
  `name` substring, or a `region` in CSS px for things without an element id), `optional` (not pointing is fine),
  `none` (must not point) and `say` (at least one of these words must be spoken).
- `pages/*.html`: the pages, self-contained (no network).

## Run it

1. Capture the pages through the real extension pipeline (DOM set-of-marks + masked, resized screenshot) at both page
   budgets. No backend is needed; the build must be current:

   ```powershell
   cd extension
   npm run build
   $env:CC_EVAL_OUT = (Resolve-Path ..\backend).Path + "\.clicky\eval\pages"
   npx playwright test eval-capture
   ```

2. Score a model (default: the profile's brain; API models get the `full` pages, local models `compact`):

   ```powershell
   cd backend
   uv run clicky eval brain                                                   # the configured brain
   uv run clicky eval brain --provider anthropic --effort low                 # claude-opus-5-5
   uv run clicky eval brain --provider anthropic --model claude-haiku-5-5 --effort low
   uv run clicky eval brain --provider ollama --model qwen3.5:4b
   uv run clicky eval brain --cases login,chart                               # a subset
   ```

Each run prints one line per case and a summary, and saves the full results (spoken text, directives, problems,
timings, token usage) to `.clicky/eval/brain-<model>-<time>.json`. Paid models cost real money: a Claude Opus 5.5 run
of all 20 cases is roughly $0.20-0.50.

## Adding a case

Add the page to `pages/`, the case to `cases.json`, re-run the capture, and check the target exists in the captured
page (its `role` and accessible name appear in `<pages>/full/<id>.json` → `elements`).
