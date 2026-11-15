#!/usr/bin/env python3
"""
Awning CLI

Command-line interface for controlling an awning device through Bond Bridge.
"""

import json
import sys
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from awning_controller import (
    BondAPIError,
    BondAwningController,
    ConfigurationError,
    create_controller_from_env,
)
import awning_override
from awning_override import OverrideError

console = Console()


def show_help() -> None:
    """Display beautiful help message."""
    console.print()
    console.print(
        Panel.fit(
            "[bold cyan]Awning Controller[/bold cyan]\n"
            "Control your awning device through Bond Bridge",
            border_style="cyan",
        )
    )
    console.print()

    # Commands table
    table = Table(
        show_header=True, header_style="bold magenta", border_style="blue", padding=(0, 2)
    )
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Description", style="white")

    table.add_row("☀️  open", "Open the awning")
    table.add_row("🌙 close", "Close the awning")
    table.add_row("✋ stop", "Stop awning movement")
    table.add_row("🔄 toggle", "Toggle between open and closed")
    table.add_row("📊 status", "Get current awning state")
    table.add_row("ℹ️  info", "Get device information")
    table.add_row("🔒 close/open --until T", "Hold it closed/open until T (5pm, 17:30, 3h, 90m), then follow the weather")
    table.add_row("🔒 close/open --forever", "Hold it closed/open until you run [cyan]auto[/cyan]")
    table.add_row("🌤️  auto", "Follow the weather and schedule again")
    table.add_row("📌 override", "Show the current hold")

    console.print(table)
    console.print()

    # Environment variables
    console.print(
        "[bold yellow]Environment Variables[/bold yellow] [dim](set in .env file)[/dim]"
    )
    console.print(
        "  [cyan]BOND_TOKEN[/cyan]  Bond Bridge authentication token [red](required)[/red]"
    )
    console.print(
        "  [cyan]BOND_HOST[/cyan]   Bond Bridge IP address [red](required)[/red] [dim](set up DHCP reservation)[/dim]"
    )
    console.print(
        "  [cyan]DEVICE_ID[/cyan]   Device ID for the awning [red](required)[/red]"
    )
    console.print()

    # Usage examples
    console.print("[bold green]Examples:[/bold green]")
    console.print("  [dim]$[/dim] awning open")
    console.print("  [dim]$[/dim] awning status")
    console.print("  [dim]$[/dim] nix run . -- close")
    console.print("  [dim]$[/dim] nix run . -- close --until 5pm")
    console.print("  [dim]$[/dim] nix run . -- open --forever")
    console.print()
    console.print(
        "[dim]--until/--forever are set on the Pi (it decides every 15 minutes), so they need SSH to it. "
        "A held [bold]open[/bold] still closes for wind or rain.[/dim]"
    )
    console.print()


def hold(mode: str, until: Optional[datetime]) -> None:
    """Hold the awning open or closed on the Pi, until `until` (None = until `auto`)."""
    icon = "🔒" if mode == "closed" else "🔓"
    ending = f"until {until.strftime('%a %-I:%M %p')}" if until else "until you run `auto`"
    console.print(f"{icon} [bold cyan]Holding the awning {mode} {ending}...[/bold cyan]")
    try:
        decided = awning_override.set_override(mode, until)
    except OverrideError as e:
        console.print(f"[bold red]✗ Error:[/bold red] {e}")
        sys.exit(1)
    if decided:
        console.print(f"[dim]{decided}[/dim]")
    console.print(f"[bold green]✓[/bold green] Held {mode} {ending}")


def release_hold() -> None:
    """Clear any hold: the weather decides again."""
    console.print("🌤️  [bold cyan]Handing the awning back to the weather...[/bold cyan]")
    try:
        decided = awning_override.set_override("auto")
    except OverrideError as e:
        console.print(f"[bold red]✗ Error:[/bold red] {e}")
        sys.exit(1)
    if decided:
        console.print(f"[dim]{decided}[/dim]")
    console.print("[bold green]✓[/bold green] Following the weather")


def show_hold() -> None:
    """Show the current hold."""
    try:
        console.print(f"📌 Override: {awning_override.show_override()}")
    except OverrideError as e:
        console.print(f"[bold red]✗ Error:[/bold red] {e}")
        sys.exit(1)


class AwningCLI:
    """CLI interface for awning controller."""

    def __init__(self, controller: BondAwningController):
        """
        Initialize CLI with controller.

        Args:
            controller: BondAwningController instance
        """
        self.controller = controller

    def cmd_open(self) -> None:
        """Execute open command."""
        console.print("☀️  [bold cyan]Opening awning...[/bold cyan]")
        try:
            self.controller.open()
            console.print("[bold green]✓[/bold green] Awning is opening")
        except BondAPIError as e:
            console.print(f"[bold red]✗ Error:[/bold red] {e}")
            sys.exit(1)

    def cmd_close(self) -> None:
        """Execute close command."""
        console.print("🌙 [bold cyan]Closing awning...[/bold cyan]")
        try:
            self.controller.close()
            console.print("[bold green]✓[/bold green] Awning is closing")
        except BondAPIError as e:
            console.print(f"[bold red]✗ Error:[/bold red] {e}")
            sys.exit(1)

    def cmd_stop(self) -> None:
        """Execute stop command."""
        console.print("✋ [bold yellow]Stopping awning...[/bold yellow]")
        try:
            self.controller.stop()
            console.print("[bold green]✓[/bold green] Awning stopped")
        except BondAPIError as e:
            console.print(f"[bold red]✗ Error:[/bold red] {e}")
            sys.exit(1)

    def cmd_toggle(self) -> None:
        """Execute toggle command."""
        console.print("🔄 [bold magenta]Toggling awning...[/bold magenta]")
        try:
            self.controller.toggle()
            console.print("[bold green]✓[/bold green] Awning toggled")
        except BondAPIError as e:
            console.print(f"[bold red]✗ Error:[/bold red] {e}")
            sys.exit(1)

    def cmd_status(self) -> None:
        """Execute status command."""
        try:
            state = self.controller.get_state()
            if state == 1:
                console.print("☀️  Awning is [bold green]OPEN[/bold green]")
            elif state == 0:
                console.print("🌙 Awning is [bold blue]CLOSED[/bold blue]")
            else:
                console.print(f"❓ Awning state: [yellow]{state}[/yellow]")
        except BondAPIError as e:
            console.print(f"[bold red]✗ Error:[/bold red] {e}")
            sys.exit(1)

    def cmd_info(self) -> None:
        """Execute info command."""
        try:
            info = self.controller.get_info()
            if not isinstance(info, dict):
                # A bridge answering with a list/string/null would otherwise die
                # with AttributeError on info.items().
                console.print(f"[bold red]✗ Error:[/bold red] Unexpected device info response: {info!r}")
                sys.exit(1)

            # Create a table for device information
            table = Table(show_header=False, box=None, padding=(0, 2))
            table.add_column("Property", style="cyan bold", no_wrap=True)
            table.add_column("Value", style="white")

            # Display common fields in a nice format
            field_map = {
                "name": "Name",
                "type": "Type",
                "location": "Location",
                "template": "Template",
                "addr": "Address",
                "freq": "Frequency",
                "actions": "Actions",
                "properties": "Properties",
                "commands": "Commands",
            }

            # Add known fields first
            for key, label in field_map.items():
                if key in info:
                    value = info[key]
                    # Format lists and dicts nicely
                    if isinstance(value, (list, dict)):
                        value = ", ".join(str(v) for v in value) if isinstance(value, list) else str(len(value)) + " items"
                    table.add_row(label, str(value))

            # Add any remaining fields
            for key, value in info.items():
                if key not in field_map:
                    # Format the key nicely (capitalize, replace underscores)
                    label = key.replace("_", " ").title()
                    if isinstance(value, (list, dict)):
                        value = ", ".join(str(v) for v in value) if isinstance(value, list) else str(len(value)) + " items"
                    table.add_row(label, str(value))

            console.print()
            console.print(Panel.fit(
                table,
                title="[bold cyan]ℹ️  Device Information[/bold cyan]",
                border_style="cyan"
            ))
            console.print()

        except BondAPIError as e:
            console.print(f"[bold red]✗ Error:[/bold red] {e}")
            sys.exit(1)


def parse_hold_flags(command: str, args: list[str]) -> tuple[Optional[str], bool]:
    """
    Read `--until T` / `--until=T` / `--forever` after the command.

    Returns (until_text, forever). Exits with a one-line error on anything else:
    only open and close take a flag, and `--until` and `--forever` exclude each other.
    """
    until_text: Optional[str] = None
    forever = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--forever":
            forever = True
        elif arg.startswith("--until="):
            until_text = arg.split("=", 1)[1]
        elif arg == "--until":
            if i + 1 >= len(args):
                console.print("[bold red]✗ Error:[/bold red] --until needs a time, like 5pm or 3h")
                sys.exit(1)
            i += 1
            until_text = args[i]
        else:
            console.print(f"[bold red]✗ Error:[/bold red] Invalid number of arguments (unexpected '{arg}')")
            console.print("  Run [cyan]awning --help[/cyan] for usage information")
            sys.exit(1)
        i += 1

    if (until_text is not None or forever) and command not in ("open", "close"):
        console.print("[bold red]✗ Error:[/bold red] --until and --forever only go with open or close")
        sys.exit(1)
    if until_text is not None and forever:
        console.print("[bold red]✗ Error:[/bold red] Use --until or --forever, not both")
        sys.exit(1)
    return until_text, forever


def main() -> None:
    """Main entry point for the awning control script."""
    # Check for help flag or no arguments
    if len(sys.argv) == 1 or (
        len(sys.argv) == 2 and sys.argv[1] in ["-h", "--help", "help"]
    ):
        show_help()
        sys.exit(0)

    command = sys.argv[1]
    valid_commands = ["open", "close", "stop", "toggle", "status", "info", "auto", "override"]

    if command not in valid_commands:
        console.print(f"[bold red]✗ Error:[/bold red] Unknown command '{command}'")
        console.print(f"  Valid commands: {', '.join(valid_commands)}")
        console.print("  Run [cyan]awning --help[/cyan] for usage information")
        sys.exit(1)

    until_text, forever = parse_hold_flags(command, sys.argv[2:])

    # A hold, `auto` and `override` are set on the Pi over SSH and never touch the
    # Bond Bridge from here.
    hold_until: Optional[datetime] = None
    if until_text is not None:
        try:
            hold_until = awning_override.parse_until(until_text)
        except OverrideError as e:
            console.print(f"[bold red]✗ Error:[/bold red] {e}")
            sys.exit(1)
    pi_action = None
    if until_text is not None or forever:
        pi_action = lambda: hold("closed" if command == "close" else "open", hold_until)
    elif command == "auto":
        pi_action = release_hold
    elif command == "override":
        pi_action = show_hold
    if pi_action:
        try:
            pi_action()
        except Exception as e:
            console.print(f"[bold red]✗ Unexpected error:[/bold red] {type(e).__name__}: {e}")
            sys.exit(1)
        return

    # Load configuration and create controller
    try:
        controller = create_controller_from_env()
    except ConfigurationError as e:
        console.print(f"[bold red]✗ Error:[/bold red] {e}")
        sys.exit(1)

    # Create CLI and execute command
    cli = AwningCLI(controller)
    command_map = {
        "open": cli.cmd_open,
        "close": cli.cmd_close,
        "stop": cli.cmd_stop,
        "toggle": cli.cmd_toggle,
        "status": cli.cmd_status,
        "info": cli.cmd_info,
    }

    # Any failure the commands do not handle themselves (a malformed reply, a bug)
    # becomes a one-line error instead of a raw traceback. SystemExit passes through.
    try:
        command_map[command]()
    except Exception as e:
        console.print(f"[bold red]✗ Unexpected error:[/bold red] {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
