"""FastAPI app wiring the MCP client + REST + WebSocket into one process.

REST routes are thin: validate input, call into the MCP client, return
the JSON the MCP tool produced (with light reshaping for the panels
that need it). WebSocket pushes filesystem-change events so the frontend
can refetch live.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .graph import compute_branch_graph
from .mcp_client import RenpyMcpClient
from .watcher import FileEvent, ProjectWatcher
from . import recent as recent_buffer

log = logging.getLogger("renpy_mcp_gui.app")


class AppState:
    client: RenpyMcpClient
    watcher: ProjectWatcher
    project_root: Path
    sdk_root: Path
    ws_clients: set[WebSocket]
    fanout_task: asyncio.Task | None = None


state = AppState()


class AddCharacter(BaseModel):
    var: str
    display_name: str
    color: str | None = None
    image_tag: str | None = None
    extra_kwargs: dict[str, str] | None = None
    file: str | None = None


class UpdateCharacter(BaseModel):
    display_name: str | None = None
    color: str | None = None


class AddDialogueLine(BaseModel):
    character: str | None = None  # omit for narration
    text: str


class SwapBackground(BaseModel):
    new_background: str


class SetSceneMusic(BaseModel):
    asset: str  # empty string emits `stop music`
    fadein: float | None = None
    loop: bool = False
    validate_asset: bool = True


class SetVariableDefault(BaseModel):
    value: str
    file: str | None = None


class AddMinigameScaffold(BaseModel):
    name: str
    on_complete_label: str
    screens_file: str | None = None
    label_file: str | None = None


class CanvasPositionsBody(BaseModel):
    positions: dict[str, dict[str, float]]
    replace: bool = False


class AddLabelBody(BaseModel):
    body: list[str] | None = None
    file: str | None = None


class AddMenuBranchBody(BaseModel):
    text: str
    body: list[str] | None = None
    condition: str | None = None
    raw: bool = False


class AddJumpBody(BaseModel):
    target: str


# ---------- Phase 4 event-stream bodies ----------------------------------------


class AddPauseBody(BaseModel):
    duration: float


class AddSetvarBody(BaseModel):
    name: str
    # JSON value of any supported scalar (string/bool/int/float/null). The
    # backend `add_setvar` tool validates the type itself, so we accept
    # anything here and forward it along.
    value: Any = None


class AddShowBody(BaseModel):
    tag: str
    expression: str | None = None
    position: str | None = None
    transition: str | None = None


class AddWithEffectBody(BaseModel):
    expression: str


class AddFlashBody(BaseModel):
    color: str
    duration: float | None = None


class BuildDistributeBody(BaseModel):
    targets: list[str]
    destination: str | None = None


class ScreenLayoutBody(BaseModel):
    name: str
    root: dict[str, Any]
    file: str | None = None


class StageBody(BaseModel):
    label: str
    background: str | None = None
    sprites: list[dict[str, Any]] | None = None
    transition: str | None = None


class ImageMapBody(BaseModel):
    name: str
    ground: str
    hover: str
    hotspots: list[dict[str, Any]]
    file: str | None = None


class RedirectJumpBody(BaseModel):
    file: str
    line: int
    new_target: str


class UpdateMenuChoiceBody(BaseModel):
    file: str
    line: int
    text: str
    raw: bool = False


class AddMenuBody(BaseModel):
    choices: list[dict]


def _make_lifespan(project_root: Path, sdk_root: Path):
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state.project_root = project_root
        state.sdk_root = sdk_root
        state.ws_clients = set()
        # Construct the watcher first so we have a `mark_self_write` target
        # for the MCP client's response observer.
        state.watcher = ProjectWatcher(project_root)
        state.client = RenpyMcpClient(
            project_root, sdk_root, response_observer=_self_write_observer
        )
        await state.client.start()
        state.watcher.start(asyncio.get_running_loop())
        state.fanout_task = asyncio.create_task(_fanout_file_events())
        try:
            yield
        finally:
            if state.fanout_task is not None:
                state.fanout_task.cancel()
            state.watcher.stop()
            await state.client.stop()

    return lifespan


def _self_write_observer(_name: str, payload: dict[str, Any]) -> None:
    """Mark every file an internal write touched so the watcher skips its echo,
    and record the write in the GUI's recent-edits buffer with origin="gui".

    Single-file writes (Tier 2 primitives, most Tier 3 intents) return a
    top-level `file`. Multi-file writes (e.g. `add_minigame_screen_scaffold`)
    return a `diffs` array of `{file, ...}` entries. Sidecar tools
    (`set_canvas_positions`, `set_ignored_diagnostics`) write outside
    `game/` and do not return `file` — naturally skipped here.
    """
    if not isinstance(payload, dict) or "error" in payload:
        return
    summary = payload.get("summary") if isinstance(payload.get("summary"), str) else ""
    rel = payload.get("file")
    if isinstance(rel, str) and rel:
        state.watcher.mark_self_write(rel)
        # No-op writes return file but no diff — keep them out of the buffer.
        if not payload.get("no_op"):
            diff = payload.get("diff") if isinstance(payload.get("diff"), str) else ""
            recent_buffer.record(file=rel, origin="gui", summary=summary, diff=diff)
    diffs = payload.get("diffs")
    if isinstance(diffs, list):
        for entry in diffs:
            if isinstance(entry, dict):
                f = entry.get("file")
                if isinstance(f, str) and f:
                    state.watcher.mark_self_write(f)
                    if not entry.get("no_op"):
                        recent_buffer.record(
                            file=f,
                            origin="gui",
                            summary=summary,
                            diff=entry.get("diff") if isinstance(entry.get("diff"), str) else "",
                        )


async def _fanout_file_events() -> None:
    """Forward watcher events to every connected WebSocket client and
    record them in the GUI's recent-edits buffer with origin="agent"."""
    try:
        while True:
            evt: FileEvent = await state.watcher.queue.get()
            payload = json.dumps({"type": "file_change", "kind": evt.kind, "action": evt.action, "path": evt.path})
            recent_buffer.record(
                file=evt.path,
                origin="agent",
                summary=f"{evt.action} {evt.path}",
            )
            for ws in list(state.ws_clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    state.ws_clients.discard(ws)
    except asyncio.CancelledError:
        pass


def build_app(project_root: Path, sdk_root: Path, static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="renpy-mcp-gui",
        version="0.1.0",
        lifespan=_make_lifespan(project_root, sdk_root),
    )
    # This API can write project files and spawn the Ren'Py preview process,
    # so it must not accept requests from arbitrary web origins. The Vite
    # dev server (5173) proxies `/api`/`/ws` to this backend same-origin; in
    # production the built frontend is served by this same app (see the
    # static-mount below), so no cross-origin access is actually needed —
    # this allowlist only covers the dev-server-open-in-a-separate-tab case.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )

    # ---------- read endpoints ----------

    @app.get("/api/overview")
    async def overview() -> Any:
        return await state.client.call("get_project_overview")

    @app.get("/api/labels")
    async def list_labels() -> Any:
        return await state.client.call("list_labels")

    @app.get("/api/labels/{name}")
    async def read_label(name: str) -> Any:
        payload = await state.client.call("read_label", {"name": name})
        if "error" in payload:
            raise HTTPException(404, payload["error"])
        return payload

    @app.get("/api/graph")
    async def branch_graph() -> Any:
        return await compute_branch_graph(state.client)

    @app.get("/api/characters")
    async def list_characters() -> Any:
        return await state.client.call("list_characters")

    @app.get("/api/images")
    async def list_images() -> Any:
        return await state.client.call("list_images")

    @app.get("/api/audio")
    async def list_audio() -> Any:
        return await state.client.call("list_audio")

    @app.get("/api/variables")
    async def list_variables() -> Any:
        return await state.client.call("list_variables")

    @app.get("/api/screens")
    async def list_screens() -> Any:
        return await state.client.call("list_screens")

    @app.get("/api/lint")
    async def lint() -> Any:
        # The frontend's own `lib/lint.ts` parses `stdout` client-side, so
        # it needs the include_raw escape hatch — the default response
        # (patterns grouped by message, no raw stdout) is for MCP callers,
        # not this endpoint.
        return await state.client.call("get_lint_report", {"include_raw": True})

    # ---------- write endpoints ----------

    @app.post("/api/characters")
    async def add_character(body: AddCharacter = Body(...)) -> Any:
        return await state.client.call("add_character", body.model_dump(exclude_none=True))

    @app.patch("/api/characters/{var}")
    async def update_character(var: str, body: UpdateCharacter = Body(...)) -> Any:
        args = {"var": var, **body.model_dump(exclude_none=True)}
        return await state.client.call("update_character", args)

    @app.post("/api/labels/{name}/dialogue")
    async def append_dialogue(name: str, body: AddDialogueLine = Body(...)) -> Any:
        args: dict[str, Any] = {"label": name, "text": body.text}
        if body.character is not None:
            args["character"] = body.character
        return await state.client.call("add_say", args)

    @app.post("/api/labels/{name}/background")
    async def swap_label_background(name: str, body: SwapBackground = Body(...)) -> Any:
        return await state.client.call(
            "swap_background", {"label": name, "new_background": body.new_background}
        )

    @app.post("/api/labels/{name}/music")
    async def set_label_music(name: str, body: SetSceneMusic = Body(...)) -> Any:
        args: dict[str, Any] = {
            "label": name,
            "asset": body.asset,
            "loop": body.loop,
            "validate_asset": body.validate_asset,
        }
        if body.fadein is not None:
            args["fadein"] = body.fadein
        return await state.client.call("set_scene_music", args)

    @app.put("/api/variables/{name}")
    async def upsert_variable_default(name: str, body: SetVariableDefault = Body(...)) -> Any:
        args: dict[str, Any] = {"name": name, "value": body.value}
        if body.file is not None:
            args["file"] = body.file
        return await state.client.call("set_variable_default", args)

    @app.post("/api/minigames")
    async def add_minigame(body: AddMinigameScaffold = Body(...)) -> Any:
        return await state.client.call(
            "add_minigame_screen_scaffold", body.model_dump(exclude_none=True)
        )

    # ---------- canvas positions (Story Map) ----------

    @app.get("/api/canvas-positions")
    async def read_positions() -> Any:
        return await state.client.call("read_canvas_positions")

    @app.post("/api/canvas-positions")
    async def write_positions(body: CanvasPositionsBody = Body(...)) -> Any:
        return await state.client.call(
            "set_canvas_positions",
            body.model_dump(exclude_none=True),
        )

    # ---------- Story Map editing (Phase 3b) ----------

    @app.post("/api/labels/{name}")
    async def create_label(name: str, body: AddLabelBody = Body(...)) -> Any:
        args: dict[str, Any] = {"name": name}
        args.update(body.model_dump(exclude_none=True))
        return await state.client.call("add_label", args)

    @app.delete("/api/labels/{name}")
    async def remove_label(name: str) -> Any:
        return await state.client.call("delete_label", {"label": name})

    @app.post("/api/labels/{name}/menu")
    async def attach_menu(name: str, body: AddMenuBody = Body(...)) -> Any:
        return await state.client.call(
            "add_menu", {"label": name, "choices": body.choices}
        )

    @app.post("/api/labels/{name}/menu-branches")
    async def append_menu_branch(name: str, body: AddMenuBranchBody = Body(...)) -> Any:
        args: dict[str, Any] = {"label": name}
        args.update(body.model_dump(exclude_none=True))
        return await state.client.call("add_menu_branch", args)

    @app.post("/api/labels/{name}/jumps")
    async def append_jump(name: str, body: AddJumpBody = Body(...)) -> Any:
        return await state.client.call(
            "add_jump", {"label": name, "target": body.target}
        )

    # ---------- label tree (Phase 4 Inspector) ----------

    @app.get("/api/labels/{name}/tree")
    async def label_tree(name: str) -> Any:
        payload = await state.client.call("read_label_tree", {"name": name})
        if "error" in payload:
            raise HTTPException(404, payload["error"])
        return payload

    @app.get("/api/choice-graph")
    async def choice_graph() -> Any:
        return await state.client.call("get_choice_graph")

    # ---------- translations + distribute (Phase 8) ----------

    @app.get("/api/translations/coverage")
    async def translation_coverage() -> Any:
        return await state.client.call("get_translation_coverage")

    @app.get("/api/translations/stale")
    async def stale_translations(language: str | None = None) -> Any:
        args: dict[str, Any] = {}
        if language is not None:
            args["language"] = language
        return await state.client.call("find_stale_translations", args)

    @app.post("/api/translations/scaffolding/{language}")
    async def scaffold_translations(language: str) -> Any:
        return await state.client.call(
            "generate_translation_scaffolding", {"language": language}
        )

    @app.post("/api/build/distribute")
    async def build_distribute(body: BuildDistributeBody = Body(...)) -> Any:
        args: dict[str, Any] = {"targets": body.targets}
        if body.destination is not None:
            args["destination"] = body.destination
        return await state.client.call("build_distribution", args)

    # ---------- composers (Phase 7) ----------

    @app.post("/api/composers/screen-layout")
    async def composer_screen_layout(body: ScreenLayoutBody = Body(...)) -> Any:
        return await state.client.call(
            "add_screen_layout", body.model_dump(exclude_none=True)
        )

    @app.post("/api/scaffold/repair")
    async def repair_scaffold() -> Any:
        return await state.client.call("repair_scaffold")

    @app.post("/api/composers/stage")
    async def composer_stage(body: StageBody = Body(...)) -> Any:
        return await state.client.call("add_stage", body.model_dump(exclude_none=True))

    @app.post("/api/composers/imagemap")
    async def composer_imagemap(body: ImageMapBody = Body(...)) -> Any:
        return await state.client.call(
            "add_imagemap", body.model_dump(exclude_none=True)
        )

    # ---------- Phase 4 event-stream tools ----------

    @app.post("/api/labels/{name}/events/pause")
    async def append_pause(name: str, body: AddPauseBody = Body(...)) -> Any:
        return await state.client.call("add_pause", {"label": name, "duration": body.duration})

    @app.post("/api/labels/{name}/events/setvar")
    async def append_setvar(name: str, body: AddSetvarBody = Body(...)) -> Any:
        return await state.client.call(
            "add_setvar",
            {"label": name, "name": body.name, "value": body.value},
        )

    @app.post("/api/labels/{name}/events/show")
    async def append_show(name: str, body: AddShowBody = Body(...)) -> Any:
        args: dict[str, Any] = {"label": name, "tag": body.tag}
        if body.expression is not None:
            args["expression"] = body.expression
        if body.position is not None:
            args["position"] = body.position
        if body.transition is not None:
            args["transition"] = body.transition
        return await state.client.call("add_show", args)

    @app.post("/api/labels/{name}/events/with")
    async def append_with(name: str, body: AddWithEffectBody = Body(...)) -> Any:
        return await state.client.call(
            "add_with_effect", {"label": name, "expression": body.expression}
        )

    @app.post("/api/labels/{name}/events/flash")
    async def append_flash(name: str, body: AddFlashBody = Body(...)) -> Any:
        args: dict[str, Any] = {"label": name, "color": body.color}
        if body.duration is not None:
            args["duration"] = body.duration
        return await state.client.call("add_flash", args)

    @app.post("/api/jumps/redirect")
    async def redirect_jump(body: RedirectJumpBody = Body(...)) -> Any:
        return await state.client.call("redirect_jump", body.model_dump(exclude_none=True))

    @app.post("/api/menu-choices/edit")
    async def update_menu_choice(body: UpdateMenuChoiceBody = Body(...)) -> Any:
        return await state.client.call("update_menu_choice", body.model_dump(exclude_none=True))

    @app.get("/api/recent-edits")
    async def recent_edits(limit: int | None = None) -> Any:
        entries = recent_buffer.snapshot(limit=limit)
        return {"count": len(entries), "entries": [e.to_dict() for e in entries]}

    # ---------- preview lifecycle ----------

    @app.get("/api/preview")
    async def preview_status() -> Any:
        return await state.client.call("get_preview_status")

    @app.post("/api/preview")
    async def preview_launch() -> Any:
        return await state.client.call("launch_preview")

    @app.delete("/api/preview")
    async def preview_stop() -> Any:
        return await state.client.call("stop_preview")

    # Asset upload — copy a user-supplied file into the project's assets dir.
    # Kept tiny on purpose; the MCP server doesn't have an asset-upload tool.
    from fastapi import UploadFile, File, Form

    @app.post("/api/assets/upload")
    async def upload_asset(
        kind: str = Form(...),  # "image" | "audio"
        upload: UploadFile = File(...),
    ) -> Any:
        if kind not in ("image", "audio"):
            raise HTTPException(400, "kind must be `image` or `audio`")
        subdir = "images" if kind == "image" else "audio"
        target_dir = project_root / "game" / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        if not upload.filename:
            raise HTTPException(400, "upload missing a filename")
        target = target_dir / Path(upload.filename).name
        target.write_bytes(await upload.read())
        return {"asset_path": f"game/{subdir}/{target.name}", "size_bytes": target.stat().st_size}

    # ---------- websocket ----------

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        state.ws_clients.add(websocket)
        try:
            await websocket.send_json({"type": "ready", "project_root": str(project_root)})
            while True:
                # Drain anything the client sends; we don't act on it but we need to
                # keep the receive side alive so we notice disconnects promptly.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            state.ws_clients.discard(websocket)

    # ---------- meta ----------

    @app.get("/api/meta")
    async def meta() -> Any:
        return {
            "project_root": str(project_root),
            "sdk_root": str(sdk_root),
        }

    # ---------- static frontend ----------

    if static_dir is not None and static_dir.is_dir():
        # Serve the production build (`vite build`'s `dist/`) under /.
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        @app.get("/{path:path}", include_in_schema=False)
        async def spa_fallback(path: str = ""):
            # Anything that doesn't look like an /api/* call falls through to index.html
            # so client-side routing can take over.
            if path.startswith("api/") or path.startswith("ws"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            target = static_dir / path
            if path and target.is_file():
                return FileResponse(target)
            return FileResponse(static_dir / "index.html")

    return app
