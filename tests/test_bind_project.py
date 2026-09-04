"""Tests for `bind_project` — the mid-session escape hatch for pointing
the server at an existing project that doesn't live under
`games_root/<slug>/` (e.g. a large pre-existing repo whose game sits
directly at `<repo_root>/game/`).
"""

from __future__ import annotations

from pathlib import Path

from .conftest import FIXTURE_ROOT, parse


def _write_other_project(root: Path, *, labels: int = 2) -> None:
    game = root / "game"
    game.mkdir(parents=True)
    body = "\n".join(f"label extra_{i}:\n    return" for i in range(labels))
    (game / "script.rpy").write_text(f"label start:\n    return\n\n{body}\n")


async def test_bind_project_switches_to_existing_root(registry, config, tmp_path):
    other = tmp_path / "some_other_repo"
    _write_other_project(other, labels=3)

    out = parse(await registry.call("bind_project", {"path": str(other)}))
    assert out["bound"] is True
    assert Path(out["project_root"]) == other.resolve()
    assert "already scaffolded" in out["summary"]
    # 1 `start` label + 3 extras.
    assert out["counts"]["labels"] == 4

    # The live config object was rebound in place.
    assert config.project_root == other.resolve()

    # Subsequent reads through the same registry now see the new project.
    overview = parse(await registry.call("get_project_overview", {}))
    assert overview["project_root"] == str(other.resolve())


async def test_bind_project_rejects_missing_path(registry, tmp_path):
    missing = tmp_path / "does_not_exist"
    out = parse(await registry.call("bind_project", {"path": str(missing)}))
    assert "error" in out
    assert "does not exist" in out["error"]


async def test_bind_project_rejects_unscaffolded_directory(registry, tmp_path):
    empty = tmp_path / "not_a_project"
    empty.mkdir()
    out = parse(await registry.call("bind_project", {"path": str(empty)}))
    assert "error" in out
    assert "new_project" in out["error"]


async def test_bind_project_does_not_mutate_target(registry, tmp_path):
    """Binding to an already-scaffolded project must not touch its files
    beyond the idempotent images/audio dir check `scaffold_project` does."""
    other = tmp_path / "untouched"
    _write_other_project(other)
    script = other / "game" / "script.rpy"
    before = script.read_bytes()

    await registry.call("bind_project", {"path": str(other)})
    assert script.read_bytes() == before


async def test_bind_project_leaves_original_project_on_disk(registry, tmp_path):
    """Binding away from the fixture project must not touch its files."""
    other = tmp_path / "another"
    _write_other_project(other)
    fixture_script = FIXTURE_ROOT / "game" / "script.rpy"
    before = fixture_script.read_bytes()

    await registry.call("bind_project", {"path": str(other)})
    assert fixture_script.read_bytes() == before
