"""Apply safety regressions exercise real adapters with only in-memory writers."""

import unittest
from copy import deepcopy

from prosafe.switches.general import (
    BaseSwitch,
    VlanPortMembership,
    execute_vlan_transition,
    plan_vlan_transition,
)
from prosafe.switches.gs108ev3.switch import Switch as GS108Ev3
from prosafe.switches.gs116ev2.switch import Switch as GS116Ev2

U = VlanPortMembership.UNTAGGED
T = VlanPortMembership.TAGGED
N = VlanPortMembership.IGNORED


class StatefulPrimitives:
    """Record writes and verify each PVID has an existing VLAN during apply."""

    def __init__(self, membership, pvids):
        self.membership = deepcopy(membership)
        self.pvids = pvids.copy()
        self.writes = []

    def fetch_vlan_membership(self):
        return deepcopy(self.membership)

    def fetch_pvids(self):
        return self.pvids.copy()

    def _add_vlan(self, vid):
        self.writes.append(("add", vid))
        self.membership[vid] = dict.fromkeys(range(1, self._port_count + 1), N)

    def _set_vlan_membership(self, vid, ports):
        if isinstance(ports, str):
            ports = {pid: VlanPortMembership(int(state)) for pid, state in enumerate(ports, 1)}
        self.writes.append(("membership", vid, ports.copy()))
        self.membership[vid] = ports.copy()

    def _set_pvids(self, pvids):
        for pid, vid in pvids.items():
            assert self.membership[vid][pid] != N, "PVID moved before destination membership"
        self.writes.append(("pvid", pvids.copy()))
        self.pvids.update(pvids)

    def _set_ports_pvid(self, ports, vid):
        self._set_pvids(dict.fromkeys(ports, vid))

    def _delete_vlans(self, vids):
        assert not set(vids) & set(self.pvids.values()), "Deleted a VLAN before moving its PVIDs"
        self.writes.append(("delete", vids.copy()))
        for vid in vids:
            del self.membership[vid]


class MemoryGS108(StatefulPrimitives, GS108Ev3):
    _port_count = 3


class MemoryGS116(StatefulPrimitives, GS116Ev2):
    _port_count = 3


class MemoryShared(StatefulPrimitives, BaseSwitch):
    _port_count = 3

    def _write_vlan_membership(self, vid, membership):
        self._set_vlan_membership(vid, membership)

    def _write_pvids(self, pvids):
        self._set_pvids(pvids)


class ApplyOrderTests(unittest.TestCase):
    def test_unchanged_snapshots_emit_no_writes(self):
        membership = {1: {1: U, 2: U, 3: N}, 100: {1: T, 2: T, 3: U}, 200: {1: N, 2: N, 3: N}}
        pvids = {1: 1, 2: 1, 3: 100}
        for adapter in (MemoryShared, MemoryGS108, MemoryGS116):
            with self.subTest(adapter=adapter.__name__):
                switch = adapter(membership, pvids)
                switch.apply_vlan_config(deepcopy(membership), pvids.copy())
                self.assertEqual(switch.writes, [])
                self.assertEqual(switch.membership, membership)

    def test_additive_migration_precedes_pvid_and_old_vlan_deletion(self):
        for adapter in (MemoryShared, MemoryGS108, MemoryGS116):
            with self.subTest(adapter=adapter.__name__):
                switch = adapter(
                    {1: {1: U, 2: N, 3: N}, 10: {1: N, 2: U, 3: U}},
                    {1: 1, 2: 10, 3: 10},
                )
                desired = {1: {1: U, 2: N, 3: N}, 20: {1: N, 2: U, 3: U}}
                switch.apply_vlan_config(desired, {1: 1, 2: 20, 3: 20})
                self.assertEqual(
                    [write[0] for write in switch.writes], ["add", "membership", "pvid", "delete"]
                )
                self.assertEqual(switch.writes[0], ("add", 20))
                self.assertEqual(switch.writes[-1], ("delete", [10]))
                self.assertEqual(switch.membership, desired)

    def test_unchanged_snapshot_does_not_repair_existing_policy_exceptions(self):
        membership = {1: {1: N, 2: N, 3: N}, 20: {1: U, 2: U, 3: N}}
        pvids = {1: 20, 2: 20, 3: 20}
        for adapter in (MemoryShared, MemoryGS108, MemoryGS116):
            with self.subTest(adapter=adapter.__name__):
                switch = adapter(membership, pvids)
                switch.apply_vlan_config(membership, pvids)
                self.assertEqual(switch.writes, [])

    def test_mixed_membership_changes_keep_old_members_until_pvid_moves(self):
        switch = MemoryShared(
            {1: {1: U, 2: N, 3: U}, 20: {1: N, 2: U, 3: N}},
            {1: 1, 2: 20, 3: 1},
        )
        desired = {1: {1: U, 2: U, 3: N}, 20: {1: N, 2: N, 3: U}}
        switch.apply_vlan_config(desired, {2: 1, 3: 20})
        self.assertEqual(
            [write[0] for write in switch.writes],
            ["membership", "membership", "pvid", "membership", "membership"],
        )
        self.assertEqual(switch.writes[0], ("membership", 1, {1: U, 2: U, 3: U}))
        self.assertEqual(switch.writes[1], ("membership", 20, {1: N, 2: U, 3: U}))
        self.assertEqual(switch.membership, desired)

    def test_existing_vlan_removals_happen_after_pvid_transition(self):
        switch = MemoryShared(
            {1: {1: U, 2: N, 3: U}, 20: {1: N, 2: U, 3: N}},
            {1: 1, 2: 20, 3: 1},
        )
        desired = {1: {1: U, 2: N, 3: N}, 20: {1: N, 2: U, 3: U}}
        switch.apply_vlan_config(desired, {3: 20})
        self.assertEqual(
            [write[0] for write in switch.writes], ["membership", "pvid", "membership"]
        )
        self.assertEqual(switch.writes[0][1], 20)
        self.assertEqual(switch.writes[-1][1], 1)
        self.assertEqual(switch.membership, desired)

    def test_invalid_desired_configs_fail_before_any_mutations(self):
        invalid = [
            ({1: {1: U}, 0: {2: U}}, {2: 0}),
            ({1: {1: U}, 4095: {2: U}}, {2: 4095}),
            ({1: {1: U}, 20: {4: U}}, {}),
            ({1: {1: U}, 20: {2: 99}}, {}),
            ({1: {1: U}, 20: {2: True}}, {}),
            ({1: {1: U}, 20: {2: N}}, {2: 20}),
            ({1: {1: U}, 20: {2: U}}, {4: 20}),
            ({1: {1: U}, 20: {2: U}}, {2: 30}),
            ({20: {2: U}}, {2: 20}),
        ]
        for adapter in (MemoryShared, MemoryGS108, MemoryGS116):
            for membership, pvids in invalid:
                with self.subTest(adapter=adapter.__name__, membership=membership, pvids=pvids):
                    switch = adapter({1: {1: U, 2: U, 3: U}}, {1: 1, 2: 1, 3: 1})
                    # GS108 preserves omitted ports and their VLAN, so dropping VLAN 1
                    # is valid there; explicitly move port 1 to a missing VLAN instead.
                    if membership == {20: {2: U}} and adapter is MemoryGS108:
                        pvids = {1: 30, 2: 20}
                    with self.assertRaises(ValueError):
                        switch.apply_vlan_config(membership, pvids)
                    self.assertEqual(switch.writes, [])

    def test_both_tagging_conversions_fail_before_earlier_additions(self):
        for adapter in (MemoryShared, MemoryGS108, MemoryGS116):
            for before, after in ((T, U), (U, T)):
                with self.subTest(adapter=adapter.__name__, before=before):
                    switch = adapter(
                        {1: {1: U, 2: U, 3: U}, 20: {1: before, 2: N, 3: N}},
                        {1: 1, 2: 1, 3: 1},
                    )
                    with self.assertRaisesRegex(ValueError, "Unsafe tagged/untagged"):
                        switch.apply_vlan_config(
                            {1: {1: U, 2: U, 3: U}, 10: {1: T}, 20: {1: after}},
                            {},
                        )
                    self.assertEqual(switch.writes, [])

    def test_gs108_preserves_only_omitted_ports_old_pvid_membership(self):
        switch = MemoryGS108(
            {1: {1: U, 2: N, 3: N}, 10: {1: N, 2: U, 3: U}, 20: {1: N, 2: T, 3: T}},
            {1: 1, 2: 10, 3: 10},
        )
        switch.apply_vlan_config({1: {1: U, 2: N}}, {1: 1})
        self.assertEqual(switch.membership, {1: {1: U, 2: N, 3: N}, 10: {1: N, 2: U, 3: U}})
        self.assertEqual(switch.pvids, {1: 1, 2: 10, 3: 10})
        self.assertNotIn("pvid", [write[0] for write in switch.writes])

    def test_gs108_omitted_ports_on_unchanged_vlan_do_not_crash_or_write(self):
        switch = MemoryGS108({1: {1: U, 2: U, 3: U}}, {1: 1, 2: 1, 3: 1})
        switch.apply_vlan_config({1: {1: U}}, {1: 1})
        self.assertEqual(switch.writes, [])

    def test_gs116_omitted_membership_is_ignored_but_pvid_is_unchanged(self):
        switch = MemoryGS116(
            {1: {1: U, 2: U, 3: U}, 20: {1: T, 2: T, 3: T}},
            {1: 1, 2: 1, 3: 1},
        )
        switch.apply_vlan_config({1: {1: U}, 20: {1: T}}, {1: 1})
        self.assertEqual(switch.membership, {1: {1: U, 2: N, 3: N}, 20: {1: T, 2: N, 3: N}})
        self.assertEqual(switch.pvids, {1: 1, 2: 1, 3: 1})
        self.assertEqual([write[0] for write in switch.writes], ["membership", "membership"])

    def test_gs116_requires_active_vlan_one_before_changes(self):
        switch = MemoryGS116({1: {1: U, 2: U, 3: U}}, {1: 1, 2: 1, 3: 1})
        with self.assertRaisesRegex(ValueError, "VLAN 1"):
            switch.apply_vlan_config({1: {}, 20: {1: U, 2: U, 3: U}}, {1: 20, 2: 20, 3: 20})
        self.assertEqual(switch.writes, [])

    def test_shared_planner_retains_vlan_one_when_adapter_requests_it(self):
        plan = plan_vlan_transition(
            {1: {1: U}, 10: {2: U, 3: U}},
            {1: 1, 2: 10, 3: 10},
            {10: {2: U, 3: U}},
            {},
            port_count=3,
            retain_vlan_one=True,
        )
        self.assertEqual(plan.delete_vlans, ())
        self.assertEqual(plan.remove_memberships, {})

    def test_plan_is_deterministic_and_does_not_mutate_inputs(self):
        old = {30: {1: N, 2: U, 3: N}, 1: {1: U, 2: N, 3: U}}
        desired = {20: {3: U}, 1: {1: U}, 10: {2: U}}
        old_copy, desired_copy = deepcopy(old), deepcopy(desired)
        plan = plan_vlan_transition(old, {1: 1, 2: 30, 3: 1}, desired, {3: 20, 2: 10}, port_count=3)
        self.assertEqual(plan.add_vlans, (10, 20))
        self.assertEqual(list(plan.add_memberships), [10, 20])
        self.assertEqual(list(plan.change_pvids), [2, 3])
        self.assertEqual(plan.delete_vlans, (30,))
        self.assertEqual(old, old_copy)
        self.assertEqual(desired, desired_copy)

    def test_standalone_executor_can_drive_an_independent_adapter(self):
        switch = MemoryShared({1: {1: U, 2: U, 3: U}}, {1: 1, 2: 1, 3: 1})
        plan = plan_vlan_transition(
            switch.membership,
            switch.pvids,
            {1: {1: U, 2: U, 3: U}, 20: {1: T}},
            {},
            port_count=3,
        )
        execute_vlan_transition(
            plan,
            add_vlan=switch._add_vlan,
            set_membership=switch._set_vlan_membership,
            set_pvids=switch._set_pvids,
            delete_vlans=switch._delete_vlans,
        )
        self.assertEqual([write[0] for write in switch.writes], ["add", "membership"])


if __name__ == "__main__":
    unittest.main()
