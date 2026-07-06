"""Per-run token-usage logging (build plan §4.4: make cost observable).

Each LLM run appends one JSON line to a usage log; ``summarize`` totals it. Logging
is best-effort — it must never break a run.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .providers.base import Usage


def record_run(
    path: str | Path,
    *,
    command: str,
    provider: str,
    model: str,
    usage: Usage,
) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "provider": provider,
        "model": model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:  # pragma: no cover - logging must never break a run
        pass


def summarize(path: str | Path) -> dict:
    path = Path(path)
    total = {"runs": 0, "input_tokens": 0, "output_tokens": 0, "by_model": {}}
    if not path.exists():
        return total
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        inp = int(entry.get("input_tokens", 0))
        out = int(entry.get("output_tokens", 0))
        total["runs"] += 1
        total["input_tokens"] += inp
        total["output_tokens"] += out
        model = total["by_model"].setdefault(
            entry.get("model", "?"), {"runs": 0, "input_tokens": 0, "output_tokens": 0}
        )
        model["runs"] += 1
        model["input_tokens"] += inp
        model["output_tokens"] += out
    return total
