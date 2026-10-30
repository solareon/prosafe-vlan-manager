from pathlib import Path

import click

from .switches import SwitchModel
from .vlan_config import load_config, serialize_config


@click.group()
def cli() -> None:
    """Read and manage VLAN configuration on Netgear ProSAFE switches."""


@cli.command("export")
@click.option("--address", required=True, help="Switch address or URL.")
@click.option("--model", required=True, type=click.Choice([model.value for model in SwitchModel]))
@click.option(
    "--password",
    envvar="PROSAFE_PASSWORD",
    prompt=True,
    hide_input=True,
    help="Switch password; prefer PROSAFE_PASSWORD or the hidden prompt over command arguments.",
)
@click.option(
    "--output",
    "-o",
    required=True,
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    help="Destination TOML file; use - for standard output.",
)
@click.option("--name", default="switch1", show_default=True, help="Switch name in the TOML file.")
def export(address: str, model: str, password: str, output: Path, name: str) -> None:
    """Read current VLANs and PVIDs into an apply-compatible, password-free TOML file."""
    try:
        switch_model = SwitchModel(model)
        switch = switch_model.driver(address, password)
        with switch.logged_in():
            membership = switch.fetch_vlan_membership()
            pvids = switch.fetch_pvids()
        text = serialize_config(
            name, address=address, model=switch_model, membership=membership, pvids=pvids
        )
        if str(output) == "-":
            click.echo(text, nl=False)
        else:
            output.write_text(text, encoding="utf-8")
    except Exception as exc:
        # Transport errors may include authentication request data. Never echo them.
        raise click.ClickException(f"Export failed ({type(exc).__name__})") from None


@cli.command()
@click.option(
    "--config",
    "-c",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="Path to your configuration file.",
)
@click.option(
    "--savepath",
    type=click.Path(exists=True, file_okay=False, writable=True, path_type=Path),
    help="Save switch configuration backups in this directory before applying.",
)
@click.option(
    "--restore-on-failure",
    is_flag=True,
    help="Explicitly opt into restoring a full backup if applying fails (may disrupt the switch).",
)
def apply(config: Path, savepath: Path | None, restore_on_failure: bool) -> None:
    """Apply declarative VLAN configuration; never restore automatically by default."""
    try:
        configs = load_config(config)
        # Resolve all credentials before contacting any switch.
        passwords = {name: cfg.get_password() for name, cfg in configs.items()}
    except Exception as exc:
        raise click.ClickException(f"Cannot load configuration ({type(exc).__name__})") from None

    for name, cfg in configs.items():
        click.echo(f"Processing switch '{name}' ...")
        try:
            switch = cfg.model.driver(cfg.address, passwords[name])
            with switch.logged_in():
                backup = None
                if savepath is not None or restore_on_failure:
                    backup = switch.backup()
                    if savepath is not None:
                        # A switch nickname must not escape the requested backup directory.
                        if Path(name).name != name or name in {".", ".."}:
                            raise ValueError("Switch names used for backups must be filenames")
                        (savepath / f"{name}.cfg").write_bytes(backup)
                try:
                    switch.apply_vlan_config(cfg.get_vlan_membership(), cfg.get_pvids())
                except Exception:
                    if restore_on_failure:
                        click.echo(f"Restoring switch '{name}' from its backup ...", err=True)
                        switch.restore(backup)
                    raise
        except Exception as exc:
            raise click.ClickException(
                f"Applying switch '{name}' failed ({type(exc).__name__}); stopped."
            ) from None
    click.echo("All done!")
