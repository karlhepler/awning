# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bond Bridge awning controller - sends HTTP commands to control a motorized awning via the Bond Local API v2. Written in Python 3 with Nix flakes for reproducible dependency management.

## Architecture

The project is organized into two main modules:

**Core Domain (`awning_controller.py`):**
- `BondAwningController` class - Core business logic for awning control
- `load_config()` - Loads configuration from environment variables
- `create_controller_from_env()` - Factory function to create controller from env
- `auto_discover_bond()` - Converts Bond ID to mDNS hostname
- Custom exceptions: `ConfigurationError`, `BondAPIError`
- No dependencies on CLI libraries (rich, etc.) - can be used independently
- All methods raise exceptions instead of printing/exiting

**CLI Interface (`awning.py`):**
- `AwningCLI` class - Handles user-facing command execution
- `show_help()` - Displays rich-formatted help
- `main()` - Entry point with argument parsing
- Catches exceptions from controller and displays user-friendly error messages
- Uses `rich` library for colorful output

**Configuration Loading:**
- `.env` file is loaded from current working directory first, then falls back to script directory
- This allows `nix run . -- <command>` to work correctly from the project directory
- Auto-discovery of Bond Bridge via mDNS using `BOND_ID` (converts to `{bond_id}.local` hostname)
- Configuration errors raise `ConfigurationError` instead of calling `sys.exit()`

**Command Flow:**
1. CLI parses command-line arguments (simple manual parsing, no argparse)
2. Create controller via `create_controller_from_env()` (handles config loading)
3. CLI calls controller method (open/close/stop/toggle/get_state/get_info)
4. Controller sends HTTP request to Bond API
5. CLI catches exceptions and displays formatted output

**Bond API:**
- Base URL: `http://{BOND_HOST}/v2/devices/{DEVICE_ID}`
- Authentication via `BOND-Token` header
- Actions: `Open`, `Close`, `Stop`, `ToggleOpen`
- State endpoint returns `{"open": 1}` or `{"open": 0}`

## Development

**Running commands:**
```bash
# Via Nix (recommended)
nix run . -- open
nix run . -- status

# Development shell
nix develop
python3 awning.py open

# Build and install
nix build
./result/bin/awning status
```

**Available commands:** `open`, `close`, `stop`, `toggle`, `status`, `info`

**Dependencies:**
- Python 3 with: `requests`, `python-dotenv`, `rich`
- Managed via Nix flake (see `flake.nix`)

**Environment setup (.env file):**
- `BOND_TOKEN` - Bond Bridge auth token (required) - get from Bond Home app → Settings
- `BOND_ID` - Bond ID (e.g., ZZIF27980) for mDNS auto-discovery (recommended)
- `BOND_HOST` - Bond hostname/IP (alternative to BOND_ID, less resilient to DHCP changes)
- `DEVICE_ID` - Device ID for the awning (required)

**Using the controller independently (without CLI):**
```python
from awning_controller import BondAwningController, create_controller_from_env

# Option 1: Create from environment variables
controller = create_controller_from_env()

# Option 2: Create manually
controller = BondAwningController(
    bond_host="bond-abc123.local",
    bond_token="your_token",
    device_id="device_id_here"
)

# Use the controller
controller.open()
state = controller.get_state()  # Returns 1 (open) or 0 (closed)
info = controller.get_info()    # Returns dict with device info
```

## UI/UX Guidelines

The CLI uses `rich` for colorful, human-friendly output:
- Emojis inline with text (not in separate table columns - avoids alignment issues)
- Color scheme: cyan (actions), green (success), red (errors), yellow (warnings)
- Custom `show_help()` function (not argparse) displays table of commands
- Error handling: catch exceptions in CLI, display formatted messages, exit with code 1
- **Info command:** Controller returns raw JSON dict, CLI formats it as human-readable table
  - Common fields (name, type, location, etc.) shown first with friendly labels
  - Lists displayed as comma-separated values
  - Dicts show item count
  - Unknown fields auto-formatted with title-cased labels

## Security

- Never log or print `BOND_TOKEN`
- HTTP requests have 10-second timeouts
- All environment variables are stripped of whitespace
- Input validation on commands (limited to predefined set)
