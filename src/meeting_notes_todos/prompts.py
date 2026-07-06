"""Prompt loading with simple ``{slot}`` substitution (build plan §4.2)."""

from __future__ import annotations

from pathlib import Path


def load_prompt(prompts_dir: str | Path, filename: str, **slots: str) -> str:
    """Read a prompt file and substitute ``{slot}`` placeholders.

    Substitution is plain string replacement so prompts stay readable and
    editable without touching code. Slots not present in the file are no-ops;
    placeholders with no matching slot are left untouched.
    """
    text = (Path(prompts_dir) / filename).read_text(encoding="utf-8")
    for key, value in slots.items():
        text = text.replace("{" + key + "}", value)
    return text
