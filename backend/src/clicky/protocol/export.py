"""Export the wire-protocol + worker-IPC JSON Schema.

    uv run python -m clicky.protocol.export          # write backend/protocol.schema.json
    uv run python -m clicky.protocol.export --check  # exit 1 if the committed file is stale (drift gate)

The extension generates src/lib/protocol.gen.ts from this file (npm run gen:protocol).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from clicky.protocol.events import ClientEvent, ServerEvent
from clicky.protocol.worker_ipc import WorkerCommand, WorkerEvent

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "protocol.schema.json"
_DISCRIMINATORS = ("type", "kind")


class ClickyTabsProtocol(BaseModel):
    """Bundle model: one property per message family so every model lands in $defs."""

    client: ClientEvent
    server: ServerEvent
    worker_command: WorkerCommand
    worker_event: WorkerEvent


def build_schema() -> dict[str, Any]:
    schema = ClickyTabsProtocol.model_json_schema(ref_template="#/$defs/{model}")
    # Discriminators have defaults in Python (ergonomic constructors) but must be *required* on the wire so that
    # generated TypeScript unions narrow correctly.
    for definition in schema.get("$defs", {}).values():
        props = definition.get("properties", {})
        for name in _DISCRIMINATORS:
            if "const" in props.get(name, {}):
                required = definition.setdefault("required", [])
                if name not in required:
                    required.insert(0, name)
    return schema


def render() -> str:
    return json.dumps(build_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write(path: Path = SCHEMA_PATH) -> Path:
    path.write_text(render(), encoding="utf-8", newline="\n")
    return path


def is_current(path: Path = SCHEMA_PATH) -> bool:
    try:
        return path.read_text(encoding="utf-8") == render()
    except FileNotFoundError:
        return False


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--check" in args:
        if is_current():
            print(f"{SCHEMA_PATH.name} is up to date")
            return 0
        print(f"{SCHEMA_PATH} is stale - run: uv run clicky schema export", file=sys.stderr)
        return 1
    print(f"wrote {write()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
