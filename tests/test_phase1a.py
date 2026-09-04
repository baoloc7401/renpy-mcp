"""Tests for Phase 1a diagnostics + refresh_project.

The fixture is intentionally lint-clean for jump targets and characters,
so every diagnostic test that needs a positive case mutates a per-test
copy of the fixture before calling the tool. This mirrors the per-test-
fixture pattern used by `test_tier2.py` and `test_phase0.py`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from renpy_mcp.config import ServerConfig
from renpy_mcp.project.label_tree import iter_statements, parse_label_body
from renpy_mcp.project.scanner import ProjectIndex
from renpy_mcp.tools import tier1_read
from renpy_mcp.tools.registry import ToolRegistry

from .conftest import FIXTURE_ROOT, SDK_ROOT, parse


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[ServerConfig, ToolRegistry, ProjectIndex]:
    """Per-test copy of the fixture so .rpy mutations don't leak."""
    proj = tmp_path / "tiny_project"
    shutil.copytree(FIXTURE_ROOT, proj)
    cfg = ServerConfig(project_root=proj.resolve(), sdk_root=SDK_ROOT)
    idx = ProjectIndex(cfg)
    reg = ToolRegistry()
    tier1_read.register(reg, cfg, idx)
    return cfg, reg, idx


# ---------- iter_statements walker ---------------------------------------------


def test_iter_statements_recurses_into_menus_and_ifs():
    body = (
        "    if x:\n"
        "        e \"in if\"\n"
        "    menu:\n"
        "        \"option\":\n"
        "            jump elsewhere\n"
        "    return\n"
    )
    tree = parse_label_body(body, body_start_line=1)
    kinds = [s["kind"] for s in iter_statements(tree["body"])]
    # Order: depth-first preorder. if → say in if → menu → jump in option → return.
    assert kinds == ["if", "say", "menu", "jump", "return"]


# ---------- find_invalid_jumps -------------------------------------------------


async def test_find_invalid_jumps_clean_fixture(registry):
    out = parse(await registry.call("find_invalid_jumps", {}))
    assert out["rule"] == "invalid_jump"
    assert out["count"] == 0
    assert out["diagnostics"] == []


async def test_find_invalid_jumps_flags_missing_target(workspace):
    cfg, reg, idx = workspace
    script = cfg.project_root / "game" / "script.rpy"
    text = script.read_text()
    # Replace the existing `jump cafe_scene` with a target that doesn't exist.
    new_text = text.replace("jump cafe_scene", "jump doesnt_exist", 1)
    script.write_text(new_text)
    idx.refresh()
    out = parse(await reg.call("find_invalid_jumps", {}))
    assert out["count"] == 1
    diag = out["diagnostics"][0]
    assert diag["rule"] == "invalid_jump"
    assert diag["severity"] == "error"
    assert diag["file"] == "game/script.rpy"
    assert diag["label"] == "start"
    assert "doesnt_exist" in diag["message"]


async def test_find_invalid_jumps_inside_if_branch(workspace):
    cfg, reg, idx = workspace
    # Add a label whose `if/else` body contains a bad jump in one branch.
    extra = cfg.project_root / "game" / "extra.rpy"
    extra.write_text(
        "label branchy:\n"
        "    if True:\n"
        "        jump nowhere_in_particular\n"
        "    else:\n"
        "        return\n"
    )
    idx.refresh()
    out = parse(await reg.call("find_invalid_jumps", {}))
    assert out["count"] == 1
    assert out["diagnostics"][0]["label"] == "branchy"


# ---------- find_undefined_characters ------------------------------------------


async def test_find_undefined_characters_clean_fixture(registry):
    out = parse(await registry.call("find_undefined_characters", {}))
    assert out["rule"] == "undefined_character"
    assert out["count"] == 0


async def test_find_undefined_characters_flags_missing_define(workspace):
    cfg, reg, idx = workspace
    extra = cfg.project_root / "game" / "extra.rpy"
    extra.write_text(
        'label ghost:\n'
        '    g "Boo, I have no define."\n'
        '    return\n'
    )
    idx.refresh()
    out = parse(await reg.call("find_undefined_characters", {}))
    assert out["count"] == 1
    diag = out["diagnostics"][0]
    assert diag["rule"] == "undefined_character"
    assert diag["severity"] == "error"
    assert diag["label"] == "ghost"
    assert "`g`" in diag["message"]


async def test_find_undefined_characters_ignores_narration(workspace):
    """Narration (no character tag) should NOT count as an undefined ref."""
    cfg, reg, idx = workspace
    extra = cfg.project_root / "game" / "extra.rpy"
    extra.write_text(
        'label narrative:\n'
        '    "Just narration here, no speaker."\n'
        '    return\n'
    )
    idx.refresh()
    out = parse(await reg.call("find_undefined_characters", {}))
    assert out["count"] == 0


# ---------- find_unused_characters ---------------------------------------------


async def test_find_unused_characters_clean_fixture(registry):
    """Both fixture characters speak, so nothing should be flagged."""
    out = parse(await registry.call("find_unused_characters", {}))
    assert out["count"] == 0


async def test_find_unused_characters_flags_silent_define(workspace):
    cfg, reg, idx = workspace
    extra = cfg.project_root / "game" / "extra.rpy"
    extra.write_text('define silent_one = Character("Quiet")\n')
    idx.refresh()
    out = parse(await reg.call("find_unused_characters", {}))
    names = {d["message"].split("`")[1] for d in out["diagnostics"]}
    assert "silent_one" in names
    diag = next(d for d in out["diagnostics"] if "silent_one" in d["message"])
    assert diag["severity"] == "warning"
    assert diag["file"] == "game/extra.rpy"


# ---------- character diagnostics: projects that don't use Character() ---------
#
# Real projects sometimes build every character through a custom class
# (a `Person` subclass, say) rather than `define x = Character(...)`. The
# scanner's `CharacterInfo` model only recognizes the literal `Character(`
# shape, so `snap.characters` ends up empty for such projects. Before this
# fix that meant `find_undefined_characters` flagged nearly every
# say-statement in the game (a multi-megabyte false-positive dump) while
# `find_unused_characters` silently reported a falsely-clean zero.


def _bare_workspace(tmp_path: Path) -> tuple[ServerConfig, ToolRegistry, ProjectIndex]:
    """A minimal from-scratch project with no Character() definitions at
    all — unlike `workspace`, this does NOT start from the fixture, whose
    `define e = Character(...)` would mask the empty-registry code path."""
    game = tmp_path / "proj" / "game"
    game.mkdir(parents=True)
    cfg = ServerConfig(project_root=tmp_path / "proj", sdk_root=SDK_ROOT)
    idx = ProjectIndex(cfg)
    reg = ToolRegistry()
    tier1_read.register(reg, cfg, idx)
    return cfg, reg, idx


async def test_find_undefined_characters_low_confidence_when_registry_empty(tmp_path):
    cfg, reg, idx = _bare_workspace(tmp_path)
    (cfg.project_root / "game" / "script.rpy").write_text(
        'label start:\n'
        '    jennifer "Hi there."\n'
        '    return\n'
    )
    idx.refresh()
    out = parse(await reg.call("find_undefined_characters", {}))
    assert out["count"] == 0
    assert out["diagnostics"] == []
    assert out["low_confidence"] is True
    assert "warning" in out
    assert "unreliable" in out["warning"]


async def test_find_undefined_characters_low_confidence_even_with_widened_matches(tmp_path):
    """Regression for the Lab Rats 2 report: a project can have ZERO
    literal `Character(...)` definitions while still having plenty of
    factory-bound names in the widened union (`*_ren.py` top-level names,
    other `define`/`default` statements). The trigger for low-confidence
    must be "no `Character()` defs" specifically — not "widened union is
    empty" — because a project that routes dialogue through a generic
    reassigned variable (`the_person = erica` ... `the_person "..."`)
    still has a non-empty widened union (`erica` is in it) while the
    actual say-tag (`the_person`) is never statically bound at all, which
    would otherwise flood the response with one repeated false positive
    thousands of times over."""
    cfg, reg, idx = _bare_workspace(tmp_path)
    people_dir = cfg.project_root / "game" / "people" / "Jennifer"
    people_dir.mkdir(parents=True)
    (people_dir / "jennifer_definition_ren.py").write_text(
        "class Person:\n"
        "    def __init__(self, name):\n"
        "        self.name = name\n"
        "\n"
        "def create_jennifer_character():\n"
        "    return Person('Jennifer')\n"
        "\n"
        "jennifer = create_jennifer_character()\n"
    )
    (cfg.project_root / "game" / "script.rpy").write_text(
        'define also_a_factory_binding = create_something()\n'
        'label start:\n'
        # `the_person` is never bound anywhere — a generic say-tag a real
        # project reassigns at runtime (`$ the_person = jennifer`), which
        # no static scan can trace.
        '    the_person "Hi there."\n'
        '    return\n'
    )
    idx.refresh()
    out = parse(await reg.call("find_undefined_characters", {}))
    assert out["count"] == 0
    assert out["diagnostics"] == []
    assert out["low_confidence"] is True
    assert "warning" in out


async def test_find_undefined_characters_widened_lookup_applies_once_registry_nonempty(tmp_path):
    """Once at least one real `Character(...)` definition exists, the
    widened lookup (other `define`/`default` statements, `*_ren.py`
    top-level names) still avoids false positives for factory-bound
    characters mixed into the same cast."""
    cfg, reg, idx = _bare_workspace(tmp_path)
    people_dir = cfg.project_root / "game" / "people" / "Jennifer"
    people_dir.mkdir(parents=True)
    (people_dir / "jennifer_definition_ren.py").write_text(
        "class Person:\n"
        "    def __init__(self, name):\n"
        "        self.name = name\n"
        "\n"
        "def create_jennifer_character():\n"
        "    return Person('Jennifer')\n"
        "\n"
        "jennifer = create_jennifer_character()\n"
    )
    (cfg.project_root / "game" / "script.rpy").write_text(
        'define e = Character("Eileen")\n'
        'define mara = create_mara_character()\n'
        'label start:\n'
        '    e "Hello."\n'
        '    mara "Hi from a non-Character() define."\n'
        '    jennifer "Hi from a *_ren.py factory binding."\n'
        '    return\n'
    )
    idx.refresh()
    out = parse(await reg.call("find_undefined_characters", {}))
    assert out.get("low_confidence") is not True
    assert out["count"] == 0


async def test_find_undefined_characters_tolerates_unparseable_ren_py_file(tmp_path):
    """A `*_ren.py` file with a syntax error must not crash the scan —
    just contribute nothing from that file."""
    cfg, reg, idx = _bare_workspace(tmp_path)
    (cfg.project_root / "game" / "broken_ren.py").write_text("def(:\n    not valid python\n")
    (cfg.project_root / "game" / "script.rpy").write_text(
        'define e = Character("Eileen")\n'
        'label start:\n'
        '    e "Hello."\n'
        '    return\n'
    )
    idx.refresh()
    out = parse(await reg.call("find_undefined_characters", {}))
    assert out.get("low_confidence") is not True
    assert out["count"] == 0


async def test_find_undefined_characters_still_flags_real_typos_when_registry_nonempty(tmp_path):
    """The widened detection must not swallow genuine undefined-character
    findings once there's at least one real binding to anchor on."""
    cfg, reg, idx = _bare_workspace(tmp_path)
    (cfg.project_root / "game" / "script.rpy").write_text(
        'define e = Character("Eileen")\n'
        'label start:\n'
        '    e "Hello."\n'
        '    zzz "This speaker was never defined anywhere."\n'
        '    return\n'
    )
    idx.refresh()
    out = parse(await reg.call("find_undefined_characters", {}))
    assert out.get("low_confidence") is not True
    assert out["count"] == 1
    assert "`zzz`" in out["diagnostics"][0]["message"]


async def test_find_undefined_characters_truncates_past_limit(tmp_path):
    cfg, reg, idx = _bare_workspace(tmp_path)
    lines = ['define e = Character("Eileen")', "label start:"]
    for i in range(250):
        lines.append(f'    ghost_{i} "line {i}"')
    lines.append("    return")
    (cfg.project_root / "game" / "script.rpy").write_text("\n".join(lines) + "\n")
    idx.refresh()
    out = parse(await reg.call("find_undefined_characters", {}))
    assert out["truncated"] is True
    assert out["total_found"] == 250
    assert out["count"] == 200
    assert len(out["diagnostics"]) == 200


async def test_find_unused_characters_low_confidence_when_no_character_defs(tmp_path):
    """Even when other detection sources find bindings (so
    `find_undefined_characters` would proceed normally), `find_unused_characters`
    has nothing shaped like `Character(...)` to check — it must say so
    rather than returning a falsely-clean empty result."""
    cfg, reg, idx = _bare_workspace(tmp_path)
    (cfg.project_root / "game" / "script.rpy").write_text(
        'define jennifer = create_jennifer_character()\n'
        'label start:\n'
        '    jennifer "Hi there."\n'
        '    return\n'
    )
    idx.refresh()
    out = parse(await reg.call("find_unused_characters", {}))
    assert out["count"] == 0
    assert out["low_confidence"] is True
    assert "warning" in out


# ---------- refresh_project ----------------------------------------------------


async def test_refresh_project_picks_up_external_changes(workspace):
    cfg, reg, idx = workspace
    # Take a snapshot via the index's pre-refresh count.
    before = parse(await reg.call("get_project_overview", {}))
    # Drop a new file out of band — without refreshing.
    (cfg.project_root / "game" / "extra.rpy").write_text(
        "label brand_new:\n    return\n"
    )
    # The cached snapshot still hides it.
    mid = parse(await reg.call("get_project_overview", {}))
    assert mid["counts"]["labels"] == before["counts"]["labels"]
    # refresh_project forces a re-scan.
    out = parse(await reg.call("refresh_project", {}))
    assert out["counts"]["labels"] == before["counts"]["labels"] + 1
    after = parse(await reg.call("get_project_overview", {}))
    assert "brand_new" in after["labels"]
