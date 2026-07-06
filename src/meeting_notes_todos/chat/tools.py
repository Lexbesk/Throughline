"""Proposal tool definitions for the chat assistant (v2 §4.2, §4.4).

Anthropic-shaped definitions (``name`` / ``description`` / ``input_schema``); the
local provider translates them to OpenAI function-calling format. The tools are
never executed — every call the model emits is parsed into a *staged proposal*
that the user accepts, edits, or rejects (§4.3).
"""

from __future__ import annotations

_STAGED = " This stages a proposal for the user to approve; it does not change anything by itself."

TOOL_DEFS: list[dict] = [
    {
        "name": "propose_new",
        "description": "Propose adding a new task to the list." + _STAGED,
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short imperative title"},
                "description": {"type": "string", "description": "Optional extra context"},
                "owner": {
                    "type": "string",
                    "description": "Person responsible — only if the user stated one",
                },
                "due_date_text": {
                    "type": "string",
                    "description": "Deadline phrase as the user said it (e.g. 'by Friday'); "
                    "do not compute a date",
                },
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "source_snippet": {
                    "type": "string",
                    "description": "For items extracted from pasted text: the exact input "
                    "text this item came from",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "propose_update",
        "description": "Propose changing fields on an existing task. Pass only the fields "
        "that should change." + _STAGED,
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Exact id of the task to change"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "owner": {"type": "string"},
                "due_date_text": {
                    "type": "string",
                    "description": "New deadline phrase as the user said it; do not compute a date",
                },
                "project": {"type": "string"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "propose_delete",
        "description": "Propose removing a task from the list (a soft delete: the task "
        "moves to the user's Deleted list and can be restored)." + _STAGED,
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Exact id of the task to remove"}
            },
            "required": ["id"],
        },
    },
    {
        "name": "propose_merge",
        "description": "Propose collapsing two entries that are the same task into one. "
        "The task with keep_id survives; the one with absorb_id is folded into it and "
        "removed. Use only when the two items are clearly the same underlying work."
        + _STAGED,
        "input_schema": {
            "type": "object",
            "properties": {
                "keep_id": {"type": "string", "description": "Exact id of the task to keep"},
                "absorb_id": {
                    "type": "string",
                    "description": "Exact id of the duplicate to fold into the kept task",
                },
                "title": {
                    "type": "string",
                    "description": "Optional better title for the merged task",
                },
                "description": {
                    "type": "string",
                    "description": "Optional combined description for the merged task",
                },
            },
            "required": ["keep_id", "absorb_id"],
        },
    },
    {
        "name": "reprioritize",
        "description": "Propose changing a task's priority and/or its position in the list "
        "(position 0 = top). Pass at least one of priority or position." + _STAGED,
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Exact id of the task"},
                "priority": {
                    "type": "string",
                    "enum": ["high", "medium", "low", "none"],
                    "description": "'none' clears the priority",
                },
                "position": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Target position in the list, 0 = top",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "propose_complete",
        "description": "Propose marking a task done." + _STAGED,
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Exact id of the task to mark done"}
            },
            "required": ["id"],
        },
    },
    {
        "name": "propose_profile_update",
        "description": "Propose replacing ONE named section of the user's profile, when the "
        "input genuinely reveals something durable (a stated goal, a shifted focus or "
        "priority, a lasting constraint). Canonical sections: 'Long-term goals', "
        "'Current focus', 'Priorities & values', 'Working style & personality', "
        "'Context & constraints' — add a new section only if nothing fits. Pass the "
        "complete replacement text for that section: fold the new fact in, keep what "
        "still holds, drop nothing the user didn't retract, and keep it to a few short "
        "lines. Never include facts the user didn't state." + _STAGED,
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "The profile section to update, e.g. 'Long-term goals'",
                },
                "new_text": {
                    "type": "string",
                    "description": "The complete replacement text for that section "
                    "(a few short lines)",
                },
            },
            "required": ["section", "new_text"],
        },
    },
]
