# Scene eval

The scene acceptance suite: 10 scene requests (`cases.json`: four three.js, two 2D canvas, two SVG, two HTML), each run
through the real pipeline (codegen model → `scene.render` → the extension's sandbox → `scene.result` → one automatic
repair if needed). A case passes when its scene renders: the code ran, 20 frames went by without an error, and
something visible was drawn. Results for the reference setup are in
[docs/MODELS.md](../../../docs/MODELS.md#scenes).

The brain is the fake one, so every model gets exactly the same brief: the fake brain answers
`[scene:<kind>|<title>] <brief>` with `<scene kind="<kind>" title="<title>"><brief></scene>`. Only the codegen
capability is real.

## Run it

1. A throw-away backend whose brain is fake and whose codegen is the model to measure (here the cloud preset,
   `claude-opus-5-5` at effort medium; the API key comes from your environment, never from this folder):

   ```powershell
   $env:CLICKY_HOME = "$env:TEMP\clicky-scene-eval"; New-Item -ItemType Directory -Force $env:CLICKY_HOME
   Set-Content "$env:CLICKY_HOME\clicky.toml" "profile = `"cloud`"`n[profiles.cloud.capabilities.brain]`nprovider = `"fake`""
   $env:CLICKY_AUTH_TOKEN = 'scene-eval-token-0123456789'
   $env:CLICKY_ALLOWED_EXTENSION_IDS = 'fhgliaahcafninaodeelekpdemjkifmf'
   uv run clicky serve --port 18766 --profile cloud
   ```

   For a local model use `profile = "local"` and `[profiles.local.capabilities.brain]` instead (codegen is then the
   local preset, `qwen3.5:4b`), or override `[profiles.<name>.capabilities.codegen]` to try another model.

2. The suite (the extension build must be current):

   ```powershell
   cd extension
   $env:CC_SCENE_EVAL_BACKEND = '18766:scene-eval-token-0123456789'
   $env:CC_SCENE_EVAL_OUT = "$env:TEMP\scene-eval-out"       # results JSON + a screenshot of every stage
   npx playwright test scene-eval
   ```

   `CC_SCENE_EVAL_CASES=solar-system,tcp` runs a subset. Each case prints `RENDERED`/`FAILED`, the attempts, the
   codegen time and the total time from the request to the final state.

A Claude Opus 5.5 run of all 10 cases costs roughly $1-2 (2-8k output tokens per scene at effort medium).
