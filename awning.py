#!/usr/bin/env python3
"""
Awning CLI

Command-line interface for controlling an awning device through Bond Bridge.
"""

import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from awning_controller import (
    BondAPIError,
    BondAwningController,
    ConfigurationError,
    create_controller_from_env,
)

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
        "  [cyan]BOND_HOST[/cyan]   Bond Bridge hostname or IP [dim](e.g., bond-zzif27980.local)[/dim]"
    )
    console.print(
        "  [cyan]BOND_ID[/cyan]     Bond ID for auto-discovery [dim](e.g., ZZIF27980)[/dim]"
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
    console.print()


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
            console.print(
                Panel.fit(
                    json.dumps(info, indent=2),
                    title="[bold cyan]Device Information[/bold cyan]",
                    border_style="cyan",
                )
            )
        except BondAPIError as e:
            console.print(f"[bold red]✗ Error:[/bold red] {e}")
            sys.exit(1)


def main() -> None:
    """Main entry point for the awning control script."""
    # Check for help flag or no arguments
    if len(sys.argv) == 1 or (
        len(sys.argv) == 2 and sys.argv[1] in ["-h", "--help", "help"]
    ):
        show_help()
        sys.exit(0)

    # Simple argument parsing
    if len(sys.argv) != 2:
        console.print("[bold red]✗ Error:[/bold red] Invalid number of arguments")
        console.print("  Run [cyan]awning --help[/cyan] for usage information")
        sys.exit(1)

    command = sys.argv[1]
    valid_commands = ["open", "close", "stop", "toggle", "status", "info"]

    if command not in valid_commands:
        console.print(f"[bold red]✗ Error:[/bold red] Unknown command '{command}'")
        console.print(f"  Valid commands: {', '.join(valid_commands)}")
        console.print("  Run [cyan]awning --help[/cyan] for usage information")
        sys.exit(1)

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

    command_map[command]()


if __name__ == "__main__":
    main()
