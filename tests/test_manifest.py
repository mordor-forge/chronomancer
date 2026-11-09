from pathlib import Path

import yaml

from change_windows.tools import SCHEMAS

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_declares_exactly_the_registered_tools():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert sorted(manifest["provides_tools"]) == sorted(s["name"] for s in SCHEMAS)
    assert manifest["provides_hooks"] == ["pre_tool_call"]
    assert manifest["requires_hermes"].startswith(">=")


def test_shipped_default_mode_is_enforce():
    # Owner decision M1a: the shipped default blocks; warn and off are opt-in.
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert manifest["config_schema"]["mode"]["default"] == "enforce"
    init_src = (ROOT / "__init__.py").read_text()
    assert 'setting("mode", "enforce")' in init_src


def test_readme_discloses_persisted_window_identities():
    disclosure = (ROOT / 'README.md').read_text().split('## Disclosure')[1].split('\n## ')[0]
    assert 'proposing profile name' in disclosure
    assert '`telegram:<id>`' in disclosure
    assert 'decision snapshot' in disclosure


def test_release_version_is_consistent_and_readme_has_the_install_line():
    import re

    import change_windows
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    version = str(manifest["version"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), "a release version, not a dev marker"
    assert change_windows.__version__ == version
    readme = (ROOT / "README.md").read_text()
    assert f"({version})" in readme.split("## Why")[0] and "dev0" not in readme
    assert "hermes plugins install Gdetrane/hermes-change-windows" in readme
    assert "hermes plugins enable change-windows" in readme
    assert "waived" in readme.split("## Why")[0]  # the phone check is stated as not passed


def test_desktop_approval_setting_and_trust_statement_are_documented():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    spec = manifest["config_schema"]["approval.desktop"]
    assert spec["type"] == "bool" and spec["default"] is True
    keys = list(manifest["config_schema"])
    assert keys.index("approval.desktop") == keys.index("approver_telegram_ids") + 1  # next to the approver settings
    readme = (ROOT / "README.md").read_text()
    assert "| `approval.desktop` | `true` |" in readme
    how = readme.split("## How it works")[1].split("## Agent tools")[0]
    assert "No agent tool can approve or decline" in how and "approval.desktop: false" in how
    assert "Telegram-signed initData" in how and "not signed" in how
    desktop = readme.split("## Desktop")[1].split("## Kanban cards")[0]
    for phrase in ("Status-bar chip", "Kanban card badges", "Palette command", "sidebar row", "narrow viewport",
                   "code running as your OS user can reach the local API", "403", "Close now stays"):
        assert phrase in desktop, phrase
    assert "mobile" not in desktop.lower()  # no claim about the Hermes mobile app
    assert "has not been run\n> inside the Hermes Desktop app" in readme.split("## Why")[0]


def test_dashboard_manifest_points_at_the_backend_routes():
    import json
    data = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
    assert data["name"] == "change-windows" and data["api"] == "plugin_api.py"
    assert data["tab"]["hidden"] is True  # backend routes only: no tab in the web dashboard
    assert (ROOT / "dashboard" / data["api"]).is_file() and (ROOT / "desktop" / "plugin.js").is_file()
    assert data["version"] == yaml.safe_load((ROOT / "plugin.yaml").read_text())["version"]
