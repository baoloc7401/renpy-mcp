"""Tests for `sdk.create_subprocess` / `sdk.run` — the Windows-safe
subprocess shim every SDK shell-out (lint, translate, distribute,
launch_preview, warp_to) routes through.

Two independent bugs this guards against:

1. `asyncio.create_subprocess_exec` can raise a bare `NotImplementedError()`
   (empty message) on hosts where the running event loop can't create async
   subprocesses. Before this fix that exception propagated straight out of
   `sdk.run`, producing exactly the symptom reported against the real
   project: `{"error": "failed to invoke renpy.sh lint: "}` — an unusable,
   empty error message. `create_subprocess` catches that specific failure
   and falls back to a thread-driven `subprocess.Popen`, mirroring the
   workaround the `mcp` package itself ships for the same failure mode on
   the client side.

2. Omitting `stdin` lets a spawned child inherit this process's own stdin
   handle on Windows. When this server runs as an MCP stdio server, its
   own stdin is a pipe being read asynchronously (a background thread
   blocked waiting for the next request line) — a child that inherits a
   handle to that same pipe deadlocks against it. Confirmed via the real
   MCP stdio transport with a real Ren'Py SDK: the child spawns and does
   a trace amount of work, then sits at ~0% CPU forever; explicitly
   passing `stdin=DEVNULL` is what fixes it (not output size, not which
   subprocess API is used — every alternative was tried and hung
   identically). Nothing this server invokes reads stdin, so `stdin` is
   hardcoded to `DEVNULL` rather than left configurable.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from renpy_mcp import sdk


async def test_create_subprocess_native_path_captures_output():
    proc = await sdk.create_subprocess(
        sys.executable,
        "-c",
        "print('native-ok')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    assert b"native-ok" in stdout
    assert proc.returncode == 0
    assert isinstance(proc.pid, int)


async def test_create_subprocess_falls_back_when_native_path_unavailable(monkeypatch):
    """Force the exact failure mode reported on Windows: the native asyncio
    subprocess path raises a bare NotImplementedError. `create_subprocess`
    must catch it and still produce a working process via `subprocess.Popen`."""

    async def _boom(*args, **kwargs):
        raise NotImplementedError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

    proc = await sdk.create_subprocess(
        sys.executable,
        "-c",
        "print('fallback-ok')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert isinstance(proc, sdk.ThreadedProcess)
    stdout, _ = await proc.communicate()
    assert b"fallback-ok" in stdout
    assert proc.returncode == 0
    assert isinstance(proc.pid, int)


async def test_create_subprocess_redirects_stdin_on_native_path():
    """A child spawned via the native asyncio path must never inherit this
    process's own stdin — it must see immediate EOF (what DEVNULL gives),
    not block waiting for input. The nested-MCP-server deadlock this
    guards against can't be reproduced in a synchronous unit test (it
    only manifests once this process's own stdin is itself a pipe being
    read asynchronously elsewhere), so this asserts the causal
    precondition instead: stdin is never left unredirected."""
    proc = await sdk.create_subprocess(
        sys.executable,
        "-c",
        "import sys; data = sys.stdin.read(); print(f'got:{data!r}')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    assert b"got:''" in stdout


async def test_create_subprocess_redirects_stdin_on_fallback_path(monkeypatch):
    """Same guarantee on the ThreadedProcess (Popen) fallback path."""

    async def _boom(*args, **kwargs):
        raise NotImplementedError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

    proc = await sdk.create_subprocess(
        sys.executable,
        "-c",
        "import sys; data = sys.stdin.read(); print(f'got:{data!r}')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert isinstance(proc, sdk.ThreadedProcess)
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    assert b"got:''" in stdout


async def test_threaded_process_supports_terminate_and_wait():
    """A long-running process spawned via the fallback path must still be
    terminable and awaitable, the same interface `stop_preview` relies on."""
    popen = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
    )
    proc = sdk.ThreadedProcess(popen)
    assert proc.returncode is None
    proc.terminate()
    rc = await proc.wait()
    assert rc is not None
    assert proc.returncode is not None


def _use_python_as_fake_launcher(monkeypatch) -> Path:
    """Point `sdk_launcher_name()` at `sys.executable`'s own basename so
    `run()`'s `sdk_root / sdk_launcher_name()` resolves back to the real
    Python interpreter — a stand-in "SDK launcher" that's a genuine
    executable, not a real Ren'Py SDK. Returns `sdk_root` (== python's
    parent dir) for the caller to pass to `run()`.
    """
    exe = Path(sys.executable)
    monkeypatch.setattr(sdk, "sdk_launcher_name", lambda: exe.name)
    return exe.parent


async def test_run_propagates_stdout_through_native_path(tmp_path: Path, monkeypatch):
    """End-to-end through `sdk.run`: `run()` invokes `<launcher> <project> lint`,
    so pointing "project" at a real script lets python.exe run it directly —
    confirming stdout/returncode round-trip correctly through the native path."""
    sdk_root = _use_python_as_fake_launcher(monkeypatch)
    script = tmp_path / "fake_launcher.py"
    script.write_text("print('LINT OK')\n")

    result = await sdk.run(sdk_root, script, "lint")
    assert result.returncode == 0
    assert "LINT OK" in result.stdout


async def test_run_propagates_stdout_through_fallback_path(tmp_path: Path, monkeypatch):
    """Same as above, but with the native asyncio subprocess path forced to
    fail — this is the exact scenario that used to surface as an unusable
    empty error message from `get_lint_report`."""
    sdk_root = _use_python_as_fake_launcher(monkeypatch)
    script = tmp_path / "fake_launcher.py"
    script.write_text("print('LINT OK')\n")

    async def _boom(*args, **kwargs):
        raise NotImplementedError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

    result = await sdk.run(sdk_root, script, "lint")
    assert result.returncode == 0
    assert "LINT OK" in result.stdout


async def test_run_captures_nonzero_exit_and_stderr(tmp_path: Path, monkeypatch):
    """A failing lint invocation must come back as a normal SDKResult
    (returncode != 0, stderr populated) rather than raising — `run()`
    only raises on timeout."""
    sdk_root = _use_python_as_fake_launcher(monkeypatch)
    script = tmp_path / "fake_launcher.py"
    script.write_text("import sys; sys.stderr.write('boom'); sys.exit(3)\n")

    result = await sdk.run(sdk_root, script, "lint")
    assert result.returncode == 3
    assert "boom" in result.stderr
