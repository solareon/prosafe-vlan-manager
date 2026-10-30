# ProSAFE VLAN Manager

A Python utility and library for declarative 802.1Q VLAN configuration on Netgear
ProSAFE switches. It uses each switch's web interface rather than NSDP.

## Requirements and installation

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
uv sync --python 3.14
uv run --python 3.14 prosafe --help
```

Python 3.14 or newer is required; `.python-version` selects 3.14 by default. The
package installs the `prosafe` console command. `uv run python -m prosafe` is also
supported. Development dependencies include pytest and Ruff.

## Supported switches

| Model | Firmware / coverage |
| --- | --- |
| GS108Ev3 | Existing support for V2.06.10EN and V2.06.24EN |
| GS116Ev2 | Existing support for V2.6.0.48 |
| GS308E (original model) | Export and VLAN creation, membership, and PVID writes verified on V1.00.08EN with staged readback |

GS308E support refers to the original GS308E, **not** GS308Ev4 or another similarly
named model. Live GS308E writes were exercised through the driver with per-stage
verification; restore, reboot, and firmware operations have not been verified.
Optional binary backup and restore are not implemented for GS308E; requesting
either fails before VLAN changes are applied.

Advanced 802.1Q mode must already be enabled. This tool does not enable it for you,
change VLAN modes, reboot switches, or update firmware as part of export/apply.

## Export current configuration (read-only)

```bash
uv run --python 3.14 prosafe export \
  --address 192.0.2.1 --model gs308e --output switch.toml
```

The command prompts for a password without echoing it. For noninteractive use,
set `PROSAFE_PASSWORD` in your environment or secret manager. For example, in Bash:

```bash
read -r -s -p 'Switch password: ' PROSAFE_PASSWORD
export PROSAFE_PASSWORD
printf '\n'
uv run --python 3.14 prosafe export \
  --address 192.0.2.1 --model gs308e --output switch.toml
unset PROSAFE_PASSWORD
```

`--password` is available, but environment variables or the prompt avoid exposing
the password in shell history and command-line process listings. Exported files
contain `password_env = "PROSAFE_PASSWORD"`, **never the password**. Environment
references are resolved only when applying, so exports can be inspected without
credentials. Existing configurations with a literal `password` remain supported;
do not commit credentials to source control.

Export logs in, reads every VLAN and every physical port's PVID, then logs out.
For GS308E, selecting each VLAN for reading requires a selection-only HTTP POST
with `VLAN_ID` and the session hash. It omits the membership-writing fields
`hiddenMem` and `VLAN_ID_HD`: this is a read selection, not a configuration write.
Export does not call VLAN/PVID writers, backup, restore, mode changes, or reboot.
A read failure exits nonzero without publishing a partial export.

Use `--name core` to choose the switch's configuration name, or `--output -` to
write TOML to standard output. The exported snapshot retains all VLAN IDs,
including VLANs with no participating ports, all explicit excluded memberships,
and every PVID.

## Apply declarative configuration

Review an exported file before applying it:

```bash
uv run --python 3.14 prosafe apply --config switch.toml
```

Provide the environment variable referenced by `password_env` before applying.
The planner compares the current and desired state first. Applying an unchanged
export makes **no configuration writes**. Changes run in this order:
create VLANs → add memberships → move PVIDs → remove memberships → delete VLANs.
Existing tagged↔untagged conversions on the same VLAN/port are rejected before
any write: ordering alone cannot safely perform these without a verified
independent management path. GS108Ev3 and GS308E preserve an omitted port's old
PVID-VLAN membership; GS116Ev2 retains omitted PVIDs but excludes omitted
memberships, subject to its VLAN 1 guard. Applying changes can still interrupt
connectivity: review the intended topology and use an independent recovery path.

Failures stop processing and return a nonzero exit status. **There is no automatic
backup restore by default.** A full restore is potentially disruptive and is not
a safe generic response to a failed VLAN change. On a driver supporting backups:

```bash
# Save a backup without enabling automatic restore:
uv run prosafe apply --config switch.toml --savepath ./backups

# Explicitly opt into full restore if the apply operation fails:
uv run prosafe apply --config switch.toml --restore-on-failure
```

The backup directory must already exist. `--restore-on-failure` makes an in-memory
backup even without `--savepath`. GS308E does not implement these optional backup
or restore operations. The old `--norestore` flag has been removed: no restore is
now the default, and restoring requires the explicit option above.

## Configuration format

Configuration is TOML, with one or more named entries under `switches`. Model
names match the CLI registry (`gs108ev3`, `gs116ev2`, `gs308e`).

```toml
[switches.core]
address = "192.0.2.1"
model = "gs308e"
password_env = "PROSAFE_PASSWORD"
# Explicit VLAN IDs preserve even empty VLANs such as 200.
vlans = [1, 100, 200]

[switches.core.ports]
1 = { pvid = 1,   vlans = ["1U", "100T", "200N"] }
2 = { pvid = 1,   vlans = ["1U", "100T", "200N"] }
3 = { pvid = 100, vlans = ["1N", "100U", "200N"] }
4 = { pvid = 100, vlans = ["1N", "100U", "200N"] }
5 = { pvid = 100, vlans = ["1N", "100U", "200N"] }
6 = { pvid = 100, vlans = ["1N", "100U", "200N"] }
7 = { pvid = 100, vlans = ["1N", "100U", "200N"] }
8 = { pvid = 100, vlans = ["1N", "100U", "200N"] }
```

A membership note is a VLAN ID followed by `U` (untagged), `T` (tagged), or `N`
(excluded / ignored). Missing memberships default to excluded. VLAN IDs range
from 1 to 4094. A port's PVID must refer to a declared VLAN, either in its notes
or in the switch-level `vlans` list. Switch-level `vlans` is optional for older
handwritten configurations; VLAN IDs appearing in port notes are always included.
Only one of `password` and `password_env` may be set. Multiple switches may use
different environment variable names.

Exports include every port; partial handwritten configurations should be used
carefully. Omitted PVIDs are retained. GS108Ev3 and GS308E preserve membership
needed by omitted ports; GS116Ev2 treats omitted memberships as excluded.

## Development

```bash
uv sync --python 3.14
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests use fake transports and sanitized captured HTML; they do not require a live
switch. CLI help is available with `uv run prosafe export --help` and
`uv run prosafe apply --help`.

## Local lab operations for agents

Read [.agents/skills/local-switch/SKILL.md](.agents/skills/local-switch/SKILL.md)
before manipulating the local GS308E or lab power. It records the last observed
port layout, credential handling, preflight/apply/readback workflow, Tapo device
selection, and boot-capture procedure. Always rediscover live state; the skill
is not standing authorization to change configuration or power.

This project is licensed under AGPL-3.0-only.
