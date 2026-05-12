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

# Deploy to Orange Pi (USER ONLY — requires interactive password)
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
1. **Sunny**: multi-layer model — ALL three layers must be true:
   - **Model layer**: GHI >= `MIN_GHI_WM2` (default 400 W/m²) OR UV >= `MIN_UV_INDEX` (default 4.0)
   - **Consistency layer**: DNI >= `MIN_DNI_WM2` (default 50 W/m²) OR total cloud cover < `MAX_CLOUD_COVER_PCT` (default 80%)
   - **Overcast ceiling**: max(cloud_cover_mid, cloud_cover_high) < threshold (default 95%) OR DNI >= `MIN_DNI_CIRRUS_WM2` (default 30 W/m²). The DNI guard bypasses the ceiling when direct irradiance proves the sun is reaching the ground — added after the 2026-05-12 incident where Open-Meteo's `cloud_cover_high` field hallucinated 100% on a clear day with DNI=905 W/m².
2. **Calm**: Wind speed < `WIND_SPEED_THRESHOLD_MPH` (default 15.0 mph)
3. **No rain**: Precipitation = 0 mm/h
4. **Above minimum temperature**: Temperature > `MIN_TEMPERATURE_F` (default 45°F; was 60°F prior to commit `24ebd12`)
5. **Daytime**: Between sunrise and sunset
6. **Sun high enough**: Altitude >= `MIN_SUN_ALTITUDE_DEG` (default 15°)
7. **Sun facing window**: Azimuth between 90° and 260° (hardcoded; SE-through-SW arc)

If ANY condition fails, the awning closes. Fail-safe: closes awning if weather API is unavailable.

**Logging:**
- Daily log rotation in `logs/` directory as `awning-YYYY-MM-DD.log`
- Symlink at `~/awning.log` always points to today's log
- Auto-cleanup after 30 days (configurable via `LOG_RETENTION_DAYS`)
- View logs: `tail -f ~/awning.log`

## Deployment

**🚨 The user runs `./deploy.sh` — Claude must NEVER run it.** Deployment requires interactive sudo/password input on the remote Orange Pi that only the user can provide. Claude's responsibility ends at `git commit` + `git push`; the user handles the actual deploy from their own terminal. Attempting to invoke `deploy.sh` (directly, via `bash -x`, via a sub-agent, via SSH, etc.) WILL fail because Claude cannot supply the password, and the attempt wastes tool budget plus produces misleading error output. When code is pushed and ready, tell the user "ready to deploy" and stop — wait for them to run it.

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
- `WIND_SPEED_THRESHOLD_MPH` - Max wind speed (mph) to open awning; no default, must be set
- `MIN_SUN_ALTITUDE_DEG` - Min sun altitude (degrees above horizon); no default, must be set

**Optional for automation (have defaults):**
- `MIN_GHI_WM2` - Min global horizontal irradiance W/m² for Layer 1 sunny gate (default: 400)
- `MIN_UV_INDEX` - Min UV Index for Layer 1 sunny gate (default: 4)
- `MIN_DNI_WM2` - Min direct normal irradiance W/m² for Layer 2 consistency check (default: 50)
- `MAX_CLOUD_COVER_PCT` - Max total cloud cover % for Layer 2 consistency check (default: 80)
- `MIN_TEMPERATURE_F` - Min temperature °F to open awning (default: 45)
- `OVERCAST_THRESHOLD_PCT` - Layer 3 hard ceiling: max(cloud_cover_mid, cloud_cover_high) must be below this % (default: 95)
- `MIN_DNI_CIRRUS_WM2` - Layer 3 DNI guard: bypasses overcast ceiling when DNI >= this W/m² (default: 30)

**Optional:**
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` - For notifications
- `LOG_RETENTION_DAYS` - Days to keep logs (default: 30)
- `BOND_ID` - For mDNS discovery in deploy.sh

## UI/UX Guidelines

- Emojis inline with text (not in separate table columns - avoids alignment issues)
- Color scheme: cyan (actions), green (success), red (errors), yellow (warnings)
- Controller returns raw data, CLI formats it for display
