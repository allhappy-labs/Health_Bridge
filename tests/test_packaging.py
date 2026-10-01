"""Release payload contracts: HACS must ship runnable source and both cards."""

import json
from pathlib import Path
import tomllib

from packaging.specifiers import SpecifierSet


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/health_bridge"


def test_hacs_and_python_minimum_match_qualified_runtime():
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert hacs["content_in_root"] is False
    assert hacs["homeassistant"] == "2026.9.3"
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    versions = SpecifierSet(project["requires-python"])
    assert "3.14.2" in versions
    assert "3.14.1" not in versions


def test_manifest_and_hacs_payload_are_complete():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert manifest["domain"] == "health_bridge"
    assert manifest["config_flow"] is True
    assert manifest["version"] == project["version"] == "2.1.1a3"
    assert {"backup", "frontend", "http", "lovelace", "webhook"} <= set(manifest["dependencies"])
    assert manifest["requirements"] == []
    for name in (
        "__init__.py", "config_flow.py", "archive_api.py", "archive_catalog_v2.json",
        "archive_protocol.py", "archive_store.py", "archive_webhook.py",
        "archive_projection.py", "statistic_rules.py", "backup.py",
        "cards/health-bridge-cards.js", "cards/health-bridge-archive.js",
    ):
        assert (COMPONENT / name).is_file(), name


def test_install_payload_has_no_bytecode_artifacts():
    artifacts = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "custom_components").rglob("*")
        if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
    ]
    assert artifacts == []


async def test_packaged_cards_are_registered_and_served(bridge_client):
    for name in ("health-bridge-cards", "health-bridge-archive"):
        response = await bridge_client.get(f"/health_bridge/{name}.js")
        assert response.status == 200
        assert "customElements.define" in await response.text()
