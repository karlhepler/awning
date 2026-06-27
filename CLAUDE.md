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

## 🚨 Git Workflow — Commit Directly to Main (NO branches, NO PRs)

**This repo commits DIRECTLY to `main`. Always. No exceptions, ever.**

- ✅ Commit straight to `main` and push to `origin/main`.
- ❌ NEVER create a branch. ❌ NEVER open a pull request. ❌ NEVER use the `karlhepler/` branch-naming convention here.
- This overrides any global guidance about draft PRs, branch prefixes, or PR descriptions — none of that applies to this repository.
- Workflow for every change: commit to `main` → `git push` → tell the user "ready to deploy" (the user runs `./deploy.sh`).

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
- Cross-checks live RainViewer NEXRAD radar (free, no API key) as an independent rain signal; decodes radar tiles with Pillow (PIL). The PIL import is lazy and fails open, so a missing Pillow never crashes the automation — radar simply disables and the Open-Meteo signals continue to guard.
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
3. **No rain (multi-signal gate)**: the awning closes only on **observed wetness** or a forecast signal that is not vetoed by provably-clear sky conditions. Signals are divided into two categories:

   **Observed signals** (close on any positive reading; never suppressed by any veto):
   - Open-Meteo `precipitation` > 0 mm — current-slot actual precipitation
   - Open-Meteo `minutely_15` precipitation in the last ~30 min > 0 — recent actual precipitation lookback
   - **RainViewer NEXRAD radar** shows precipitation over the configured `LATITUDE`/`LONGITUDE`. This is a live radar observation, independent of the Open-Meteo forecast model, so it catches storms the hourly model has not yet ingested. The radar check fails open: any fetch/parse error (or a missing Pillow dependency) returns "no radar rain" so it can never wedge the awning closed. Added after the 2026-06-23 incident, where Open-Meteo reported `precipitation=0` and `DNI=486 W/m²` (full sun) during a confirmed downpour and the single-field `precipitation == 0` gate let the awning open. A single radar tile is sampled (no adjacent-tile lookup); see the in-code note for the tile-boundary caveat.
     - **Clear-sky radar veto:** the radar signal is ignored when `DNI >= RADAR_VETO_DNI_WM2` (default 650 W/m²) AND `max(cloud_cover_low, cloud_cover_mid) < RADAR_VETO_CLOUD_PCT` (default 15%). The cloud condition looks ONLY at the rain-bearing low/mid layers, NOT total cloud cover — thin high cirrus does not produce rain but can push total cloud cover well above the ceiling while full sun still reaches the ground. NEXRAD operates in clear-air mode on hot, dry days and renders non-precipitation echoes (insect/bird biological scatter, ground clutter, anomalous propagation) as faint pixels; the decode (`is_raining = alpha > 0`) cannot distinguish those from real rain, so a lone clutter pixel was vetoing all four honest dry signals. The veto suppresses ONLY the radar arm and ONLY when independent measurements prove the sky is clear — real rain falls from low/mid clouds (which lift `max(cloud_cover_low, cloud_cover_mid)` above the ceiling) and collapses DNI, so BOTH veto conditions fail during genuine rain and it cannot engage. The 650 W/m² DNI default sits well above the 486 W/m² reading from the 2026-06-23 incident for margin. Each veto is logged. Added after the 2026-06-24 clutter incident (radar pixel RGBA 158,147,117,110 closed the awning on a clear 80°F afternoon, DNI=790 W/m², 3% cloud); the rain-bearing-layer cloud condition was added later the same day after thin high cirrus (total cloud 22–52%, but low 2%/mid 0%, DNI 888 W/m²) defeated the original total-cloud-cover veto and re-closed the awning.

   **Forecast signals** (subject to the provably-clear forecast veto):
   - Open-Meteo `precipitation_probability` (current hour) >= `RAIN_PROBABILITY_THRESHOLD` (default 20%)
   - Open-Meteo `weather_code` is a drizzle/rain/snow/shower/thunderstorm WMO code
     - **Provably-clear forecast veto:** when actual `precipitation == 0` AND `max(cloud_cover_low, cloud_cover_mid) < RADAR_VETO_CLOUD_PCT` (default 15%), forecast signals are suppressed — rain cannot fall from air with no rain-bearing clouds regardless of what the NWP model predicts. Only observed wetness (actual precip, minutely_15 rain, or radar-confirmed precipitation not vetoed by the clear-sky radar veto) closes the gate in this state. Real rain always produces elevated low/mid cloud cover, so this veto cannot engage during genuine precipitation. Added after the 2026-06-27 incident where a forecast signal (precipitation_probability or weather_code) closed the awning at 11:00 while cloud_low=10%, cloud_mid=2%, precipitation=0.0 mm (bone dry, clear sky).

   A missing/null signal is treated as rain (bias toward closed). Each cron run logs a `Rain signals:` diagnostic line recording every signal's value and whether the forecast veto engaged. When the gate closes, the `Decision:` log message names the exact signal(s) that fired and their values — ending the whack-a-mole pattern where `Raining (0.0 mm/h)` provided no attribution.
4. **Above minimum temperature**: Temperature > `MIN_TEMPERATURE_F` (default 45°F; was 60°F prior to commit `24ebd12`)
5. **Daytime**: Between sunrise and sunset
6. **Sun high enough**: Altitude >= `MIN_SUN_ALTITUDE_DEG` (default 15°)
7. **Sun facing window**: Azimuth between 90° and 260° (hardcoded; SE-through-SW arc)

If ANY condition fails, the awning closes. Fail-safe: closes awning if weather API is unavailable.

**Each cron run acts immediately on the current conditions** — all conditions met opens the awning, any condition failing closes it, with no debounce or vote-counting between runs. (An earlier anti-flapping hysteresis that required two consecutive "open" votes was removed on 2026-06-24: Open-Meteo's irradiance/cloud data is hourly so it does not jitter between 15-min runs, rain-driven close is already immediate, and the RainViewer clear-sky veto removed the main flap source — so the debounce only added a ~30-minute open lag.)

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
4. Installs dependencies via pip — **`deploy.sh` carries its own hardcoded package list** (`requests python-dotenv rich pvlib pandas pytz tenacity Pillow`); it does NOT read `requirements.txt`. 🚨 When adding a new runtime dependency you MUST add it to BOTH `requirements.txt` (for local/Nix dev) AND the pip-install line in `deploy.sh` (for the Pi), or the deploy will crash on import.
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
- `RAIN_PROBABILITY_THRESHOLD` - Min Open-Meteo precipitation-probability % (current hour) that closes the rain gate (default: 20)
- `RADAR_VETO_DNI_WM2` - Clear-sky veto: ignore the RainViewer radar signal when DNI >= this W/m² AND cloud cover is below `RADAR_VETO_CLOUD_PCT` (default: 650)
- `RADAR_VETO_CLOUD_PCT` - Rain-bearing (max of low/mid layer) cloud cover % ceiling shared by both the radar veto and the forecast veto; high cirrus is excluded. Radar veto: suppresses a false radar rain signal when ALSO DNI >= `RADAR_VETO_DNI_WM2`. Forecast veto: suppresses forecast-only signals (probability, weather_code) when precipitation==0 (no DNI condition required) (default: 15)

**Optional:**
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` - For notifications
- `LOG_RETENTION_DAYS` - Days to keep logs (default: 30)
- `BOND_ID` - For mDNS discovery in deploy.sh

## UI/UX Guidelines

- Emojis inline with text (not in separate table columns - avoids alignment issues)
- Color scheme: cyan (actions), green (success), red (errors), yellow (warnings)
- Controller returns raw data, CLI formats it for display
