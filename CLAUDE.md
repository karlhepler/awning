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
- Bond Bridge IP specified via `BOND_HOST` environment variable (requires DHCP reservation for stability)
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

## Weather Automation

**Automation Script (`awning_automation.py`):**
- Automatically opens/closes awning based on comprehensive weather and sun conditions
- Designed to run as a cron job or Kubernetes scheduled job
- Uses Open-Meteo API (free, no API key required) for weather data
- Uses pvlib for solar position calculations
- Imports `awning_controller` for awning control

**Enhanced Decision Logic (ALL 8 conditions must be met):**
1. **Sunny**: Shortwave radiation >= threshold (configurable, default 200 W/m²)
2. **Clear sky**: Cloud cover <= threshold (configurable, e.g., 5%)
3. **Calm**: Wind speed < threshold (configurable, default 10 mph)
4. **No rain**: Precipitation = 0 mm/h (hardcoded)
5. **Above freezing**: Temperature > 32°F (hardcoded)
6. **Daytime**: Current time between sunrise and sunset (from weather API)
7. **Sun high enough**: Sun altitude >= threshold (configurable, default 10°, accounts for trees)
8. **Sun facing SE**: Sun azimuth 90°-180° (East to South, hardcoded for SE window)

- **Opens awning if**: ALL 8 conditions are True
- **Closes awning if**: ANY condition is False
- Checks current awning state before acting (only sends command if state needs to change)
- Fail-safe: Closes awning if weather API is unavailable

**Configuration (add to .env):**
- `LATITUDE` - Latitude for weather location (required, e.g., 37.7749)
- `LONGITUDE` - Longitude for weather location (required, e.g., -122.4194)
- `SHORTWAVE_RADIATION_THRESHOLD` - Minimum solar radiation in W/m² (required, e.g., 120)
- `MAX_CLOUD_COVER_PERCENT` - Maximum cloud cover percentage for "clear sky" (required, e.g., 5)
- `MIN_SUN_ALTITUDE_DEG` - Minimum sun altitude in degrees (required, e.g., 10)
- `WIND_SPEED_THRESHOLD_MPH` - Wind speed threshold in mph (required, e.g., 10)

**Running automation:**
```bash
# Via Nix (recommended)
nix run .#automation

# Dry-run mode (test without controlling awning)
nix run .#automation -- --dry-run

# Specify .env file location (useful for cron jobs)
nix run .#automation -- --env-file=/path/to/awning/.env

# Both flags together
nix run .#automation -- --env-file=/path/to/awning/.env --dry-run

# Development shell
nix develop
python3 awning_automation.py
python3 awning_automation.py --dry-run
python3 awning_automation.py --env-file=/path/to/.env
```

**Example cron job:**
```bash
# Create logs directory first: mkdir -p ~/logs

# Run every 15 minutes with explicit .env path (recommended for cron)
*/15 * * * * nix run /Users/YOUR_USERNAME/path/to/awning#automation -- --env-file=/Users/YOUR_USERNAME/path/to/awning/.env >> ~/logs/awning-automation.log 2>&1

# Alternative: Change to project directory first (auto-finds .env)
*/15 * * * * cd /Users/YOUR_USERNAME/path/to/awning && nix run .#automation >> ~/logs/awning-automation.log 2>&1

# Note: No need to restrict cron to daylight hours - the automation
# checks sunrise/sunset internally and will only act during daytime
```

**Weather API:**
- Uses Open-Meteo Forecast API: `https://api.open-meteo.com/v1/forecast`
- Fetches current: shortwave radiation (W/m²), cloud cover (%), wind speed (mph), precipitation (mm/h), temperature, is_day
- Fetches daily: sunrise and sunset times
- 10-second timeout on requests
- No API key required for non-commercial use

**Solar Position:**
- Uses pvlib library with NREL SPA algorithm
- Calculates sun azimuth and altitude for current location and time
- Azimuth convention: 0°=North, 90°=East, 180°=South, 270°=West
- Southeast window requires azimuth 90°-180°

**Error Handling:**
- Weather API failures: Closes awning as fail-safe
- Awning API failures: Logs error and exits
- Missing environment variables: Clear error messages
- All actions logged to stdout (redirect to file in cron)

**Logging:**
- INFO level logs to stdout
- Format: `YYYY-MM-DD HH:MM:SS - LEVEL - Message`
- Logs all conditions with ✓/✗ symbols
- Shows: weather, sun position, sunrise/sunset, decision rationale, current state, and actions taken

## Deployment

**Target:** Orange Pi 3 LTS running Debian (user: `karlhepler@orangepi3-lts`)

**Deploy script (`deploy.sh`):**
```bash
./deploy.sh
```

This script:
1. Discovers Bond Bridge IP via mDNS (using `BOND_ID` from `.env`)
2. Updates `BOND_HOST` in local `.env` if discovered
3. Sends Telegram notification (deploy start)
4. Creates Python venv on remote if needed
5. Installs dependencies via pip
6. Copies scripts and `.env` to `~/.config/awning/`
7. Configures cron job (every 15 minutes)
8. Runs dry-run verification
9. Sends Telegram notification (deploy complete with version SHA)

**Remote structure:**
- Scripts: `~/.config/awning/awning_automation.py`, `awning_controller.py`
- Config: `~/.config/awning/.env`
- Venv: `~/.config/awning/venv/`
- Logs: `~/awning.log`

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
- Python 3 with: `requests`, `python-dotenv`, `rich`, `pvlib`, `pandas`, `pytz`
- Managed via Nix flake (see `flake.nix`)
- `pvlib` and `pandas` are used for solar position calculations in automation

**Environment setup (.env file):**

*For basic awning control:*
- `BOND_TOKEN` - Bond Bridge auth token (required) - get from Bond Home app → Settings
- `BOND_HOST` - Bond Bridge IP address (required) - set up DHCP reservation in your router
- `DEVICE_ID` - Device ID for the awning (required)

*For weather automation (in addition to above):*
- `LATITUDE` - Latitude for weather location (required, e.g., 37.7749)
- `LONGITUDE` - Longitude for weather location (required, e.g., -122.4194)
- `SHORTWAVE_RADIATION_THRESHOLD` - Minimum solar radiation in W/m² (required, e.g., 120)
- `MAX_CLOUD_COVER_PERCENT` - Maximum cloud cover percentage for "clear sky" (required, e.g., 5)
- `MIN_SUN_ALTITUDE_DEG` - Minimum sun altitude in degrees (required, e.g., 10)
- `WIND_SPEED_THRESHOLD_MPH` - Wind speed threshold in mph (required, e.g., 10)

*For Telegram notifications (optional):*
- `TELEGRAM_BOT_TOKEN` - Bot token from @BotFather on Telegram
- `TELEGRAM_CHAT_ID` - Chat ID to send notifications to (get from @userinfobot)
- Notifications are sent when awning opens/closes or on errors
- Auto-enabled when both variables are set

**Using the controller independently (without CLI):**
```python
from awning_controller import BondAwningController, create_controller_from_env

# Option 1: Create from environment variables
controller = create_controller_from_env()

# Option 2: Create manually (with IP address)
controller = BondAwningController(
    bond_host="192.168.1.100",
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
