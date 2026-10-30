from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum

VlanId = int
PortId = int


class VlanPortMembership(IntEnum):
    # Wire values used by the GS108Ev3 web interface, not a safety ordering.
    UNTAGGED = 1
    TAGGED = 2
    IGNORED = 3


SingleVlanConfig = dict[PortId, VlanPortMembership]
VlanConfig = dict[VlanId, SingleVlanConfig]
PvidConfig = dict[PortId, VlanId]


@dataclass(frozen=True)
class VlanTransition:
    """A fully validated, deterministic sequence of configuration changes."""

    add_vlans: tuple[VlanId, ...]
    add_memberships: VlanConfig
    change_pvids: PvidConfig
    remove_memberships: VlanConfig
    delete_vlans: tuple[VlanId, ...]


def _validate_id(value: int, maximum: int, label: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer in 1..{maximum}: {value!r}")


def _normalize_membership(membership: VlanConfig, port_count: int) -> VlanConfig:
    normalized = {}
    for vid, ports in membership.items():
        _validate_id(vid, 4094, "VLAN ID")
        for pid, state in ports.items():
            _validate_id(pid, port_count, "Port ID")
            if isinstance(state, bool) or not isinstance(state, (int, VlanPortMembership)):
                raise ValueError(f"Invalid membership for VLAN {vid}, port {pid}: {state!r}")
            if state not in VlanPortMembership:
                raise ValueError(f"Invalid membership for VLAN {vid}, port {pid}: {state!r}")
        normalized[vid] = {
            pid: VlanPortMembership(ports.get(pid, VlanPortMembership.IGNORED))
            for pid in range(1, port_count + 1)
        }
    return dict(sorted(normalized.items()))


def _validate_pvids(pvids: PvidConfig, port_count: int) -> None:
    for pid, vid in pvids.items():
        _validate_id(pid, port_count, "Port ID")
        _validate_id(vid, 4094, "PVID")


def plan_vlan_transition(
    old_membership: VlanConfig,
    old_pvids: PvidConfig,
    membership: VlanConfig,
    pvids: PvidConfig,
    *,
    port_count: int,
    preserve_omitted_ports: bool = False,
    retain_vlan_one: bool = False,
    require_vlan_one_member: bool = False,
) -> VlanTransition:
    """Validate and plan additions, PVID moves, then removals without writes.

    This is not a guarantee of management connectivity for an arbitrary topology.
    A TAGGED/UNTAGGED conversion on an existing VLAN/port is rejected: neither
    representation can coexist there, and no atomic or out-of-band transition
    mechanism is available. Perform such changes through a separately verified
    management path instead of relying on the membership enum's numeric order.

    GS108Ev3 preserves the old PVID membership of ports absent or IGNORED in
    every requested VLAN. Other memberships on those ports are still removed.
    GS116Ev2 instead treats omitted memberships as IGNORED and retains VLAN 1.
    Omitted PVID entries always retain their current value.
    """
    _validate_id(port_count, 65535, "Port count")
    old = _normalize_membership(old_membership, port_count)
    desired = _normalize_membership(membership, port_count)
    _validate_pvids(old_pvids, port_count)
    _validate_pvids(pvids, port_count)
    if set(old_pvids) != set(range(1, port_count + 1)):
        raise ValueError("Current PVID state must include every switch port")
    for pid, vid in old_pvids.items():
        if vid not in old:
            raise ValueError(f"Current PVID {vid} for port {pid} has no VLAN")
    # A snapshot round-trip must not try to repair or reject pre-existing policy
    # exceptions (for example an unused VLAN 1). There is no transition to guard.
    if desired == old and old_pvids | pvids == old_pvids:
        return VlanTransition((), {}, {}, {}, ())

    if require_vlan_one_member and not any(
        state != VlanPortMembership.IGNORED for state in desired.get(1, {}).values()
    ):
        raise ValueError("VLAN 1 must retain at least one member port")
    if retain_vlan_one and 1 in old and 1 not in desired:
        desired[1] = old[1].copy()
    if preserve_omitted_ports:
        active_ports = {
            pid
            for ports in desired.values()
            for pid, state in ports.items()
            if state != VlanPortMembership.IGNORED
        }
        for pid in sorted(set(old_pvids) - active_ports):
            vid = old_pvids[pid]
            desired.setdefault(
                vid, dict.fromkeys(range(1, port_count + 1), VlanPortMembership.IGNORED)
            )[pid] = old[vid][pid]

    final_pvids = old_pvids | pvids
    for pid, vid in final_pvids.items():
        if vid not in desired:
            raise ValueError(f"PVID {vid} for port {pid} would reference a removed VLAN")
        if pid in pvids and desired[vid][pid] == VlanPortMembership.IGNORED:
            raise ValueError(f"Port {pid} must be a member of its requested PVID VLAN {vid}")

    additions = {}
    removals = {}
    ignored = dict.fromkeys(range(1, port_count + 1), VlanPortMembership.IGNORED)
    for vid in sorted(desired):
        before = old.get(vid, ignored)
        after = desired[vid]
        intermediate = before.copy()
        for pid, state in after.items():
            previous = before[pid]
            if previous == state:
                continue
            if previous != VlanPortMembership.IGNORED and state != VlanPortMembership.IGNORED:
                raise ValueError(
                    f"Unsafe tagged/untagged conversion on VLAN {vid}, port {pid}; "
                    "use a separately verified management path"
                )
            if previous == VlanPortMembership.IGNORED:
                intermediate[pid] = state
        if intermediate != before:
            additions[vid] = intermediate
        if after != intermediate:
            removals[vid] = after

    return VlanTransition(
        add_vlans=tuple(sorted(desired.keys() - old.keys())),
        add_memberships=additions,
        change_pvids={pid: vid for pid, vid in sorted(pvids.items()) if old_pvids[pid] != vid},
        remove_memberships=removals,
        delete_vlans=tuple(sorted(old.keys() - desired.keys())),
    )


def execute_vlan_transition(
    plan: VlanTransition,
    *,
    add_vlan: Callable[[VlanId], None],
    set_membership: Callable[[VlanId, SingleVlanConfig], None],
    set_pvids: Callable[[PvidConfig], None],
    delete_vlans: Callable[[list[VlanId]], None],
) -> None:
    """Execute only changes; an unchanged snapshot never invokes a writer."""
    for vid in plan.add_vlans:
        add_vlan(vid)
    for vid, ports in plan.add_memberships.items():
        set_membership(vid, ports)
    if plan.change_pvids:
        set_pvids(plan.change_pvids)
    for vid, ports in plan.remove_memberships.items():
        set_membership(vid, ports)
    if plan.delete_vlans:
        delete_vlans(list(plan.delete_vlans))


class BaseSwitch:
    _port_count: int
    _preserve_omitted_ports = False
    _retain_vlan_one = False
    _require_vlan_one_member = False

    def login(self):
        raise NotImplementedError

    def logout(self):
        raise NotImplementedError

    @contextmanager
    def logged_in(self):
        self.login()
        try:
            yield
        finally:
            self.logout()

    def backup(self) -> bytes:
        """Fetch a switch configuration backup."""
        raise NotImplementedError

    def restore(self, config: bytes):
        raise NotImplementedError

    def fetch_information(self) -> dict[str, str]:
        """Fetch basic information such as name and firmware version."""
        raise NotImplementedError

    def fetch_vlan_membership(self) -> VlanConfig:
        raise NotImplementedError

    def fetch_pvids(self) -> PvidConfig:
        raise NotImplementedError

    def apply_vlan_config(self, membership: VlanConfig, pvids: PvidConfig):
        plan = plan_vlan_transition(
            self.fetch_vlan_membership(),
            self.fetch_pvids(),
            membership,
            pvids,
            port_count=self._port_count,
            preserve_omitted_ports=self._preserve_omitted_ports,
            retain_vlan_one=self._retain_vlan_one,
            require_vlan_one_member=self._require_vlan_one_member,
        )
        execute_vlan_transition(
            plan,
            add_vlan=self._add_vlan,
            set_membership=self._write_vlan_membership,
            set_pvids=self._write_pvids,
            delete_vlans=self._delete_vlans,
        )

    def _add_vlan(self, vid: VlanId):
        raise NotImplementedError

    def _delete_vlans(self, vids: list[VlanId]):
        raise NotImplementedError

    def _write_vlan_membership(self, vid: VlanId, membership: SingleVlanConfig):
        raise NotImplementedError

    def _write_pvids(self, pvids: PvidConfig):
        raise NotImplementedError

    def fetch_statistics(self) -> dict:
        raise NotImplementedError
