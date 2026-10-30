import tomllib

import pytest
from pydantic import ValidationError

from prosafe.switches import SwitchModel
from prosafe.switches.general import VlanPortMembership as Membership
from prosafe.vlan_config import SwitchVlanConfig, load_config, serialize_config


def snapshot():
    membership = {
        1: {port: Membership.UNTAGGED if port < 3 else Membership.IGNORED for port in range(1, 9)},
        100: {port: Membership.TAGGED if port < 3 else Membership.UNTAGGED for port in range(1, 9)},
        200: {port: Membership.IGNORED for port in range(1, 9)},
    }
    pvids = {port: 1 if port < 3 else 100 for port in range(1, 9)}
    return membership, pvids


def test_exact_roundtrip_including_excluded_vlan(tmp_path):
    membership, pvids = snapshot()
    text = serialize_config(
        'switch.with "quotes"',
        address="192.0.2.1",
        model=SwitchModel.GS108EV3,
        membership=membership,
        pvids=pvids,
    )
    filename = tmp_path / "export.toml"
    filename.write_text(text)
    config = load_config(filename)['switch.with "quotes"']
    assert config.get_vlan_membership() == membership
    assert config.get_pvids() == pvids
    assert config.password is None
    assert config.password_env == "PROSAFE_PASSWORD"
    assert "password =" not in text
    assert "200N" in text


def test_empty_membership_dictionary_is_retained():
    membership, pvids = snapshot()
    membership[300] = {}
    text = serialize_config(
        "switch1", address="192.0.2.1", model="gs108ev3", membership=membership, pvids=pvids
    )
    data = tomllib.loads(text)["switches"]["switch1"]
    assert 300 in data["vlans"]
    config = SwitchVlanConfig.model_validate(data)
    assert config.get_vlan_membership()[300] == {port: Membership.IGNORED for port in range(1, 9)}


def test_password_environment_is_resolved_only_when_requested(monkeypatch):
    membership, pvids = snapshot()
    text = serialize_config(
        "switch1", address="192.0.2.1", model="gs108ev3", membership=membership, pvids=pvids
    )
    monkeypatch.delenv("PROSAFE_PASSWORD", raising=False)
    config = SwitchVlanConfig.model_validate(tomllib.loads(text)["switches"]["switch1"])
    with pytest.raises(ValueError, match="PROSAFE_PASSWORD"):
        config.get_password()
    monkeypatch.setenv("PROSAFE_PASSWORD", "test-only-secret")
    assert config.get_password() == "test-only-secret"
    assert "test-only-secret" not in text
    assert "test-only-secret" not in repr(config)


def test_existing_plaintext_config_and_after_validator():
    config = SwitchVlanConfig.model_validate(
        {
            "address": "192.0.2.1",
            "model": "gs108ev3",
            "password": "test-only-secret",
            "ports": {"1": {"pvid": 1, "vlans": ["1U", "100T"]}},
        }
    )
    assert isinstance(config, SwitchVlanConfig)
    assert config.get_password() == "test-only-secret"
    assert "test-only-secret" not in repr(config)
    assert config.get_pvids() == {1: 1}
    assert config.get_vlan_membership()[100][1] == Membership.TAGGED


@pytest.mark.parametrize("note", ["1X", "0U", "4095T", "U", ""])
def test_invalid_membership_notes_are_rejected(note):
    with pytest.raises(ValidationError):
        SwitchVlanConfig.model_validate(
            {
                "address": "192.0.2.1",
                "model": "gs108ev3",
                "password_env": "PROSAFE_PASSWORD",
                "ports": {"1": {"pvid": 1, "vlans": [note]}},
            }
        )


def test_incomplete_snapshot_is_rejected():
    membership, pvids = snapshot()
    del pvids[8]
    with pytest.raises(ValueError, match="every physical port"):
        serialize_config(
            "switch1", address="192.0.2.1", model="gs108ev3", membership=membership, pvids=pvids
        )


def test_duplicate_vlan_notes_are_rejected():
    with pytest.raises(ValidationError, match="Duplicate VLAN ID"):
        SwitchVlanConfig.model_validate(
            {
                "address": "192.0.2.1",
                "model": "gs108ev3",
                "password_env": "PROSAFE_PASSWORD",
                "ports": {"1": {"pvid": 1, "vlans": ["1U", "1T"]}},
            }
        )
