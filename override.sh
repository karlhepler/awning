#!/usr/bin/env bash
# Override the awning's schedule from this Mac.
#
#   ./override.sh closed   force closed until cleared (ignores weather and sun)
#   ./override.sh open     force open (ignores sun and time; still closes for wind or rain)
#   ./override.sh auto     follow the weather and schedule again
#   ./override.sh          show the current mode
#
# The setting lives on the Pi (logs/.override), because the Pi's 15-minute cron job
# is what decides. After setting it, this script runs the automation once so the
# awning moves now instead of waiting up to 15 minutes.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Same .env reader as deploy.sh (second use; not worth sharing until a third).
env_get() {
    local line
    line=$(grep -m1 "^$1=" "$SCRIPT_DIR/.env" 2>/dev/null) || return 0
    line="${line#*=}"
    case "$line" in
        \"*\") line="${line#\"}"; line="${line%%\"*}" ;;
        \'*\') line="${line#\'}"; line="${line%%\'*}" ;;
        *) line="${line%%[[:space:]]#*}"; line="${line%"${line##*[![:space:]]}"}" ;;
    esac
    printf '%s' "$line"
}

PI_HOST=$(env_get PI_HOST)
SERVER="karlhepler@${PI_HOST:-orangepi3-lts}"
MODE="$1"

# Runs on the Pi. $D, $LOG etc. expand there, not here.
CURRENT_CMD='cat "$HOME/.config/awning/logs/.override" 2>/dev/null || echo auto'

case "$MODE" in
    "")
        echo "Current override on $SERVER: $(lan-run ssh "$SERVER" "$CURRENT_CMD")"
        exit 0
        ;;
    closed|open|auto) ;;
    *)
        echo "Usage: $0 closed|open|auto   (no argument shows the current mode)" >&2
        exit 2
        ;;
esac

# MODE is one of three fixed words (checked above), so interpolating it is safe.
lan-run ssh "$SERVER" "
D=\$HOME/.config/awning
LOG=\$D/logs/awning-\$(date +%Y-%m-%d).log
RUN=\"\$D/venv/bin/python \$D/awning_automation.py --env-file=\$D/.env\"

\$RUN --set-override=$MODE >> \$LOG 2>&1

# Code on the Pi from before this feature ignores --set-override and just runs the
# automation. Check the setting really changed rather than trust the exit code.
NOW=\$($CURRENT_CMD)
if [ \"\$NOW\" != \"$MODE\" ]; then
    echo \"ERROR: override is '\$NOW', wanted '$MODE'. Has ./deploy.sh been run since this feature was added?\" >&2
    exit 1
fi

\$RUN >> \$LOG 2>&1 || echo 'WARNING: the automation run failed - see ~/awning.log' >&2
grep -E 'Override:|Decision:|Close sent|Open sent|Awning set' \$LOG | tail -n 4
"
echo "Override is now: $MODE"
