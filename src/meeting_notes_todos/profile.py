"""User profile (v2 M9, structured in v3 M11): a human-editable ``profile.md``
beside the store — the app's north star.

v3 gives the file a light **section schema** (v3 plan §5): markdown ``##``
headers key the five must-have sections below, and the assistant *proposes*
updates to one named section at a time (``propose_profile_update``), written
only after explicit approval — never silently. That gate is the guard against
profile drift and junk accumulation; each section stays a few tight lines
(``SECTION_CAP``) because the whole profile is injected into every turn.
The parse is deliberately light (split on ``## `` headers) so the file stays
a plain, hand-editable markdown document.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

NO_PROFILE = "(no profile provided)"

# the five must-have sections (v3 plan §5.1) with their seed placeholder hints
MUST_HAVE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Long-term goals",
     "_(2–5 durable directions you're steering toward — statements, not checkboxes.)_"),
    ("Current focus",
     "_(The medium-term themes and active efforts bridging daily todos and goals.)_"),
    ("Priorities & values",
     "_(How you weigh competing demands — what matters most right now.)_"),
    ("Working style & personality",
     "_(How you operate and how the assistant should tailor its delivery.)_"),
    ("Context & constraints",
     "_(Durable situational facts: role, life stage, commitments, hard limits.)_"),
)

SECTION_CAP = 600  # max chars per assistant-proposed section body — keep it tight (§5)


def profile_path(store_path: str | Path) -> Path:
    """The profile lives beside the todo store (v2 plan §6)."""
    return Path(store_path).parent / "profile.md"


def load_profile(store_path: str | Path) -> str | None:
    path = profile_path(store_path)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def save_profile(store_path: str | Path, text: str) -> Path:
    path = profile_path(store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n" if text.strip() else "", encoding="utf-8")
    return path


# --- section schema (v3 M11) --------------------------------------------------


def parse_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split into ``(preamble, [(header, body), ...])`` on ``## `` headers.

    Light and round-trippable: anything before the first header is preamble;
    bodies keep their markdown as-is (blank-line normalization happens on
    render). Hand-written custom sections survive untouched.
    """
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif not sections:
            preamble.append(line)
        else:
            sections[-1][1].append(line)
    return (
        "\n".join(preamble).strip(),
        [(name, "\n".join(lines).strip()) for name, lines in sections],
    )


def render_sections(preamble: str, sections: list[tuple[str, str]]) -> str:
    parts = [preamble] if preamble else []
    for name, body in sections:
        parts.append(f"## {name}" + (f"\n\n{body}" if body else ""))
    return "\n\n".join(parts) + "\n" if parts else ""


def default_profile() -> str:
    """The seeded profile: the five must-have sections with placeholder hints."""
    return render_sections("# Profile", list(MUST_HAVE_SECTIONS))


def section_body(text: str | None, section: str) -> str | None:
    """The named section's current body (case-insensitive), or None if absent."""
    if not text:
        return None
    for name, body in parse_sections(text)[1]:
        if name.lower() == section.strip().lower():
            return body
    return None


def replace_section(text: str, section: str, new_text: str) -> str:
    """Pure text transform: replace one named section's body (creating the section
    if needed). All other content is preserved."""
    preamble, sections = parse_sections(text)
    section = section.strip()
    out: list[tuple[str, str]] = []
    replaced = False
    for name, body in sections:
        if name.lower() == section.lower():
            out.append((name, new_text.strip()))
            replaced = True
        else:
            out.append((name, body))
    if not replaced:
        out.append((section, new_text.strip()))
    return render_sections(preamble, out)


def update_section(store_path: str | Path, section: str, new_text: str) -> Path:
    """File-backed section update (kept for the CLI and as the FileProfile core)."""
    text = load_profile(store_path) or default_profile()
    return save_profile(store_path, replace_section(text, section, new_text))


# --- backends (v4 M16): the same profile, file-backed or per-user in Postgres ---


class ProfileBackend(ABC):
    """Thin storage interface for the profile document. All the v3 section
    machinery (parse/render/replace) operates on the text, so backends only
    load and save whole documents — the same swap the item store made."""

    @abstractmethod
    def load(self) -> str | None:
        """The profile text, or None if none exists yet."""

    @abstractmethod
    def save(self, text: str) -> None:
        """Write the full profile document."""

    def update_section(self, section: str, new_text: str) -> None:
        """Replace one named section (seeding the template on a fresh profile)."""
        text = self.load() or default_profile()
        self.save(replace_section(text, section, new_text))


class FileProfile(ProfileBackend):
    """The v1–v3 behavior: ``profile.md`` beside the todo store."""

    def __init__(self, store_path: str | Path) -> None:
        self._store_path = store_path

    def load(self) -> str | None:
        return load_profile(self._store_path)

    def save(self, text: str) -> None:
        save_profile(self._store_path, text)


class PostgresProfile(ProfileBackend):
    """Per-user profile row (v4 M16). Scoped by ``user_id`` on every query —
    the same hard-isolation rule as the item store."""

    def __init__(self, pool, user_id: str) -> None:
        self.pool = pool
        self.user_id = user_id

    def load(self) -> str | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT content FROM profiles WHERE user_id = %s", (self.user_id,)
            ).fetchone()
        if row is None:
            return None
        text = row[0].strip()
        return text or None

    def save(self, text: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO profiles (user_id, content) VALUES (%s, %s)"
                " ON CONFLICT (user_id) DO UPDATE"
                " SET content = EXCLUDED.content, updated_at = now()",
                (self.user_id, text.strip()),
            )
