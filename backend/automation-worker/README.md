# clicky-worker

The browser-automation worker for ClickyTabs. It runs [browser-use](https://github.com/browser-use/browser-use)
in its **own** uv environment because browser-use pins exact dependency versions that would otherwise hold back the
backend (`pydantic==2.13.5`, `ollama==0.6.1`, `anthropic<2`). It also isolates crashes and keeps the user's session
cookies inside a single, killable process.

The backend spawns it (`clicky.automation.worker_client`) and talks JSON lines over stdin/stdout:

- one UTF-8 JSON object per line; schema = `backend/src/clicky/protocol/worker_ipc.py` (mirrored in
  `src/clicky_worker/ipc.py`, kept identical by `backend/tests/test_worker_ipc.py`);
- stdout carries IPC only: at start-up the worker re-routes fd 1 to stderr so stray `print()`s from dependencies
  cannot corrupt the stream;
- API keys arrive through the environment only.

Install once with `uv sync` here (about 240 MB of packages, no models). The backend then runs
the environment's Python with `-m clicky_worker`, one process per browser task (on Windows inside a Job
Object that kills it with the backend; elsewhere in its own process session).

- `runner.py`: browser-use's Agent in a fresh temp profile (deleted afterwards) seeded with the consented
  `storage_state`, navigation pinned to the approved sites, the user's user agent and language, no extensions, no
  search / upload / PDF actions; per-step `task.step` with the agent's next goal and a small screenshot; a log
  filter that keeps cookie values out of the logs. A scripted run (`provider: fake`) replays the same events
  without a browser (tests, the fake profile).
- `llm_factory.py`: ChatAnthropic (adaptive thinking + effort), ChatOpenAI, ChatOllama (shared runner options).
- `hitl.py`: `ask_user` - the agent's question before irreversible steps, answered in the user's tab.
- `__main__.py`: the command loop (stdin on a thread, an asyncio task per browser task, so stop and answers reach it).
- `--stub` never imports browser-use: pings and the scripted task only.
