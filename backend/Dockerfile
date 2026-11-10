# syntax=docker/dockerfile:1
# ClickyTabs backend image: the orchestrator with the local speech extras and the Claude/OpenAI SDKs, plus the
# browser-task worker with headless Chromium. Build context: backend/ (see ../compose.yaml).
#
# Everything the backend writes (pairing token, ui-settings.json, Kokoro / Hugging Face models, logs) goes to the
# /data volume (CLICKY_HOME). API keys come from the environment (compose reads backend/.env), never from the image.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv

# Chromium for browser tasks (headless here; browser-use adds its Docker flags, e.g. --no-sandbox, by itself).
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first (cached until the lock files change), then the code. Two environments, as from source:
# /app/.venv (backend) and /app/automation-worker/.venv (browser-use and its own pins).
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --locked --no-dev --no-install-project --extra local --extra kokoro --extra cloud
COPY automation-worker/pyproject.toml automation-worker/uv.lock automation-worker/README.md automation-worker/
RUN uv sync --locked --no-dev --no-install-project --project automation-worker
COPY src ./src
COPY automation-worker/src automation-worker/src
RUN uv sync --locked --no-dev --extra local --extra kokoro --extra cloud \
    && uv sync --locked --no-dev --project automation-worker \
    && test -x automation-worker/.venv/bin/python

RUN useradd --create-home --uid 10001 clicky && mkdir -p /data && chown clicky:clicky /data
USER clicky

ENV PATH=/app/.venv/bin:$PATH \
    CLICKY_HOME=/data \
    CLICKY_HOST=0.0.0.0 \
    CLICKY_PORT=8765 \
    CLICKY_AUTOMATION_HEADLESS=true \
    CLICKY_AUTOMATION_BROWSER=/usr/bin/chromium \
    HF_HOME=/data/huggingface

VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"]
CMD ["clicky", "serve"]
