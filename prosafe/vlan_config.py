import json
import os
import tomllib
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.functional_validators import BeforeValidator

from .switches import SwitchModel
from .switches.general import PortId, PvidConfig, VlanConfig, VlanId, VlanPortMembership


def _validate_vlan_note_list(notes: list[str]) -> dict[VlanId, VlanPortMembership]:
    if not isinstance(notes, list):
        raise ValueError("VLAN memberships must be a list of notes such as '1U'")
    data = {}
    statuses = {
        "U": VlanPortMembership.UNTAGGED,
        "T": VlanPortMembership.TAGGED,
        "N": VlanPortMembership.IGNORED,
    }
    for note in notes:
        if not isinstance(note, str) or note[-1:] not in statuses:
            raise ValueError("VLAN notes must end in U, T, or N")
        vid = int(note[:-1])
        if not 1 <= vid <= 4094:
            raise ValueError("VLAN IDs must be between 1 and 4094")
        if vid in data:
            raise ValueError(f"Duplicate VLAN ID: {vid}")
        data[vid] = statuses[note[-1]]
    return data


_VlanNoteDict = Annotated[
    dict[VlanId, VlanPortMembership], BeforeValidator(_validate_vlan_note_list)
]


class _PortVlanConfig(BaseModel):
    pvid: int = Field(ge=1, le=4094)
    vlans: _VlanNoteDict


class SwitchVlanConfig(BaseModel):
    address: str
    password: str | None = Field(default=None, repr=False)
    password_env: str | None = None
    model: SwitchModel
    # Explicit IDs retain VLANs with no participating ports.
    vlans: list[VlanId] = Field(default_factory=list)
    ports: dict[PortId, _PortVlanConfig]

    @field_validator("vlans")
    @classmethod
    def check_vlan_ids(cls, vlans: list[VlanId]) -> list[VlanId]:
        if len(set(vlans)) != len(vlans):
            raise ValueError("Duplicate VLAN IDs in switch VLAN list")
        if any(not 1 <= vid <= 4094 for vid in vlans):
            raise ValueError("VLAN IDs must be between 1 and 4094")
        return vlans

    @model_validator(mode="after")
    def check_pvid_and_pid(self) -> Self:
        if (self.password is None) == (self.password_env is None):
            raise ValueError("Specify exactly one of password or password_env")
        if self.password_env is not None and not self.password_env:
            raise ValueError("password_env must name an environment variable")
        for port, config in self.ports.items():
            if config.pvid not in config.vlans and config.pvid not in self.vlans:
                raise ValueError(f"Port {port} PVID {config.pvid} is not declared as a VLAN")
            if not 1 <= port <= self.model.port_count:
                raise ValueError(f"Port {port} must be between 1 and {self.model.port_count}")
        return self

    def get_password(self) -> str:
        if self.password is not None:
            return self.password
        password = os.environ.get(self.password_env)
        if password is None:
            raise ValueError(f"Set the {self.password_env} environment variable")
        return password

    def get_config_by_vlan(self, vid: VlanId) -> dict[PortId, VlanPortMembership]:
        return {
            port: config.vlans.get(vid, VlanPortMembership.IGNORED)
            for port, config in self.ports.items()
        }

    def get_vlan_membership(self) -> VlanConfig:
        vids = set(self.vlans)
        for config in self.ports.values():
            vids.update(config.vlans)
        membership = {
            vid: {port: VlanPortMembership.IGNORED for port in range(1, self.model.port_count + 1)}
            for vid in sorted(vids)
        }
        for port, config in self.ports.items():
            for vid, status in config.vlans.items():
                membership[vid][port] = status
        return membership

    def get_pvids(self) -> PvidConfig:
        return {port: config.pvid for port, config in self.ports.items()}


def load_config(filename: str | Path) -> dict[str, SwitchVlanConfig]:
    with open(filename, "rb") as stream:
        config_data = tomllib.load(stream)
    switches = config_data.get("switches")
    if not isinstance(switches, dict) or not switches:
        raise ValueError("Specify at least one switch under [switches]")
    return {name: SwitchVlanConfig.model_validate(config) for name, config in switches.items()}


def serialize_config(
    name: str,
    *,
    address: str,
    model: SwitchModel,
    membership: VlanConfig,
    pvids: PvidConfig,
    password_env: str = "PROSAFE_PASSWORD",
) -> str:
    """Serialize a complete switch snapshot, never embedding its password.

    Missing membership entries mean IGNORED, as they do in the parser. The
    explicit VLAN list preserves even entirely empty membership dictionaries.
    Every physical port must have a PVID so an export cannot silently omit one.
    """
    model = SwitchModel(model)
    ports = set(range(1, model.port_count + 1))
    if set(pvids) != ports:
        raise ValueError("Export requires a PVID for every physical port")
    if any(not set(row).issubset(ports) for row in membership.values()):
        raise ValueError("Membership contains a port outside the model's port range")
    statuses = {
        VlanPortMembership.UNTAGGED: "U",
        VlanPortMembership.TAGGED: "T",
        VlanPortMembership.IGNORED: "N",
    }

    # JSON basic strings are also TOML basic strings for these escaped values.
    def quote(value: object) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    table = f"switches.{quote(name)}"
    lines = [
        f"[{table}]",
        f"address = {quote(address)}",
        f"model = {quote(model.value)}",
        f"password_env = {quote(password_env)}",
        f"vlans = {sorted(membership)}",
        "",
        f"[{table}.ports]",
    ]
    for port in sorted(ports):
        notes = [
            quote(f"{vid}{statuses[VlanPortMembership(row[port])]}")
            for vid, row in sorted(membership.items())
            if port in row
        ]
        lines.append(f"{port} = {{ pvid = {pvids[port]}, vlans = [{', '.join(notes)}] }}")
    text = "\n".join(lines) + "\n"
    # Validate the generated document through the same schema apply uses.
    SwitchVlanConfig.model_validate(tomllib.loads(text)["switches"][name])
    return text
