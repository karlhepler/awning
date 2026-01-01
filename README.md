# Awning Control

A simple shell script to control a motorized awning via Bond Bridge.

## Setup

1. Create `.env` with your values:

```bash
BOND_TOKEN=your_token_here
BOND_ID=ZZIF27980           # Recommended: auto-discovers hostname
# BOND_HOST=bond-zzif27980.local  # Alternative: use hostname directly
# BOND_HOST=192.168.1.246         # Or use IP (less reliable)
DEVICE_ID=4fdecc733fbbaa4e
```

2. Make the script executable (already done):

```bash
chmod +x awning
```

## Usage

```bash
./awning <command>
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
./awning open
./awning close
./awning status
```

## Environment Variables

Set these in the `.env` file:

- `BOND_TOKEN` - Bond Bridge authentication token (required)
- `BOND_ID` - Bond ID (e.g., ZZIF27980) for auto-discovery via mDNS (recommended)
- `BOND_HOST` - Bond Bridge hostname or IP address (alternative to BOND_ID)
- `DEVICE_ID` - Device ID for the awning (required)

**Note:** Using `BOND_ID` is recommended because it's resilient to IP address changes from your router's DHCP. The script will automatically resolve it to the hostname using mDNS.

## Getting Your Token

1. Open the Bond Home app on your phone
2. Go to your Bond's "Settings" screen
3. Tap the token to copy it to clipboard

## Requirements

- `curl` - For HTTP requests
- `jq` - For JSON parsing
- Bond Bridge on local network

## API Documentation

This script uses the Bond Local API v2. For more information, see the [Bond API documentation](https://github.com/bondhome/api-v2).
