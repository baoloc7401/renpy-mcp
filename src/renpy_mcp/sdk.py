"""Subprocess shim around the Ren'Py SDK launcher.

We invoke `renpy.sh <project> <command>` (`renpy.exe` on Windows) for
engine operations the scanner cannot do itself: lint, force-recompile,
build, generate-translations.

Implementation note: uses ``asyncio.create_subprocess_exec`` (argv list, no
shell interpretation) so user-controlled values never reach a shell parser.

Windows note — ``stdin=DEVNULL`` is mandatory, not cosmetic: when this
server runs as an MCP stdio server, its own stdin is a pipe read
asynchronously (a background thread blocked on the next line, waiting
for the next client request). A child spawned WITHOUT an explicit
``stdin`` redirect inherits that same pipe handle by default on Windows.
The two ends then contend for it, and the child never completes —
observed as a hang with the child holding at ~0% CPU indefinitely, not a
slow child (verified: the same lint invocation completes in ~4s run
standalone, and the ThreadedProcess/native-asyncio spawn choice makes no
difference — only explicitly redirecting the child's stdin does). This
has nothing to do with ``renpy.exe`` specifically; it reproduces with a
plain ``python.exe`` child once its output is large enough that the pipe
read needs more than one pass. Every SDK shell-out — this module and
every direct spawn in ``tools/lifecycle.py`` — must set ``stdin`` so
neither the OS default nor an omitted keyword lets a child inherit our
own stdin pipe.

A second, independent issue: ``asyncio.create_subprocess_exec`` can raise
a bare ``NotImplementedError`` (empty message) on hosts where the running
event loop can't create async subprocesses at all. ``create_subprocess``
below tries the native async path first and falls back to a
thread-driven ``subprocess.Popen`` if that raises — mirroring the
workaround the `mcp` package itself ships for the same failure mode on
the client side (`mcp/os/win32/utilities.py::create_windows_process`).
``CREATE_NO_WINDOW`` is always passed on Windows so a console never
flashes regardless of which path is taken.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import sdk_launcher_name

_CREATIONFLAGS: int = (
    subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW")
    else 0
)


class ThreadedProcess:
    """Async-compatible wrapper around a blocking ``subprocess.Popen``.

    Exposes only the subset of ``asyncio.subprocess.Process``'s interface
    this codebase actually uses, so callers of `create_subprocess` don't
    need to care which path spawned the process.
    """

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self._popen = popen

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def returncode(self) -> int | None:
        return self._popen.poll()

    async def communicate(self) -> tuple[bytes, bytes]:
        stdout, stderr = await asyncio.to_thread(self._popen.communicate)
        return stdout or b"", stderr or b""

    async def wait(self) -> int:
        return await asyncio.to_thread(self._popen.wait)

    def terminate(self) -> None:
        self._popen.terminate()

    def kill(self) -> None:
        self._popen.kill()


async def create_subprocess(
    *args: str,
    stdout: int | None = None,
    stderr: int | None = None,
) -> asyncio.subprocess.Process | ThreadedProcess:
    """Spawn ``args`` the way ``asyncio.create_subprocess_exec`` would, but
    resilient to hosts where the native async-subprocess path raises
    ``NotImplementedError`` (see module docstring). Every shell-out to the
    Ren'Py SDK launcher — this module's `run` and every direct spawn in
    `tools/lifecycle.py` (`launch_preview`, `warp_to`) — goes through this
    so both fixes (stdin redirection, the NotImplementedError fallback)
    only have to be written once.

    ``stdin`` is always ``DEVNULL`` — not configurable — because omitting
    it is exactly the bug this function exists to prevent (see module
    docstring). None of the SDK subcommands this server invokes read
    stdin.
    """
    try:
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=_CREATIONFLAGS,
        )
    except NotImplementedError:
        popen = await asyncio.to_thread(
            subprocess.Popen,
            args,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=_CREATIONFLAGS,
        )
        return ThreadedProcess(popen)


@dataclass(frozen=True)
class SDKResult:
    returncode: int
    stdout: str
    stderr: str


async def run(sdk_root: Path, project_root: Path, *args: str, timeout: float = 120.0) -> SDKResult:
    """Spawn the Ren'Py SDK launcher with the given subcommand argv and capture its output."""
    cmd = [str(sdk_root / sdk_launcher_name()), str(project_root), *args]
    proc = await create_subprocess(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return SDKResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


async def run_lint(sdk_root: Path, project_root: Path) -> SDKResult:
    """Invoke Ren'Py's built-in lint over the project."""
    return await run(sdk_root, project_root, "lint")
