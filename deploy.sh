#!/usr/bin/env nix-shell
#!nix-shell -i bash -p sshpass
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE_DIR=".config/awning"

# PI_HOST override: the Pi has no avahi-daemon installed, so "orangepi3-lts.local"
# has never resolved via mDNS — the bare hostname only ever worked because the
# router's own DNS/DHCP registered it, and that registration goes stale after a
# router reboot or a long Pi outage (see the 2026-08-31 incident: two full days
# offline left the name unresolvable on the operator's Mac even after the Pi came
# back, because the resolver had negatively cached it). Set PI_HOST in .env to an
# IP or a name that does resolve to skip relying on that registration entirely.
PI_HOST=$(grep '^PI_HOST=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2 || echo "")
SERVER="karlhepler@${PI_HOST:-orangepi3-lts}"

# Get version from git
VERSION=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD)

# Load Telegram config from .env
TELEGRAM_BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2 || echo "")
TELEGRAM_CHAT_ID=$(grep '^TELEGRAM_CHAT_ID=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2 || echo "")

# Function to send Telegram notification
send_telegram() {
    local message="$1"
    if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        # Best effort: a Telegram outage must never abort (or fail) a deploy.
        curl -s --max-time 10 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -H "Content-Type: application/json" \
            -d "{\"chat_id\": \"${TELEGRAM_CHAT_ID}\", \"text\": \"${message}\"}" > /dev/null || true
    fi
}

# Discover Bond Bridge IP via mDNS
echo "Discovering Bond Bridge IP via mDNS..."
BOND_ID=$(grep '^BOND_ID=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2 || echo "")
if [ -z "$BOND_ID" ]; then
    # Fall back to extracting from BOND_HOST if it's a hostname
    BOND_HOST=$(grep '^BOND_HOST=' "$SCRIPT_DIR/.env" | cut -d= -f2)
    if [[ "$BOND_HOST" =~ ^[A-Za-z] ]]; then
        BOND_ID=$(echo "$BOND_HOST" | sed 's/^bond-//' | sed 's/\..*$//' | tr '[:lower:]' '[:upper:]')
    fi
fi

if [ -n "$BOND_ID" ]; then
    MDNS_OUTPUT=$(mktemp)
    dns-sd -G v4 "${BOND_ID}.local" > "$MDNS_OUTPUT" 2>&1 &
    DNS_PID=$!
    sleep 3
    kill $DNS_PID 2>/dev/null || true
    BOND_IP=$(grep -oE '192\.168\.[0-9]+\.[0-9]+' "$MDNS_OUTPUT" | head -1)
    rm -f "$MDNS_OUTPUT"

    if [ -n "$BOND_IP" ]; then
        echo "Found Bond Bridge at $BOND_IP"
        sed -i.bak "s/^BOND_HOST=.*/BOND_HOST=$BOND_IP/" "$SCRIPT_DIR/.env"
        rm -f "$SCRIPT_DIR/.env.bak"
    else
        echo "Warning: Could not discover Bond Bridge IP, using existing BOND_HOST"
    fi
else
    echo "Warning: No BOND_ID found, using existing BOND_HOST"
fi

echo "Deploying awning automation (version: $VERSION)..."

# Prompt for password
# -r: without it read treats backslashes as escapes, so a password containing one
# was silently altered and authentication failed.
read -r -s -p "Enter SSH password for $SERVER: " PASSWORD
echo

# Export for sshpass
export SSHPASS="$PASSWORD"

# Send deploy start notification
send_telegram "🚀 Deploying awning automation (${VERSION})..."

# Ensure python3-venv is installed.
# The check runs first and carries no secret. Only when the package is actually
# missing (a first-time setup) is the sudo password needed, and then it travels in
# a private temp file that sudo reads from stdin: it never appears in a process
# list (ssh's argv on this Mac, bash -c on the Pi), and a password containing a
# quote cannot break out of the command. lan-run does not forward stdin, so piping
# straight into ssh is not an option.
echo "Ensuring python3-venv is installed..."
if ! lan-run sshpass -e ssh "$SERVER" "dpkg -s python3-venv > /dev/null 2>&1"; then
    echo "python3-venv is missing; installing it (needs sudo)..."
    PW_FILE=$(mktemp)
    trap 'rm -f "$PW_FILE"' EXIT
    chmod 600 "$PW_FILE"
    printf '%s\n' "$PASSWORD" > "$PW_FILE"
    lan-run sshpass -e scp -q "$PW_FILE" "$SERVER:.awning-sudo-pw"
    lan-run sshpass -e ssh "$SERVER" "chmod 600 ~/.awning-sudo-pw; sudo -S -p '' apt-get update < ~/.awning-sudo-pw && sudo -S -p '' apt-get install -y python3-venv < ~/.awning-sudo-pw; rc=\$?; rm -f ~/.awning-sudo-pw; exit \$rc"
    rm -f "$PW_FILE"
fi

# Create remote directory, logs directory, and venv (only if venv doesn't exist)
echo "Setting up remote directory and virtual environment..."
lan-run sshpass -e ssh "$SERVER" "mkdir -p ~/$REMOTE_DIR/logs && [ -d ~/$REMOTE_DIR/venv ] || python3 -m venv ~/$REMOTE_DIR/venv"

# Migrate existing ~/awning.log if it's a regular file (not symlink)
# This is idempotent: if already migrated or symlink exists, does nothing
lan-run sshpass -e ssh "$SERVER" "
    if [ -f ~/awning.log ] && [ ! -L ~/awning.log ]; then
        echo 'Migrating existing log file...'
        cat ~/awning.log >> ~/.config/awning/logs/awning-\$(date '+%Y-%m-%d').log
        rm ~/awning.log
    fi
"

# Install Python dependencies from the pinned requirements.txt (the single source
# of truth; this script used to carry its own hard-coded, unpinned list).
echo "Installing Python dependencies..."
lan-run sshpass -e scp -q "$SCRIPT_DIR/requirements.txt" "$SERVER:~/$REMOTE_DIR/requirements.txt"
lan-run sshpass -e ssh "$SERVER" "~/$REMOTE_DIR/venv/bin/pip install -r ~/$REMOTE_DIR/requirements.txt"

# Keep the version that is running now, so a build that fails verification below
# can be rolled back instead of left live under the existing cron job.
echo "Backing up the currently deployed version..."
lan-run sshpass -e ssh "$SERVER" "cd ~/$REMOTE_DIR && for f in awning_controller.py awning_automation.py .env; do if [ -f \$f ]; then cp -p \$f \$f.prev; fi; done"

# Copy .env first, then the scripts, so a cron run that lands between the two
# never pairs new code with an old .env.
echo "Copying .env..."
lan-run sshpass -e scp "$SCRIPT_DIR/.env" "$SERVER:~/$REMOTE_DIR/.env"
lan-run sshpass -e ssh "$SERVER" "chmod 600 ~/$REMOTE_DIR/.env"

echo "Copying scripts..."
lan-run sshpass -e scp "$SCRIPT_DIR/awning_controller.py" "$SCRIPT_DIR/awning_automation.py" "$SERVER:~/$REMOTE_DIR/"

# Verify BEFORE touching the cron job. A failed dry-run (config error, Bond
# unreachable, ImportError) used to be ignored: it was written as `cmd && echo ""`
# under `set -e`, which does not exit when the left side of && fails, so the
# script went on to report "Deploy complete" for a broken build that was already
# live. Now it rolls back and stops.
echo "Verifying deployment (dry-run)..."
if ! lan-run sshpass -e ssh "$SERVER" "~/$REMOTE_DIR/venv/bin/python ~/$REMOTE_DIR/awning_automation.py --env-file=~/$REMOTE_DIR/.env --dry-run"; then
    echo "ERROR: dry-run failed. Rolling back to the previous version..." >&2
    lan-run sshpass -e ssh "$SERVER" "cd ~/$REMOTE_DIR && for f in awning_controller.py awning_automation.py .env; do if [ -f \$f.prev ]; then mv -f \$f.prev \$f; fi; done" || echo "WARNING: rollback command failed - check ~/$REMOTE_DIR on the device" >&2
    send_telegram "❌ Deploy FAILED dry-run verification (version ${VERSION}); rolled back to the previous version."
    echo "Deploy FAILED (version: $VERSION). The previous version is restored; the cron job was not changed." >&2
    exit 1
fi
echo

# Log deploy start to remote log file (dated log in logs directory)
echo "Logging deploy start..."
TODAY=$(date '+%Y-%m-%d')
LOG_FILE="\$HOME/.config/awning/logs/awning-$TODAY.log"
lan-run sshpass -e ssh "$SERVER" "echo '' >> $LOG_FILE && echo '$(date '+%Y-%m-%d %H:%M:%S') - INFO - 🚀 Deploy started (version: $VERSION)' >> $LOG_FILE"

# Configure cron (removes existing awning entry first)
# Python logs to stderr only; cron captures all output to log file
# Note: % in cron must be escaped as \%
echo "Configuring cron job..."
CRON_CMD='*/15 * * * * $HOME/.config/awning/venv/bin/python $HOME/.config/awning/awning_automation.py --env-file=$HOME/.config/awning/.env >> $HOME/.config/awning/logs/awning-$(date +\%Y-\%m-\%d).log 2>&1'
lan-run sshpass -e ssh "$SERVER" "(crontab -l 2>/dev/null | grep -v 'awning_automation'; echo '$CRON_CMD') | crontab -"

# Log deploy complete to remote log file (dated log in logs directory)
lan-run sshpass -e ssh "$SERVER" "echo '$(date '+%Y-%m-%d %H:%M:%S') - INFO - ✅ Deploy complete (version: $VERSION)' >> $LOG_FILE && echo '' >> $LOG_FILE"

# Create/update symlink to today's log
lan-run sshpass -e ssh "$SERVER" "ln -sf ~/.config/awning/logs/awning-\$(date '+%Y-%m-%d').log ~/awning.log"

# Send deploy complete notification
send_telegram "✅ Deploy complete! Version: ${VERSION}"

echo "Deploy complete! Version: $VERSION"
echo "Logs: ~/.config/awning/logs/ on $SERVER (~/awning.log symlinks to today)"
