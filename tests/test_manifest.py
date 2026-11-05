from pathlib import Path

import yaml

from change_windows.tools import SCHEMAS

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_declares_exactly_the_registered_tools():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert sorted(manifest["provides_tools"]) == sorted(s["name"] for s in SCHEMAS)
    assert manifest["provides_hooks"] == ["pre_tool_call"]
    assert manifest["requires_hermes"].startswith(">=")
