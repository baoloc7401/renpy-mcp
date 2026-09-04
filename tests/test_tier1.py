from __future__ import annotations

import pytest

from .conftest import parse


async def test_overview(registry):
    out = parse(await registry.call("get_project_overview", {}))
    assert out["counts"]["labels"] == 4
    assert out["counts"]["characters"] == 2
    assert out["counts"]["screens"] == 1
    assert "start" in out["labels"]
    assert out["warnings"] == []


async def test_overview_flags_missing_start(tmp_path, monkeypatch):
    # Build a one-file project with no `start` label and confirm the overview warns.
    from renpy_mcp.config import ServerConfig
    from renpy_mcp.project.scanner import ProjectIndex
    from renpy_mcp.tools import tier1_read
    from renpy_mcp.tools.registry import ToolRegistry

    (tmp_path / "game").mkdir()
    (tmp_path / "game" / "script.rpy").write_text("label nope:\n    return\n")
    cfg = ServerConfig(project_root=tmp_path, sdk_root=tmp_path)  # sdk unused here
    reg = ToolRegistry()
    tier1_read.register(reg, cfg, ProjectIndex(cfg))
    out = parse(await reg.call("get_project_overview", {}))
    assert any("no `label start:`" in w for w in out["warnings"])


async def test_list_labels_all_and_filtered(registry):
    out = parse(await registry.call("list_labels", {}))
    assert out["count"] == 4
    out = parse(await registry.call("list_labels", {"file": "game/script.rpy"}))
    assert out["count"] == 4
    out = parse(await registry.call("list_labels", {"file": "game/options.rpy"}))
    assert out["count"] == 0


async def test_read_label_happy(registry):
    out = parse(await registry.call("read_label", {"name": "cafe_scene"}))
    assert out["label"]["name"] == "cafe_scene"
    assert "label cafe_scene:" in out["source"]
    assert "Mei" in out["source"]


async def test_read_label_missing(registry):
    out = parse(await registry.call("read_label", {"name": "no_such_label"}))
    assert "error" in out


async def test_list_and_read_character(registry):
    out = parse(await registry.call("list_characters", {}))
    assert {c["var"] for c in out["characters"]} == {"e", "m"}

    out = parse(await registry.call("read_character", {"var": "m"}))
    assert out["character"]["display_name"] == "Mei"

    out = parse(await registry.call("read_character", {"var": "ghost"}))
    assert "error" in out


async def test_list_variables_filters(registry):
    all_vars = parse(await registry.call("list_variables", {}))
    defaults = parse(await registry.call("list_variables", {"kind": "default"}))
    defines = parse(await registry.call("list_variables", {"kind": "define"}))
    assert all_vars["count"] == defaults["count"] + defines["count"]
    assert {v["name"] for v in defaults["variables"]} == {"met_mei", "affection_mei"}


async def test_list_and_read_screen(registry):
    out = parse(await registry.call("list_screens", {}))
    assert out["count"] == 1
    assert out["screens"][0]["name"] == "affection_meter"

    out = parse(await registry.call("read_screen", {"name": "affection_meter"}))
    assert "screen affection_meter():" in out["source"]


async def test_list_images_combines_aliases_and_auto(registry):
    out = parse(await registry.call("list_images", {}))
    kinds = {img["kind"] for img in out["images"]}
    # Fixture has both `image bg park = ...` aliases and PNGs in game/images/.
    assert {"alias", "auto"} <= kinds
    names = {img["name"] for img in out["images"]}
    assert "bg park" in names  # alias
    assert "eileen happy" in names  # auto


async def test_list_audio_lists_files_and_plays(registry):
    out = parse(await registry.call("list_audio", {}))
    paths = {f["asset_path"] for f in out["files"]}
    assert "game/audio/spring_theme.ogg" in paths
    assert any(p["asset"] == "audio/spring_theme.ogg" for p in out["plays"])


async def test_find_references_finds_label_jumps(registry):
    out = parse(await registry.call("find_references", {"needle": "ending"}))
    # `label ending:` plus two `jump ending` calls = 3.
    assert out["count"] >= 3
    files = {m["file"] for m in out["matches"]}
    assert "game/script.rpy" in files


async def test_find_references_word_boundary(registry):
    out = parse(await registry.call("find_references", {"needle": "e", "word_boundary": True}))
    # `e` is the character var; bounded matches should hit only its uses, not
    # every occurrence of the letter.
    bare_e_count = sum(1 for m in out["matches"] if " e " in f" {m['context']} ")
    assert bare_e_count >= 1


async def test_read_raw_file_happy_and_traversal(registry):
    out = parse(await registry.call("read_raw_file", {"path": "game/options.rpy"}))
    assert "config.name" in out["content"]

    out = parse(await registry.call("read_raw_file", {"path": "../../../etc/passwd"}))
    assert "error" in out


@pytest.mark.skipif(
    not (__import__("pathlib").Path(__import__("os").environ.get("RENPY_SDK", str(__import__("pathlib").Path.home() / "renpy-sdk"))) / "renpy.sh").is_file(),
    reason="Ren'Py SDK not present (set RENPY_SDK to enable)",
)
async def test_get_lint_report_runs(registry):
    out = parse(await registry.call("get_lint_report", {}))
    assert "returncode" in out
    # Structured shape: every lint run, even a clean one, returns the same
    # envelope. Agents can dispatch on `clean` without parsing stdout text.
    assert "patterns" in out and isinstance(out["patterns"], list)
    assert "findings_count" in out
    assert "summary" in out and "errors" in out["summary"]
    assert "clean" in out
    # Raw stdout/findings are the include_raw escape hatch, not the default.
    assert "stdout" not in out
    assert "findings" not in out

    raw = parse(await registry.call("get_lint_report", {"include_raw": True}))
    assert "stdout" in raw
    assert "findings" in raw and isinstance(raw["findings"], list)


async def test_get_lint_report_parses_findings_from_stubbed_sdk(registry, monkeypatch):
    """Stub the SDK invocation so this test runs without RENPY_SDK and
    pins the shape of the aggregated patterns end-to-end."""
    from renpy_mcp import sdk as renpy_sdk

    sample = (
        "Ren'Py 8.6.0 lint report.\n"
        "\n"
        "game/script.rpy:5 The jump is to nonexistent label 'nowhere'.\n"
        "It is advised to set config.check_conflicting_properties to True.\n"
        "\n"
        "Statistics:\n"
        "\n"
        "1 errors, 0 warnings, 0 informational messages.\n"
        "\n"
        "Lint is not a substitute for thorough testing.\n"
    )

    async def fake_run_lint(_sdk, _proj):
        return renpy_sdk.SDKResult(returncode=0, stdout=sample, stderr="")

    monkeypatch.setattr(renpy_sdk, "run_lint", fake_run_lint)

    out = parse(await registry.call("get_lint_report", {}))
    assert out["clean"] is False
    assert out["summary"] == {"errors": 1, "warnings": 0, "info": 0, "obsolete": 0}
    assert out["findings_count"] == 1
    assert out["pattern_count"] == 1
    assert out["patterns_truncated"] is False
    assert len(out["patterns"]) == 1
    group = out["patterns"][0]
    assert group["count"] == 1
    assert group["severity"] == "error"
    assert "nonexistent" in group["pattern"]
    assert "'{value}'" in group["pattern"]  # 'nowhere' collapsed to a placeholder
    example = group["examples"][0]
    assert example["file"] == "game/script.rpy"
    assert example["line"] == 5
    assert "nonexistent" in example["message"]
    assert any("config.check_conflicting" in a for a in out["advisories"])


async def test_get_lint_report_groups_repeated_pattern_and_caps_examples(registry, monkeypatch):
    """The exact reported scenario: one message pattern repeated on nearly
    every line, differing only in the quoted name. Must collapse to a
    single `patterns` entry with an accurate count and a capped example
    list, not one `findings` entry per occurrence."""
    from renpy_mcp import sdk as renpy_sdk

    names = ["jennifer", "erica", "stephanie", "mc.name", "the_person"] * 20  # 100 lines
    lines = [
        f"game/script.rpy:{i + 1} Could not evaluate '{name}' in the who part of a say statement."
        for i, name in enumerate(names)
    ]
    sample = "\n".join(lines) + "\n\nStatistics:\n\n0 errors, 100 warnings.\n"

    async def fake_run_lint(_sdk, _proj):
        return renpy_sdk.SDKResult(returncode=0, stdout=sample, stderr="")

    monkeypatch.setattr(renpy_sdk, "run_lint", fake_run_lint)

    out = parse(await registry.call("get_lint_report", {}))
    assert out["findings_count"] == 100
    assert out["pattern_count"] == 1
    assert len(out["patterns"]) == 1
    group = out["patterns"][0]
    assert group["count"] == 100
    assert group["severity"] == "warning"
    # Capped at 5 examples even though 100 lines matched.
    assert len(group["examples"]) == 5
    # The response as a whole must be small — this is the actual bug: a
    # naive dump of 100 near-identical findings vs. one grouped summary.
    import json as _json
    assert len(_json.dumps(out)) < 5000


async def test_get_lint_report_surfaces_bare_exception_with_useful_message(registry, monkeypatch):
    """A bare `NotImplementedError()` (empty `str(exc)`) — the exact
    failure asyncio subprocess creation raises on some Windows hosts — must
    not produce an unusable blank error message. It must also name the
    launcher the *current platform* actually resolves to, not a hardcoded
    `renpy.sh`."""
    from renpy_mcp import sdk as renpy_sdk
    from renpy_mcp.config import sdk_launcher_name

    async def fake_run_lint(_sdk, _proj):
        raise NotImplementedError()

    monkeypatch.setattr(renpy_sdk, "run_lint", fake_run_lint)

    out = parse(await registry.call("get_lint_report", {}))
    assert "error" in out
    assert out["error"] != f"failed to invoke {sdk_launcher_name()} lint: "
    assert "NotImplementedError" in out["error"]
    assert sdk_launcher_name() in out["error"]


async def test_get_lint_report_default_omits_raw_and_flat_findings(registry, monkeypatch):
    """Default (no args) must NOT include the flat findings list or raw
    stdout/stderr/statistics — those are the include_raw escape hatch."""
    from renpy_mcp import sdk as renpy_sdk

    async def fake_run_lint(_sdk, _proj):
        return renpy_sdk.SDKResult(returncode=0, stdout="Statistics:\n\n", stderr="")

    monkeypatch.setattr(renpy_sdk, "run_lint", fake_run_lint)

    out = parse(await registry.call("get_lint_report", {}))
    assert "stdout" not in out
    assert "stderr" not in out
    assert "statistics" not in out
    assert "findings" not in out
    assert "patterns" in out


async def test_get_lint_report_include_raw_true_restores_flat_findings(registry, monkeypatch):
    """`include_raw: true` is the explicit escape hatch back to the full,
    ungrouped view — findings list plus raw stdout/stderr/statistics."""
    from renpy_mcp import sdk as renpy_sdk

    sample = "game/script.rpy:5 The jump is to nonexistent label 'nowhere'.\n"

    async def fake_run_lint(_sdk, _proj):
        return renpy_sdk.SDKResult(returncode=0, stdout=sample, stderr="")

    monkeypatch.setattr(renpy_sdk, "run_lint", fake_run_lint)

    out = parse(await registry.call("get_lint_report", {"include_raw": True}))
    assert "stdout" in out
    assert "stderr" in out
    assert "findings" in out and len(out["findings"]) == 1
    assert "patterns" in out  # grouped view still present alongside the raw one
    assert "findings" in out


# ---------- get_recent_edits ----------------------------------------------------


@pytest.fixture
def write_workspace(tmp_path):
    """Tiered registry over a fresh fixture copy + a cleared recent-edits buffer."""
    import shutil
    from renpy_mcp.config import ServerConfig
    from renpy_mcp.project import recent as recent_buffer
    from renpy_mcp.project.scanner import ProjectIndex
    from renpy_mcp.tools import tier1_read, tier2_write
    from renpy_mcp.tools.registry import ToolRegistry

    from .conftest import FIXTURE_ROOT, SDK_ROOT

    proj = tmp_path / "tiny_project"
    shutil.copytree(FIXTURE_ROOT, proj)
    cfg = ServerConfig(project_root=proj.resolve(), sdk_root=SDK_ROOT)
    idx = ProjectIndex(cfg)
    reg = ToolRegistry()
    tier1_read.register(reg, cfg, idx)
    tier2_write.register(reg, cfg, idx)
    recent_buffer.clear()
    yield cfg, reg, idx
    recent_buffer.clear()


async def test_get_recent_edits_records_writes(write_workspace):
    _, reg, _ = write_workspace
    # Empty until a write happens.
    out = parse(await reg.call("get_recent_edits", {}))
    assert out["count"] == 0
    assert out["entries"] == []

    # A successful Tier 2 write should land in the buffer.
    await reg.call(
        "add_say",
        {"label": "park_scene", "character": "e", "text": "A new line."},
    )
    out = parse(await reg.call("get_recent_edits", {}))
    assert out["count"] == 1
    entry = out["entries"][0]
    assert entry["file"] == "game/script.rpy"
    assert "A new line." in entry["diff"]
    assert "@@" in entry["diff"]  # unified-diff hunk marker
    assert "added say" in entry["summary"].lower() or "park_scene" in entry["summary"]
    assert isinstance(entry["timestamp"], (int, float))


async def test_get_recent_edits_newest_first_and_limit(write_workspace):
    _, reg, _ = write_workspace
    await reg.call(
        "add_say",
        {"label": "park_scene", "character": "e", "text": "first"},
    )
    await reg.call(
        "add_say",
        {"label": "park_scene", "character": "e", "text": "second"},
    )
    full = parse(await reg.call("get_recent_edits", {}))
    assert full["count"] == 2
    assert "second" in full["entries"][0]["diff"]
    assert "first" in full["entries"][1]["diff"]

    capped = parse(await reg.call("get_recent_edits", {"limit": 1}))
    assert capped["count"] == 1
    assert "second" in capped["entries"][0]["diff"]


async def test_get_recent_edits_skips_no_op(write_workspace):
    cfg, reg, _ = write_workspace
    target = cfg.project_root / "game/script.rpy"
    same = target.read_text()
    # Re-write the same content via apply_write directly — this is a no-op
    # path; the buffer should remain empty.
    from renpy_mcp.project.scanner import ProjectIndex
    from renpy_mcp.project.writer import apply_write

    idx = ProjectIndex(cfg)
    apply_write(cfg, idx, "game/script.rpy", same)
    out = parse(await reg.call("get_recent_edits", {}))
    assert out["count"] == 0


# ---------- get_media_invariants -----------------------------------------------


async def test_get_media_invariants_returns_structured_dict(registry):
    out = parse(await registry.call("get_media_invariants", {}))
    assert "image" in out and "audio" in out
    assert out["image"]["background"]["exact_size"] == [1920, 1080]
    assert out["image"]["sprite"]["alpha_required"] is True
    assert "ogg" in out["audio"]["music"]["format"]
    assert out["doc"].startswith("MEDIA.md")
