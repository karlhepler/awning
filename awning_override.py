"""
Mac-side control of the awning's schedule override.

The override itself lives on the Pi (logs/.override), because the Pi's 15-minute
cron job is what decides. This module turns `awning close --until 5pm` into the
SSH call that sets it there, then runs the automation once so the awning moves
now instead of waiting up to 15 minutes. It never talks to the Bond Bridge.
"""

import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

DEFAULT_PI_HOST = "orangepi3-lts"

# Runs on the Pi. Nothing below is interpolated from user input except the mode
# (one of three fixed words) and the end time (a timestamp this module generated),
# both passed through shlex.quote anyway.
_REMOTE_DIR = "$HOME/.config/awning"
_OVERRIDE_FILE = f"{_REMOTE_DIR}/logs/.override"
_REMOTE_RUN = (
    f'"{_REMOTE_DIR}/venv/bin/python" "{_REMOTE_DIR}/awning_automation.py" '
    f'--env-file="{_REMOTE_DIR}/.env"'
)


class OverrideError(Exception):
    """The override could not be parsed, set or read. The message is for the operator."""


_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?$")
_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")
_HELP = "use a clock time (5pm, 5:30pm, 17:30) or a length (3h, 90m, 1h30m)"


def parse_until(text: str, now: Optional[datetime] = None) -> datetime:
    """
    Turn `5pm`, `17:30` or `3h` into the moment the override should end.

    A clock time means its next occurrence (so `8am` typed at night is tomorrow
    morning). A length is added to now. Returns a timestamp with a zone, so the Pi
    does not need to share this Mac's clock settings. Raises OverrideError.
    """
    now = now or datetime.now().astimezone()
    value = text.strip().lower()

    duration = _DURATION.match(value)
    if value and duration:
        hours, minutes = int(duration.group(1) or 0), int(duration.group(2) or 0)
        if hours == 0 and minutes == 0:
            raise OverrideError(f"--until {text!r} is no time at all; {_HELP}")
        return (now + timedelta(hours=hours, minutes=minutes)).replace(microsecond=0)

    clock = _CLOCK.match(value)
    if clock:
        hour, minute, meridiem = int(clock.group(1)), int(clock.group(2) or 0), clock.group(3)
        if meridiem:
            valid = 1 <= hour <= 12
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
        else:
            # A bare "5" could be morning or evening; refuse rather than guess.
            valid = clock.group(2) is not None and 0 <= hour <= 23
        if valid and minute <= 59:
            # Build the moment as local wall-clock time and let the system attach the
            # zone for that date, so a day that crosses a clock change stays correct.
            naive_now = now.replace(tzinfo=None)
            moment = naive_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if moment <= naive_now:
                moment += timedelta(days=1)
            return moment.astimezone()

    raise OverrideError(f"Cannot read --until {text!r}; {_HELP}")


def pi_host() -> str:
    """The Pi's address: PI_HOST from .env (cwd first, then beside this file), else the default name."""
    for env_file in (Path.cwd() / ".env", Path(__file__).parent / ".env"):
        if env_file.exists():
            load_dotenv(env_file)
            break
    return f"karlhepler@{os.getenv('PI_HOST', '').strip() or DEFAULT_PI_HOST}"


def _ssh(server: str, command: str) -> subprocess.CompletedProcess:
    """Run a command on the Pi. `lan-run` gets it past the macOS Local Network block inside tmux."""
    prefix = ["lan-run"] if shutil.which("lan-run") else []
    try:
        return subprocess.run(
            [*prefix, "ssh", server, command], capture_output=True, text=True, timeout=120
        )
    except FileNotFoundError as e:
        raise OverrideError(f"Cannot run ssh: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise OverrideError(f"The Pi ({server}) did not answer within 2 minutes") from e


def remote_set_command(mode: str, until: Optional[datetime]) -> str:
    """The shell script that sets the override on the Pi, checks it took, then runs the automation once."""
    until_flag = f" --until={shlex.quote(until.isoformat(timespec='seconds'))}" if until else ""
    expect_until = "1" if until and mode != "auto" else "0"
    return f"""
LOG="{_REMOTE_DIR}/logs/awning-$(date +%Y-%m-%d).log"
{_REMOTE_RUN} --set-override={shlex.quote(mode)}{until_flag} >> "$LOG" 2>&1

# Code on the Pi from before this feature ignores the flags and just runs the
# automation. Check the setting really changed rather than trust the exit code.
NOW=$(head -n 1 "{_OVERRIDE_FILE}" 2>/dev/null || echo auto)
if [ "$NOW" != {shlex.quote(mode)} ] || {{ [ {expect_until} = 1 ] && ! grep -q '^until=' "{_OVERRIDE_FILE}"; }}; then
    echo "ERROR: the Pi did not take the override (it says '$NOW'). Has ./deploy.sh been run since this feature was added?" >&2
    exit 1
fi

{_REMOTE_RUN} >> "$LOG" 2>&1 || echo 'WARNING: the automation run failed - see ~/awning.log' >&2
grep -E 'Override:|Decision:|Close sent|Open sent|Awning set' "$LOG" | tail -n 4
"""


def set_override(mode: str, until: Optional[datetime] = None) -> str:
    """
    Set the override on the Pi and make the awning act on it now.

    Returns the Pi's last few log lines (what it decided). Raises OverrideError.
    """
    server = pi_host()
    result = _ssh(server, remote_set_command(mode, until))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit code {result.returncode}"
        raise OverrideError(f"Setting the override on {server} failed: {detail}")
    return result.stdout.strip()


def show_override() -> str:
    """The Pi's description of the current override. Raises OverrideError."""
    server = pi_host()
    result = _ssh(server, f"{_REMOTE_RUN} --show-override 2>&1 | tail -n 1")
    out = result.stdout.strip()
    if result.returncode != 0 or not out:
        raise OverrideError(f"Could not read the override from {server}: {(result.stderr or out).strip()}")
    return out
