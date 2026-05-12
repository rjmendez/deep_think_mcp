from __future__ import annotations

import socket

from fastmcp import FastMCP

from deep_think_mcp.api import reasoning as reasoning_api
from deep_think_mcp import discover


def test_reasoning_tool_schema_exposes_provider_config_fields():
    mcp = FastMCP("test-provider-exposure")
    reasoning_api.register(mcp)

    tool = mcp._tool_manager._tools["deep_think_async"]
    provider_config_schema = tool.parameters["properties"]["provider_config"]["anyOf"][0]
    provider_fields = provider_config_schema["properties"]

    assert "provider" in provider_fields
    assert "medium_provider" in provider_fields
    assert "heavy_provider" in provider_fields
    assert "temperature" in provider_fields


def test_detect_cloud_providers_includes_abliteration_from_env(monkeypatch):
    monkeypatch.setenv("ABLITERATION_API_KEY", "test-abliteration-key")

    providers = discover._detect_cloud_providers()

    abliteration_models = [m for m in providers if m.provider == "abliteration"]
    assert abliteration_models
    assert abliteration_models[0].model_id == "abliterated-model"


def test_detect_cloud_providers_reads_abliteration_credentials_file(monkeypatch, tmp_path):
    monkeypatch.delenv("ABLITERATION_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(socket, "gethostname", lambda: "testhost")

    cred_dir = tmp_path / ".abliteration"
    cred_dir.mkdir()
    (cred_dir / "credentials").write_text("testhost=file-backed-key\n", encoding="utf-8")

    providers = discover._detect_cloud_providers()

    abliteration_models = [m for m in providers if m.provider == "abliteration"]
    assert abliteration_models
    assert abliteration_models[0].timeout_secs == discover.cloud_timeout("abliterated-model")
