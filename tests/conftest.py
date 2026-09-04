from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from renpy_mcp.config import ServerConfig
from renpy_mcp.project.scanner import ProjectIndex
from renpy_mcp.tools import tier1_read
from renpy_mcp.tools.registry import ToolRegistry

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "tiny_project"
# Tests that invoke the real Ren'Py SDK read its location from RENPY_SDK.
# Tests that don't need the SDK (most of them) get a placeholder path that
# only needs to exist as a directory to satisfy ServerConfig.validate.
SDK_ROOT = Path(os.environ.get("RENPY_SDK", str(Path.home() / "renpy-sdk")))


@pytest.fixture
def config() -> ServerConfig:
    return ServerConfig(project_root=FIXTURE_ROOT.resolve(), sdk_root=SDK_ROOT)


@pytest.fixture
def registry(config: ServerConfig) -> ToolRegistry:
    reg = ToolRegistry(config)
    tier1_read.register(reg, config, ProjectIndex(config))
    return reg


def parse(content_list) -> dict:
    """Decode a tool's TextContent list into the JSON payload."""
    assert len(content_list) == 1
    return json.loads(content_list[0].text)
