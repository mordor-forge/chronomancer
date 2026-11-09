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
