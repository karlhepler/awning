#!/usr/bin/env nix-shell
#!nix-shell -i bash -p sshpass
set -e

SERVER="karlhepler@orangepi3-lts"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE_DIR=".config/awning"

# Get version from git
VERSION=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD)

# Load Telegram config from .env
TELEGRAM_BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2 || echo "")
TELEGRAM_CHAT_ID=$(grep '^TELEGRAM_CHAT_ID=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2 || echo "")

# Function to send Telegram notification
send_telegram() {
    local message="$1"
    if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -H "Content-Type: application/json" \
            -d "{\"chat_id\": \"${TELEGRAM_CHAT_ID}\", \"text\": \"${message}\"}" > /dev/null
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
read -s -p "Enter SSH password for $SERVER: " PASSWORD
echo

# Export for sshpass
export SSHPASS="$PASSWORD"

# Send deploy start notification
send_telegram "🚀 Deploying awning automation (${VERSION})..."

# Ensure python3-venv is installed
echo "Ensuring python3-venv is installed..."
sshpass -e ssh "$SERVER" "dpkg -s python3-venv > /dev/null 2>&1 || (echo '$PASSWORD' | sudo -S apt-get update && echo '$PASSWORD' | sudo -S apt-get install -y python3-venv)"

# Create remote directory, logs directory, and venv (only if venv doesn't exist)
echo "Setting up remote directory and virtual environment..."
sshpass -e ssh "$SERVER" "mkdir -p ~/$REMOTE_DIR/logs && [ -d ~/$REMOTE_DIR/venv ] || python3 -m venv ~/$REMOTE_DIR/venv"

# Migrate existing ~/awning.log if it's a regular file (not symlink)
# This is idempotent: if already migrated or symlink exists, does nothing
sshpass -e ssh "$SERVER" "
    if [ -f ~/awning.log ] && [ ! -L ~/awning.log ]; then
        echo 'Migrating existing log file...'
        cat ~/awning.log >> ~/.config/awning/logs/awning-\$(date '+%Y-%m-%d').log
        rm ~/awning.log
    fi
"

# Install Python dependencies
echo "Installing Python dependencies..."
sshpass -e ssh "$SERVER" "~/$REMOTE_DIR/venv/bin/pip install requests python-dotenv rich pvlib pandas pytz tenacity"

# Copy Python scripts
echo "Copying scripts..."
sshpass -e scp "$SCRIPT_DIR/awning_controller.py" "$SCRIPT_DIR/awning_automation.py" "$SERVER:~/$REMOTE_DIR/"

# Copy .env file
echo "Copying .env..."
sshpass -e scp "$SCRIPT_DIR/.env" "$SERVER:~/$REMOTE_DIR/.env"

# Log deploy start to remote log file (dated log in logs directory)
echo "Logging deploy start..."
TODAY=$(date '+%Y-%m-%d')
LOG_FILE="\$HOME/.config/awning/logs/awning-$TODAY.log"
sshpass -e ssh "$SERVER" "echo '' >> $LOG_FILE && echo '$(date '+%Y-%m-%d %H:%M:%S') - INFO - 🚀 Deploy started (version: $VERSION)' >> $LOG_FILE"

# Configure cron (removes existing awning entry first)
# Python FileHandler writes to log file directly; discard stdout to avoid duplicates
# Only capture stderr for Python startup errors
# Note: % in cron must be escaped as \%
echo "Configuring cron job..."
CRON_CMD='*/15 * * * * $HOME/.config/awning/venv/bin/python $HOME/.config/awning/awning_automation.py --env-file=$HOME/.config/awning/.env >/dev/null 2>> $HOME/.config/awning/logs/awning-$(date +\%Y-\%m-\%d).log'
sshpass -e ssh "$SERVER" "(crontab -l 2>/dev/null | grep -v 'awning_automation'; echo '$CRON_CMD') | crontab -"

# Verify deployment
echo "Verifying deployment..."
sshpass -e ssh "$SERVER" "~/$REMOTE_DIR/venv/bin/python ~/$REMOTE_DIR/awning_automation.py --env-file=~/$REMOTE_DIR/.env --dry-run" && echo ""

# Log deploy complete to remote log file (dated log in logs directory)
sshpass -e ssh "$SERVER" "echo '$(date '+%Y-%m-%d %H:%M:%S') - INFO - ✅ Deploy complete (version: $VERSION)' >> $LOG_FILE && echo '' >> $LOG_FILE"

# Create/update symlink to today's log
sshpass -e ssh "$SERVER" "ln -sf ~/.config/awning/logs/awning-\$(date '+%Y-%m-%d').log ~/awning.log"

# Send deploy complete notification
send_telegram "✅ Deploy complete! Version: ${VERSION}"

echo "Deploy complete! Version: $VERSION"
echo "Logs: ~/.config/awning/logs/ on $SERVER (~/awning.log symlinks to today)"
