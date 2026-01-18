# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Reference

```bash
# CLI commands
nix run . -- open|close|stop|toggle|status|info

# Weather automation
nix run .#automation -- --dry-run
nix run .#automation -- --env-file=/path/to/.env

# Development shell
nix develop
python3 awning.py open
python3 awning_automation.py --dry-run

# Deploy to Orange Pi
./deploy.sh
```

## Project Overview

Bond Bridge awning controller - sends HTTP commands to control a motorized awning via the Bond Local API v2. Written in Python 3 with Nix flakes for reproducible dependency management.

## Architecture

**Core Domain (`awning_controller.py`):**
- `BondAwningController` class - Core business logic for awning control
- `create_controller_from_env()` - Factory function to create controller from env
- Custom exceptions: `ConfigurationError`, `BondAPIError`
- No dependencies on CLI libraries - can be used independently
- All methods raise exceptions instead of printing/exiting

**CLI Interface (`awning.py`):**
- `AwningCLI` class - Handles user-facing command execution
- Uses `rich` library for colorful output
- Catches exceptions from controller and displays user-friendly error messages

**Weather Automation (`awning_automation.py`):**
- Automatically opens/closes awning based on weather and sun conditions
- Uses Open-Meteo API (free, no API key) for weather data
- Uses pvlib for solar position calculations (NREL SPA algorithm)
- Imports `awning_controller` for awning control

**Configuration Loading:**
- `.env` file loaded from current working directory first, then script directory
- This allows `nix run . -- <command>` to work correctly from the project directory

**Command Flow:**
1. CLI parses arguments (simple manual parsing, no argparse)
2. Create controller via `create_controller_from_env()`
3. CLI calls controller method
4. Controller sends HTTP request to Bond API
5. CLI catches exceptions and displays formatted output

**Bond API:**
- Base URL: `http://{BOND_HOST}/v2/devices/{DEVICE_ID}`
- Authentication via `BOND-Token` header
- Actions: `Open`, `Close`, `Stop`, `ToggleOpen`
- State endpoint returns `{"open": 1}` or `{"open": 0}`

## Weather Automation

**Decision Logic (ALL 7 conditions must be met to open):**
1. **Clear sky**: Cloud cover <= `MAX_CLOUD_COVER_PERCENT`
2. **Calm**: Wind speed < `WIND_SPEED_THRESHOLD_MPH`
3. **No rain**: Precipitation = 0 mm/h
4. **Above freezing**: Temperature > 32°F
5. **Daytime**: Between sunrise and sunset
6. **Sun high enough**: Altitude >= `MIN_SUN_ALTITUDE_DEG`
7. **Sun facing SE**: Azimuth 90°-180° (hardcoded for SE window)

If ANY condition fails, the awning closes. Fail-safe: closes awning if weather API is unavailable.

**Logging:**
- Daily log rotation in `logs/` directory as `awning-YYYY-MM-DD.log`
- Symlink at `~/awning.log` always points to today's log
- Auto-cleanup after 30 days (configurable via `LOG_RETENTION_DAYS`)
- View logs: `tail -f ~/awning.log`

## Deployment

**Target:** Orange Pi 3 LTS running Debian (`karlhepler@orangepi3-lts`)

**Deploy script (`deploy.sh`):**
1. Discovers Bond Bridge IP via mDNS (using `BOND_ID` from `.env`)
2. Sends Telegram notification (deploy start)
3. Creates Python venv on remote if needed
4. Installs dependencies via pip
5. Copies scripts and `.env` to `~/.config/awning/`
6. Configures cron job (every 15 minutes)
7. Runs dry-run verification
8. Sends Telegram notification (deploy complete)

**Remote structure:**
- Scripts: `~/.config/awning/awning_automation.py`, `awning_controller.py`
- Config: `~/.config/awning/.env`
- Venv: `~/.config/awning/venv/`
- Logs: `~/.config/awning/logs/awning-YYYY-MM-DD.log`
- Symlink: `~/awning.log` -> today's log file

## Environment Variables

See `.env.example` for full documentation. Key variables:

**Required for CLI:**
- `BOND_TOKEN` - Bond Bridge auth token
- `BOND_HOST` - Bond Bridge IP address (set up DHCP reservation)
- `DEVICE_ID` - Device ID for the awning

**Required for automation:**
- `LATITUDE`, `LONGITUDE` - Location for weather/sun calculations
- `MAX_CLOUD_COVER_PERCENT`, `MIN_SUN_ALTITUDE_DEG`, `WIND_SPEED_THRESHOLD_MPH`

**Optional:**
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` - For notifications
- `LOG_RETENTION_DAYS` - Days to keep logs (default: 30)
- `BOND_ID` - For mDNS discovery in deploy.sh

## UI/UX Guidelines

- Emojis inline with text (not in separate table columns - avoids alignment issues)
- Color scheme: cyan (actions), green (success), red (errors), yellow (warnings)
- Controller returns raw data, CLI formats it for display
