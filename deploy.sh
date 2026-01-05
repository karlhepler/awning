#!/usr/bin/env nix-shell
#!nix-shell -i bash -p sshpass
set -e

SERVER="karlhepler@orangepi3-lts"
IMAGE="awning-automation"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Get version from git
VERSION=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD)

# Build Docker image
echo "Building Docker image (version: $VERSION)..."
docker build --build-arg VERSION="$VERSION" -t "$IMAGE" "$SCRIPT_DIR"

# Prompt for password
read -s -p "Enter SSH password for $SERVER: " PASSWORD
echo

# Export for sshpass
export SSHPASS="$PASSWORD"

# Save and copy image
echo "Copying image to server..."
sshpass -e ssh "$SERVER" 'rm -f /tmp/awning-automation.tar.gz'
docker save "$IMAGE" | gzip | sshpass -e ssh "$SERVER" 'cat > /tmp/awning-automation.tar.gz'

# Copy .env to server
echo "Copying .env to server..."
sshpass -e ssh "$SERVER" 'mkdir -p ~/.config/awning'
sshpass -e scp "$SCRIPT_DIR/.env" "$SERVER:~/.config/awning/.env"

# Load image
echo "Loading image..."
sshpass -e ssh "$SERVER" 'gunzip -c /tmp/awning-automation.tar.gz | podman load && rm /tmp/awning-automation.tar.gz'

# Configure cron (removes existing entry first, so only one ever exists)
echo "Configuring cron job..."
CRON_CMD='*/15 * * * * XDG_RUNTIME_DIR=/run/user/$(id -u) /usr/bin/podman run --rm --network=host --env-file=$HOME/.config/awning/.env awning-automation >> $HOME/.config/awning/automation.log 2>&1'
sshpass -e ssh "$SERVER" "(crontab -l 2>/dev/null | grep -v 'awning-automation'; echo '$CRON_CMD') | crontab -"

# Verify deployment
echo "Verifying deployment..."
DEPLOYED_VERSION=$(sshpass -e ssh "$SERVER" 'podman run --rm --network=none awning-automation version')
echo "Deploy complete! Version: $DEPLOYED_VERSION"
