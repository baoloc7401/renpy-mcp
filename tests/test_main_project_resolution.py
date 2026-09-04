"""Tests for `renpy_mcp.__main__.resolve_project_root` — the startup
logic that decides which directory the server binds to before the first
tool call.

Covers the priority order: explicit override (--project /
$RENPY_MCP_PROJECT_ROOT, merged by the caller before this function runs)
> cwd itself when it's already a Ren'Py project root > unbound (`None`).
The last case used to silently auto-scaffold `games_root/default/` under
whatever directory happened to be `cwd` — including directories with no
relation to Ren'Py at all — which wrote real files into unrelated repos
with no user action. It now creates nothing and returns `None`; binding
only happens via an explicit `new_project`/`bind_project` tool call.
This is what a stdio-server smoke test would exercise, minus actually
spawning the asyncio server (which would just block on stdin).
"""

from __future__ import annotations

import logging
from pathlib import Path

from renpy_mcp.__main__ import resolve_project_root

log = logging.getLogger("test")


def _write_project(root: Path) -> None:
    game = root / "game"
    game.mkdir(parents=True)
    (game / "script.rpy").write_text("label start:\n    return\n")


def test_explicit_project_wins_and_is_not_scaffolded_when_present(tmp_path: Path):
    existing = tmp_path / "elsewhere" / "myrepo"
    _write_project(existing)
    before = (existing / "game" / "script.rpy").read_bytes()

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    games_root = cwd / "games"

    result = resolve_project_root(
        explicit_project=existing,
        cwd=cwd,
        games_root=games_root,
        sdk_root=tmp_path,  # unused: no SDK template available
        log=log,
    )

    assert result == existing.resolve()
    assert (existing / "game" / "script.rpy").read_bytes() == before
    # No games/default/ fallback should have been created.
    assert not games_root.exists()


def test_explicit_project_is_scaffolded_in_place_when_missing(tmp_path: Path):
    target = tmp_path / "brand_new"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    games_root = cwd / "games"

    result = resolve_project_root(
        explicit_project=target,
        cwd=cwd,
        games_root=games_root,
        sdk_root=tmp_path,
        log=log,
    )

    assert result == target.resolve()
    assert (target / "game" / "script.rpy").is_file()
    # Scaffolding happened at the explicit target, not under games_root.
    assert not games_root.exists()


def test_cwd_already_a_project_binds_directly_without_games_default(tmp_path: Path):
    cwd = tmp_path / "existing_repo"
    _write_project(cwd)
    games_root = cwd / "games"

    result = resolve_project_root(
        explicit_project=None,
        cwd=cwd,
        games_root=games_root,
        sdk_root=tmp_path,
        log=log,
    )

    assert result == cwd
    # The fallback games/default/ must never be created when cwd itself
    # is already a valid project — this is the exact bug scenario: an
    # agent running the server from inside an existing large repo.
    assert not games_root.exists()


def test_no_override_and_cwd_not_a_project_returns_unbound(tmp_path: Path):
    """The exact bug scenario from the field report: the server is launched
    (e.g. as a global/user-scoped MCP server) from inside some unrelated
    directory — an npm project, a random git repo, anything that isn't a
    Ren'Py project — with no `--project` and no `$RENPY_MCP_PROJECT_ROOT`.
    It must come back unbound and must not create anything on disk; the
    caller (`main()`) is responsible for leaving the server in that state
    until an agent explicitly calls `new_project`/`bind_project`."""
    cwd = tmp_path / "unrelated_npm_project"
    cwd.mkdir()
    games_root = cwd / "games"

    result = resolve_project_root(
        explicit_project=None,
        cwd=cwd,
        games_root=games_root,
        sdk_root=tmp_path,
        log=log,
    )

    assert result is None
    assert not games_root.exists()
    # Nothing under cwd at all — not games/, not games/default/, nothing.
    assert list(cwd.iterdir()) == []


def test_repeated_unbound_startups_stay_side_effect_free(tmp_path: Path):
    cwd = tmp_path / "empty_cwd"
    cwd.mkdir()
    games_root = cwd / "games"

    for _ in range(3):
        result = resolve_project_root(
            explicit_project=None, cwd=cwd, games_root=games_root, sdk_root=tmp_path, log=log
        )
        assert result is None
        assert not games_root.exists()


async def test_unbound_registry_rejects_read_tools_and_writes_nothing(tmp_path: Path):
    """Full-stack regression for the reported bug: wire up the real server
    (`server.build_server`, exactly what `main()` runs) around a config left
    unbound the way startup now leaves it — `project_root` pointing at a
    `games/default/` that was never created — and confirm that calling the
    two read-only tools the field report used to discover the auto-scaffold
    (`get_project_overview`, `get_scaffold_status`) returns a "no project
    bound" error instead of a snapshot, and creates nothing on disk.
    """
    from renpy_mcp.config import ServerConfig
    from renpy_mcp.server import build_server

    from .conftest import parse

    cwd = tmp_path / "unrelated_repo"
    cwd.mkdir()
    games_root = cwd / "games"
    placeholder_project = (games_root / "default").resolve()

    config = ServerConfig(project_root=placeholder_project, sdk_root=tmp_path, games_root=games_root)
    assert not config.is_bound()

    _, registry = build_server(config)

    for tool_name in ("get_project_overview", "get_scaffold_status"):
        out = parse(await registry.call(tool_name, {}))
        assert out["error"] == "no project bound"
        assert "new_project" in out["hint"]
        assert "bind_project" in out["hint"]

    # Nothing was created anywhere — not games/, not games/default/, nothing.
    assert not games_root.exists()
    assert list(cwd.iterdir()) == []
