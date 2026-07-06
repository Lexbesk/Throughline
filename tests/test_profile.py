"""User-profile module tests (v2 M9 + v3 M11 sections; pure file I/O, key-free)."""

from __future__ import annotations

from meeting_notes_todos.profile import (
    MUST_HAVE_SECTIONS,
    default_profile,
    load_profile,
    parse_sections,
    profile_path,
    render_sections,
    save_profile,
    section_body,
    update_section,
)

STRUCTURED = """# Profile

Intro line kept as preamble.

## Long-term goals

Ship the eval platform. Stay healthy.

## Current focus

Q3 eval report.

## My own custom section

Hand-written and sacred.
"""


def test_profile_lives_beside_the_store(tmp_path):
    assert profile_path(tmp_path / "todos.md") == tmp_path / "profile.md"


def test_load_save_round_trip(tmp_path):
    store_path = tmp_path / "todos.md"
    assert load_profile(store_path) is None  # missing file → no profile

    save_profile(store_path, "Role: eval lead. Project: Atlas.\n")
    assert load_profile(store_path) == "Role: eval lead. Project: Atlas."
    assert (tmp_path / "profile.md").exists()

    save_profile(store_path, "   ")  # clearing leaves an empty file → None
    assert load_profile(store_path) is None


# --- section schema (v3 M11) ---------------------------------------------------


def test_parse_and_render_round_trip():
    preamble, sections = parse_sections(STRUCTURED)
    assert preamble == "# Profile\n\nIntro line kept as preamble."
    assert [name for name, _ in sections] == [
        "Long-term goals", "Current focus", "My own custom section"
    ]
    assert sections[0][1] == "Ship the eval platform. Stay healthy."
    # render → parse is stable
    assert parse_sections(render_sections(preamble, sections)) == (preamble, sections)


def test_default_profile_seeds_the_five_sections():
    seeded = default_profile()
    for name, _hint in MUST_HAVE_SECTIONS:
        assert f"## {name}" in seeded
    assert len(parse_sections(seeded)[1]) == 5


def test_section_body_is_case_insensitive():
    assert section_body(STRUCTURED, "current FOCUS") == "Q3 eval report."
    assert section_body(STRUCTURED, "Nope") is None
    assert section_body(None, "Current focus") is None


def test_update_section_replaces_only_that_section(tmp_path):
    store_path = tmp_path / "todos.md"
    save_profile(store_path, STRUCTURED)

    update_section(store_path, "current focus", "Q3 shipped; now the v3 planning work.")

    text = load_profile(store_path)
    assert section_body(text, "Current focus") == "Q3 shipped; now the v3 planning work."
    assert section_body(text, "Long-term goals") == "Ship the eval platform. Stay healthy."
    assert section_body(text, "My own custom section") == "Hand-written and sacred."
    assert "Intro line kept as preamble." in text  # preamble survives


def test_update_section_appends_missing_and_seeds_new_profiles(tmp_path):
    store_path = tmp_path / "todos.md"
    save_profile(store_path, STRUCTURED)
    update_section(store_path, "Priorities & values", "Family first; depth over breadth.")
    assert section_body(load_profile(store_path), "Priorities & values") == (
        "Family first; depth over breadth."
    )

    # on a brand-new profile, the seeded five-section structure is created around it
    fresh = tmp_path / "fresh" / "todos.md"
    update_section(fresh, "Long-term goals", "Become a stronger writer.")
    text = load_profile(fresh)
    assert section_body(text, "Long-term goals") == "Become a stronger writer."
    assert section_body(text, "Context & constraints") is not None  # seeded hint present
