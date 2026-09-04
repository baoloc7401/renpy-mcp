from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import sys
from pathlib import Path

from .config import DEFAULT_GAMES_SUBDIR, DEFAULT_PROJECT_SLUG, DEFAULT_TIERS, ServerConfig
from .project import sdk_fetch
from .project.scaffold import scaffold_project
from .server import run_stdio


def _configure_logging(verbose: bool) -> None:
    """Send all logging to stderr — stdout is reserved for the MCP wire."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_tiers(raw: str) -> frozenset[int]:
    tiers = {int(x) for x in raw.split(",") if x.strip()}
    bad = tiers - {1, 2, 3, 4}
    if bad:
        raise argparse.ArgumentTypeError(f"unknown tiers: {sorted(bad)}; pick from 1-4")
    return frozenset(tiers)


def _default_sdk() -> Path | None:
    """Return the SDK path from $RENPY_SDK, the local cache, or None.

    Lookup order:
        1. $RENPY_SDK (explicit user choice).
        2. The most-recent SDK previously placed in the cache by
           `renpy-mcp --fetch-sdk`. Lets users run `--fetch-sdk` once
           and never need to set the env var.
    """
    env = os.environ.get("RENPY_SDK")
    if env:
        return Path(env).resolve()
    cached = sdk_fetch.cached_sdk()
    return cached.resolve() if cached else None


def resolve_project_root(
    *,
    explicit_project: Path | None,
    cwd: Path,
    games_root: Path,
    sdk_root: Path,
    log: logging.Logger,
) -> Path | None:
    """Resolve `project_root` for startup — WITHOUT ever writing to disk on
    its own initiative. Returns ``None`` when nothing should be bound yet;
    the caller is responsible for leaving the server in an explicitly
    unbound state in that case (see ``main()``).

    Priority, highest first:
        1. ``explicit_project`` (already merged from ``--project`` /
           $RENPY_MCP_PROJECT_ROOT by the caller — ``--project`` wins if
           both are set). Binds directly to that path, no games_root
           shaping required; scaffolds it in place if not already a
           project. This is a deliberate, user-specified target — the one
           case where scaffolding here is still appropriate, because the
           operator explicitly named this exact path.
        2. ``cwd`` itself, when it's already a Ren'Py project root
           (``cwd/game/script.rpy`` exists) — covers launching the server
           from inside an existing project's checkout with no flags. No
           write happens: the project is already there.
        3. Otherwise: ``None``. The server starts unbound rather than
           silently scaffolding ``games_root/default/`` under whatever
           directory happened to be ``cwd`` — that behavior used to fire
           in *any* directory the server was launched from, including
           ones with no relation to Ren'Py at all, writing real files into
           unrelated git repos with no user action and no audit trail.
           Binding now requires an explicit ``new_project``/`bind_project`
           tool call (or ``--project``/`$RENPY_MCP_PROJECT_ROOT`` next
           startup) — every tool but those two reports "no project bound"
           until then.
    """
    if explicit_project is not None:
        project_root = explicit_project.resolve()
        if not (project_root / "game" / "script.rpy").is_file():
            summary = scaffold_project(project_root, sdk_root=sdk_root)
            log.info("startup: %s", summary)
        return project_root

    if (cwd / "game" / "script.rpy").is_file():
        log.info("startup: binding directly to existing project at cwd: %s", cwd)
        return cwd

    return None


def main() -> int:
    parser = argparse.ArgumentParser(prog="renpy-mcp")
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help=(
            "Path to a Ren'Py project root (the dir containing game/) — "
            "any directory, not just one under --games-root. Also settable "
            "via $RENPY_MCP_PROJECT_ROOT (this flag wins if both are set). "
            "Optional: if omitted and $RENPY_MCP_PROJECT_ROOT is unset, the "
            "server binds directly to <cwd> when <cwd>/game/script.rpy "
            "already exists; otherwise it starts UNBOUND (no directory is "
            "created automatically) until an agent calls `new_project` or "
            "`bind_project`."
        ),
    )
    parser.add_argument(
        "--games-root",
        type=Path,
        default=None,
        help=(
            "Directory under which `new_project` drops new projects. "
            f"Defaults to `<cwd>/{DEFAULT_GAMES_SUBDIR}/`."
        ),
    )
    parser.add_argument(
        "--sdk",
        type=Path,
        default=None,
        help=(
            "Path to the Ren'Py SDK (the dir containing renpy.sh). "
            "Optional: falls back to $RENPY_SDK when unset."
        ),
    )
    parser.add_argument(
        "--tiers",
        type=_parse_tiers,
        default=DEFAULT_TIERS,
        help="comma-separated tier list to load (default: 1,2,3)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level stderr logging")
    parser.add_argument(
        "--print-config",
        choices=["claude-code", "hermes"],
        default=None,
        help=(
            "Print a ready-to-paste MCP-server config snippet for the named "
            "harness, using the current --sdk / --project / --games-root "
            "values, then exit. `claude-code` emits .mcp.json; `hermes` "
            "emits the YAML block to merge into ~/.hermes/config.yaml."
        ),
    )
    parser.add_argument(
        "--fetch-sdk",
        action="store_true",
        help=(
            "Download a Ren'Py SDK into ~/.cache/renpy-mcp/sdk-<version> "
            "(or $RENPY_MCP_SDK_CACHE) and exit. Subsequent `renpy-mcp` "
            "invocations pick the cached SDK up automatically when --sdk "
            "and $RENPY_SDK are unset. Combine with --sdk-version to pin."
        ),
    )
    parser.add_argument(
        "--sdk-version",
        default=None,
        help=(
            "Version string for --fetch-sdk (e.g. `8.6.0`). Default: probe "
            "renpy.org for the highest 8.x release."
        ),
    )
    args = parser.parse_args()

    if args.print_config:
        return _print_harness_config(args)
    if args.fetch_sdk:
        _configure_logging(args.verbose)
        return _fetch_sdk_cli(args)

    _configure_logging(args.verbose)
    log = logging.getLogger("renpy_mcp")

    cwd = Path.cwd()
    games_root = (args.games_root or (cwd / DEFAULT_GAMES_SUBDIR)).resolve()
    sdk_root = (args.sdk or _default_sdk())
    if sdk_root is None:
        log.error(
            "startup: --sdk is required (or set $RENPY_SDK); point it at the Ren'Py SDK directory"
        )
        return 2

    env_project = os.environ.get("RENPY_MCP_PROJECT_ROOT")
    explicit_project = args.project or (Path(env_project) if env_project else None)

    project_root = resolve_project_root(
        explicit_project=explicit_project,
        cwd=cwd,
        games_root=games_root,
        sdk_root=sdk_root.resolve(),
        log=log,
    )
    bound = project_root is not None
    if not bound:
        # Unbound: `ServerConfig.project_root` still needs *some* Path (the
        # field isn't Optional), but this one is a placeholder that is
        # never created here — nothing on this branch touches disk. It
        # only becomes real if an agent later calls `new_project` (which
        # scaffolds under `games_root`) or `bind_project` (which points
        # `project_root` somewhere else entirely).
        project_root = (games_root / DEFAULT_PROJECT_SLUG).resolve()
        log.info(
            "startup: no --project/$RENPY_MCP_PROJECT_ROOT given and cwd is "
            "not a Ren'Py project; starting UNBOUND. Every tool but "
            "new_project/bind_project will report 'no project bound' until "
            "one of them is called — nothing is written to disk yet."
        )

    config = ServerConfig(
        project_root=project_root,
        sdk_root=sdk_root.resolve(),
        tiers=args.tiers,
        games_root=games_root,
    )
    try:
        config.validate(require_project=bound)
    except ValueError as exc:
        log.error("startup: %s", exc)
        return 2

    log.info(
        "starting stdio server | project=%s games_root=%s sdk=%s tiers=%s",
        config.project_root,
        config.games_root,
        config.sdk_root,
        sorted(config.tiers),
    )
    asyncio.run(run_stdio(config))
    return 0


def _print_harness_config(args: argparse.Namespace) -> int:
    """Emit a ready-to-paste MCP-server config snippet for `args.print_config`.

    Resolves the same default values the server itself would resolve so the
    snippet works without further user editing. Goes to stdout (not stderr —
    this output is meant to be piped into a config file). Never spawns the
    server; just prints and returns 0.
    """
    cwd = Path.cwd()
    # Use sys.executable as-is — resolving the symlink chain on a venv
    # python lands at the system python, which doesn't have renpy_mcp
    # installed. The venv's `bin/python` is the path we want the harness
    # to launch.
    python = Path(sys.executable)
    sdk = args.sdk or _default_sdk()
    games_root = (args.games_root or (cwd / DEFAULT_GAMES_SUBDIR)).resolve()
    project = args.project.resolve() if args.project else None

    extra_args: list[str] = ["-m", "renpy_mcp"]
    if sdk is not None:
        extra_args += ["--sdk", str(sdk.resolve())]
    extra_args += ["--games-root", str(games_root)]
    if project is not None:
        extra_args += ["--project", str(project)]
    if args.tiers != DEFAULT_TIERS:
        extra_args += ["--tiers", ",".join(str(t) for t in sorted(args.tiers))]

    if args.print_config == "claude-code":
        snippet = {
            "mcpServers": {
                "renpy": {
                    "type": "stdio",
                    "command": str(python),
                    "args": extra_args,
                }
            }
        }
        print("// Drop this in .mcp.json next to the directory you open Claude Code in.")
        print("// Auto-loaded on session start; tools register as mcp__renpy__<tool>.")
        print(json.dumps(snippet, indent=2))
        if sdk is None:
            print(
                "// NOTE: --sdk was omitted; set $RENPY_SDK, run "
                "`renpy-mcp --fetch-sdk` once to populate the cache, "
                "or rerun --print-config with --sdk PATH.",
                file=sys.stderr,
            )
        return 0

    # hermes-agent: YAML block to merge into ~/.hermes/config.yaml.
    yaml_args = "\n      ".join(f"- {shlex.quote(a)}" for a in extra_args)
    print("# Merge into ~/.hermes/config.yaml under `mcp_servers:`. After")
    print("# editing, run `hermes mcp test renpy` to verify the connection.")
    print("mcp_servers:")
    print("  renpy:")
    print(f"    command: {shlex.quote(str(python))}")
    print(f"    args:")
    print(f"      {yaml_args}")
    print("    timeout: 180")
    print("    connect_timeout: 60")
    if sdk is None:
        print(
            "# NOTE: --sdk was omitted; set $RENPY_SDK in hermes' .env, run "
            "`renpy-mcp --fetch-sdk` once to populate the cache, or re-run "
            "print-config with --sdk PATH.",
            file=sys.stderr,
        )
    return 0


def _fetch_sdk_cli(args: argparse.Namespace) -> int:
    """Run `--fetch-sdk` end-to-end and print the resolved SDK path."""
    log = logging.getLogger("renpy_mcp")
    try:
        result = sdk_fetch.fetch_sdk(version=args.sdk_version)
    except sdk_fetch.SDKFetchError as exc:
        log.error("fetch-sdk failed: %s", exc)
        return 2
    if result.cached:
        log.info("Ren'Py SDK %s already cached at %s", result.version, result.sdk_path)
    else:
        log.info("Ren'Py SDK %s installed at %s", result.version, result.sdk_path)
    # stdout receives the path so shell pipelines can capture it.
    print(result.sdk_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
