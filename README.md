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

Automatically open/close awning based on weather conditions. See `.env.example` for configuration options.

```bash
# Run automation
nix run .#automation

# Dry-run (test without controlling awning)
nix run .#automation -- --dry-run
```

The automation opens the awning only when ALL conditions are met:
1. Sunny (cloud cover below threshold)
2. Calm (wind speed below threshold)
3. No rain
4. Daytime (between sunrise and sunset)
5. Sun facing SE (azimuth 90°-180°)

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
