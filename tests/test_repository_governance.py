import sys
from types import SimpleNamespace

import pytest

from opportunity_radar import __version__
from opportunity_radar.cli import cmd_login, enabled_sources, main, sources
from opportunity_radar.collectors import collector_is_implemented
from opportunity_radar.collectors.mock import SAMPLES
from opportunity_radar.config import PROJECT_ROOT


def test_every_enabled_source_has_real_collector_and_mock_sample():
    enabled = enabled_sources()
    assert enabled
    assert all(collector_is_implemented(source["id"]) for source in enabled)
    assert all(source["id"] in SAMPLES for source in enabled)


def test_unimplemented_sources_are_explicitly_disabled():
    by_id = {source["id"]: source for source in sources()}
    assert by_id["crcc_ec"]["enabled"] is False
    assert by_id["powerchina"]["enabled"] is False
    assert not collector_is_implemented("crcc_ec")
    assert not collector_is_implemented("powerchina")


def test_package_version_has_one_source_of_truth():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project_section = pyproject.split("[project]", 1)[1].split("[project.optional-dependencies]", 1)[0]
    assert "\nversion =" not in project_section
    assert 'dynamic = ["version"]' in project_section
    assert 'version = {attr = "opportunity_radar.__version__"}' in pyproject
    assert __version__ == "0.2.0"


def test_cli_reports_package_version(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["radar", "--version"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"radar {__version__}"


def test_login_rejects_public_and_disabled_sources():
    with pytest.raises(SystemExit, match="无需人工登录"):
        cmd_login(SimpleNamespace(source_id="crecg_luban"))
    with pytest.raises(SystemExit, match="尚未启用"):
        cmd_login(SimpleNamespace(source_id="crcc_ec"))
