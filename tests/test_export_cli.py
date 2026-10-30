import importlib

import pytest
from click.testing import CliRunner

from prosafe.cli import cli
from prosafe.switches import SWITCH_DRIVER
from prosafe.switches.general import BaseSwitch, VlanPortMembership
from prosafe.vlan_config import load_config, serialize_config


@pytest.fixture
def fake_switch(monkeypatch):
    class FakeSwitch(BaseSwitch):
        calls = []
        failure = None
        membership = {
            1: {port: VlanPortMembership.UNTAGGED for port in range(1, 9)},
            200: {port: VlanPortMembership.IGNORED for port in range(1, 9)},
        }
        pvids = dict.fromkeys(range(1, 9), 1)

        def __init__(self, address, password):
            self.calls.append(("connect", address, password))

        def login(self):
            self.calls.append("login")

        def logout(self):
            self.calls.append("logout")

        def fetch_vlan_membership(self):
            self.calls.append("membership")
            if self.failure == "read":
                raise RuntimeError("authentication data: test-only-secret")
            return self.membership

        def fetch_pvids(self):
            self.calls.append("pvids")
            return self.pvids

        def backup(self):
            self.calls.append("backup")
            return b"test backup"

        def restore(self, data):
            assert data == b"test backup"
            self.calls.append("restore")

        def apply_vlan_config(self, membership, pvids):
            self.calls.append("apply")
            assert membership == self.membership
            assert pvids == self.pvids
            if self.failure == "apply":
                raise RuntimeError("authentication data: test-only-secret")

    monkeypatch.setitem(SWITCH_DRIVER, "gs108ev3", FakeSwitch)
    return FakeSwitch


def export_args(output):
    return ["export", "--address", "192.0.2.1", "--model", "gs108ev3", "--output", str(output)]


def config_file(tmp_path, fake_switch):
    config = tmp_path / "switch.toml"
    config.write_text(
        serialize_config(
            "switch1",
            address="192.0.2.1",
            model="gs108ev3",
            membership=fake_switch.membership,
            pvids=fake_switch.pvids,
        )
    )
    return config


def test_export_uses_only_read_methods_and_does_not_embed_password(tmp_path, fake_switch):
    output = tmp_path / "export.toml"
    result = CliRunner().invoke(
        cli, export_args(output), env={"PROSAFE_PASSWORD": "test-only-secret"}
    )
    assert result.exit_code == 0, result.output
    config = load_config(output)["switch1"]
    assert config.get_vlan_membership() == fake_switch.membership
    assert config.get_pvids() == fake_switch.pvids
    assert fake_switch.calls == [
        ("connect", "192.0.2.1", "test-only-secret"),
        "login",
        "membership",
        "pvids",
        "logout",
    ]
    assert "test-only-secret" not in output.read_text()
    assert "test-only-secret" not in result.output


def test_export_prompts_without_echoing_password(tmp_path, fake_switch, monkeypatch):
    monkeypatch.delenv("PROSAFE_PASSWORD", raising=False)
    result = CliRunner().invoke(
        cli, export_args(tmp_path / "export.toml"), input="test-only-secret\n"
    )
    assert result.exit_code == 0, result.output
    assert "Password:" in result.output
    assert "test-only-secret" not in result.output


def test_export_stdout_is_parseable(tmp_path, fake_switch):
    result = CliRunner().invoke(cli, export_args("-"), env={"PROSAFE_PASSWORD": "test-only-secret"})
    assert result.exit_code == 0, result.output
    output = tmp_path / "stdout.toml"
    output.write_text(result.output)
    assert load_config(output)["switch1"].get_vlan_membership() == fake_switch.membership


def test_export_failure_does_not_publish_output_or_credentials(tmp_path, fake_switch):
    fake_switch.failure = "read"
    output = tmp_path / "export.toml"
    output.write_text("existing file")
    result = CliRunner().invoke(
        cli, export_args(output), env={"PROSAFE_PASSWORD": "test-only-secret"}
    )
    assert result.exit_code != 0
    assert "test-only-secret" not in result.output
    assert output.read_text() == "existing file"
    assert fake_switch.calls[-1] == "logout"
    assert "backup" not in fake_switch.calls
    assert "restore" not in fake_switch.calls


def test_apply_export_does_not_backup_or_restore_by_default(tmp_path, fake_switch):
    config = config_file(tmp_path, fake_switch)
    result = CliRunner().invoke(
        cli, ["apply", "--config", str(config)], env={"PROSAFE_PASSWORD": "test-only-secret"}
    )
    assert result.exit_code == 0, result.output
    assert "apply" in fake_switch.calls
    assert "backup" not in fake_switch.calls
    assert "restore" not in fake_switch.calls


def test_apply_failure_is_nonzero_without_automatic_restore(tmp_path, fake_switch):
    fake_switch.failure = "apply"
    config = config_file(tmp_path, fake_switch)
    result = CliRunner().invoke(
        cli, ["apply", "--config", str(config)], env={"PROSAFE_PASSWORD": "test-only-secret"}
    )
    assert result.exit_code != 0
    assert "All done!" not in result.output
    assert "test-only-secret" not in result.output
    assert "backup" not in fake_switch.calls
    assert "restore" not in fake_switch.calls


def test_restore_requires_explicit_opt_in(tmp_path, fake_switch):
    fake_switch.failure = "apply"
    config = config_file(tmp_path, fake_switch)
    result = CliRunner().invoke(
        cli,
        ["apply", "--config", str(config), "--restore-on-failure"],
        env={"PROSAFE_PASSWORD": "test-only-secret"},
    )
    assert result.exit_code != 0
    assert fake_switch.calls[-4:] == ["backup", "apply", "restore", "logout"]


def test_savepath_creates_backup_without_enabling_restore(tmp_path, fake_switch):
    fake_switch.failure = "apply"
    config = config_file(tmp_path, fake_switch)
    result = CliRunner().invoke(
        cli,
        ["apply", "--config", str(config), "--savepath", str(tmp_path)],
        env={"PROSAFE_PASSWORD": "test-only-secret"},
    )
    assert result.exit_code != 0
    assert (tmp_path / "switch1.cfg").read_bytes() == b"test backup"
    assert "restore" not in fake_switch.calls


def test_missing_password_fails_before_connecting(tmp_path, fake_switch, monkeypatch):
    monkeypatch.delenv("PROSAFE_PASSWORD", raising=False)
    config = config_file(tmp_path, fake_switch)
    result = CliRunner().invoke(cli, ["apply", "--config", str(config)])
    assert result.exit_code != 0
    assert fake_switch.calls == []


def test_module_entrypoint_is_import_safe():
    module = importlib.import_module("prosafe.__main__")
    assert module.cli is cli


def test_gs308e_is_offered_by_registry():
    result = CliRunner().invoke(cli, ["export", "--help"])
    assert result.exit_code == 0
    assert "gs308e" in result.output


def test_explicit_password_option_does_not_leak(tmp_path, fake_switch, monkeypatch):
    monkeypatch.delenv("PROSAFE_PASSWORD", raising=False)
    result = CliRunner().invoke(
        cli, export_args(tmp_path / "export.toml") + ["--password", "test-only-secret"]
    )
    assert result.exit_code == 0, result.output
    assert "test-only-secret" not in result.output
    assert "test-only-secret" not in (tmp_path / "export.toml").read_text()


def test_unknown_model_is_rejected_without_connecting(tmp_path, fake_switch):
    args = export_args(tmp_path / "export.toml")
    args[4] = "unsupported"
    result = CliRunner().invoke(cli, args, env={"PROSAFE_PASSWORD": "test-only-secret"})
    assert result.exit_code != 0
    assert fake_switch.calls == []


@pytest.mark.parametrize("option", ["--savepath", "--restore-on-failure"])
def test_unsupported_backup_fails_before_apply(tmp_path, fake_switch, monkeypatch, option):
    monkeypatch.setattr(fake_switch, "backup", BaseSwitch.backup)
    config = config_file(tmp_path, fake_switch)
    args = ["apply", "--config", str(config), option]
    if option == "--savepath":
        args.append(str(tmp_path))
    result = CliRunner().invoke(cli, args, env={"PROSAFE_PASSWORD": "test-only-secret"})
    assert result.exit_code != 0
    assert "apply" not in fake_switch.calls
    assert "restore" not in fake_switch.calls
