# Awning Control

Control a motorized awning via Bond Bridge, with optional weather-based automation.

## Requirements

- [Nix](https://nixos.org/download.html) with flakes enabled
- Bond Bridge on local network

## Setup

1. Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

2. Edit `.env` with your Bond Bridge credentials:

```bash
BOND_TOKEN=your_token_here
BOND_HOST=192.168.1.XXX  # Your Bond Bridge IP (set up DHCP reservation!)
DEVICE_ID=your_device_id_here
```

## Usage

```bash
nix run . -- <command>
```

### Commands

- `open` - Open the awning
- `close` - Close the awning
- `stop` - Stop awning movement
- `toggle` - Toggle between open and closed
- `status` - Get current awning state
- `info` - Get device information
- `help` - Show help message

### Examples

```bash
nix run . -- open
nix run . -- close
nix run . -- status
```

## Environment Variables

Set these in the `.env` file:

- `BOND_TOKEN` - Bond Bridge authentication token (required)
- `BOND_HOST` - Bond Bridge IP address (required) - see below for setup
- `DEVICE_ID` - Device ID for the awning (required)

**Important:** Set up a DHCP reservation in your router for the Bond Bridge. This ensures it always gets the same IP address. This is best practice for all IoT devices.

## Getting Your Credentials

### Bond Token
1. Open the Bond Home app
2. Go to Settings → Advanced Settings
3. Copy the token

### Bond Host (IP Address)
1. Set up a DHCP reservation in your router (recommended)
2. Or find the current IP in your router's connected devices list
3. You can also find it in Bond Home app → Settings → Device Info

### Device ID
1. Open the Bond Home app
2. Select your awning device
3. Go to Settings → Advanced
4. Copy the Device ID

## Weather Automation

Automatically opens and closes the awning from the weather. Every 15 minutes (cron) it looks at the conditions and acts immediately; nothing is debounced. See `.env.example` for every setting.

```bash
# Run automation
nix run .#automation

# Dry-run (test without controlling awning)
nix run .#automation -- --dry-run
```

The automation opens the awning only when ALL seven conditions are met:

1. **Sunny**: a three-layer check of the forecast (solar radiation, UV and direct sun; cloud cover; a hard overcast ceiling), with a second-opinion rescue from two other models when the primary feed looks broken, and a close-only veto when the nearest airport reports a low broken or overcast cloud deck
2. **Calm**: average wind AND gusts below their limits, using the worse of the forecast and the airport's latest report
3. **No rain**: current or recent precipitation, rain probability, weather code, and live radar
4. **Above the minimum temperature** (default 45°F)
5. **Daytime**: between sunrise and sunset
6. **Sun high enough** above the horizon (clears trees and rooftops)
7. **Sun facing the window**: the sun's compass direction is inside the arc you configure

If any condition fails the awning closes. If the automation cannot get a trustworthy answer (weather service down, or an unexpected error before a command is sent) it closes the awning as a fail-safe. Data comes from [Open-Meteo](https://open-meteo.com) (forecast), [RainViewer](https://www.rainviewer.com) (radar) and [aviationweather.gov](https://aviationweather.gov) (airport observations, optional). Details, thresholds and the incidents behind each rule are in `CLAUDE.md`.

### Deploying to a Raspberry Pi / Orange Pi

`./deploy.sh` copies the scripts and your `.env` to the device and installs the cron job. It needs your interactive password, so you run it yourself.

### Tests

```bash
nix develop -c python3 -m unittest test_awning_automation test_awning_controller
```

The tests never touch the network.

## Development

```bash
# Enter development shell
nix develop

# Run directly
python3 awning.py open
python3 awning_automation.py --dry-run
```

## API Documentation

This uses the Bond Local API v2. See the [Bond API documentation](https://github.com/bondhome/api-v2).
