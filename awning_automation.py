#!/usr/bin/env python3
"""
Awning Weather Automation

Automatically opens/closes awning based on weather conditions.
- Opens awning if ALL 7 conditions are met: sunny, calm, no rain, above freezing,
  daytime, sun high enough, and sun facing window (90°-220°)
- Closes awning if ANY condition fails

Sunshine detection uses cirrus-aware logic:
- Normal mode: DNI >= MIN_DIRECT_IRRADIANCE_WM2 AND low clouds < MAX_LOW_CLOUD_PERCENT
- Cirrus mode: When high clouds dominate (cloud_cover_high > CIRRUS_HIGH_CLOUD_THRESHOLD)
  and low/mid clouds are minimal, a lower DNI threshold (MIN_DNI_CIRRUS_WM2) is used
  so the awning stays open on thin high-cloud days but closes on genuinely overcast days.

Designed to run as a cron job or Kubernetes scheduled job.
"""

import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv
from pvlib import solarposition
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from awning_controller import (
    BondAPIError,
    BondAwningController,
    ConfigurationError,
    create_controller_from_env,
)

# Logger instance (configured by setup_logging())
logger = logging.getLogger(__name__)


def setup_logging(env_file: Optional[Path] = None) -> Path:
    """
    Configure logging with daily file rotation and symlink.

    Creates log files in a 'logs' directory with daily rotation.
    Also creates a symlink at ~/awning.log pointing to today's log.

    Args:
        env_file: Optional path to .env file (used to determine log directory)

    Returns:
        Path to today's log file
    """
    # Determine log directory (relative to env_file or .env location)
    if env_file and env_file.exists():
        base_dir = env_file.parent
    else:
        # When env_file not specified, search for .env in cwd then script dir
        # Use the directory where .env is found for logs
        cwd_env = Path.cwd() / ".env"
        script_env = Path(__file__).parent / ".env"
        if cwd_env.exists():
            base_dir = Path.cwd()
        elif script_env.exists():
            base_dir = Path(__file__).parent
        else:
            # No .env found, default to cwd (avoids writing to Nix store)
            base_dir = Path.cwd()

    log_dir = base_dir / "logs"

    # Create logs directory if needed
    log_dir.mkdir(parents=True, exist_ok=True)

    # Generate today's log filename
    today = date.today().isoformat()  # YYYY-MM-DD, uses system timezone
    log_filename = f"awning-{today}.log"
    log_path = log_dir / log_filename

    # Configure logging to stderr only (cron redirects to file)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Only stderr handler - cron redirects to log file
    stream_handler = logging.StreamHandler()  # defaults to stderr
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    # Create/update symlink at ~/awning.log
    symlink_path = Path.home() / "awning.log"
    try:
        if symlink_path.is_symlink():
            # Existing symlink (possibly broken) - remove it
            symlink_path.unlink()
        elif symlink_path.exists():
            # Regular file exists (first migration)
            # Append existing content to today's log, then remove
            with open(symlink_path, "r") as old, open(log_path, "a") as new:
                content = old.read()
                if content.strip():
                    new.write(content)
                    if not content.endswith("\n"):
                        new.write("\n")
            symlink_path.unlink()

        # Create symlink pointing to today's log
        symlink_path.symlink_to(log_path)
    except OSError as e:
        # Permission denied or other OS error - log warning but continue
        logging.warning(f"Could not create symlink at ~/awning.log: {e}")

    return log_path


def cleanup_old_logs(log_dir: Path, retention_days: int = 30) -> None:
    """
    Delete log files older than retention_days.

    Args:
        log_dir: Directory containing log files
        retention_days: Number of days to keep log files (default: 30)
    """
    cutoff = date.today() - timedelta(days=retention_days)

    for log_file in log_dir.glob("awning-*.log"):
        try:
            # Extract date from filename: awning-YYYY-MM-DD.log
            date_str = log_file.stem.replace("awning-", "")
            log_date = date.fromisoformat(date_str)
            if log_date < cutoff:
                log_file.unlink()
                logging.info(f"Deleted old log: {log_file.name}")
        except (ValueError, OSError):
            # Invalid filename format or permission error - skip
            pass

# Retry configuration for Weather API
# Retries transient network errors with exponential backoff (2s, 4s, 8s)
WEATHER_RETRY_CONFIG = {
    "stop": stop_after_attempt(3),
    "wait": wait_exponential(multiplier=2, min=2, max=30),
    "retry": retry_if_exception_type(
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        )
    ),
    "reraise": True,
    "before_sleep": before_sleep_log(logger, logging.WARNING),
}

# Retry configuration for Telegram API (best-effort, shorter waits)
TELEGRAM_RETRY_CONFIG = {
    "stop": stop_after_attempt(2),
    "wait": wait_exponential(multiplier=1, min=1, max=5),
    "retry": retry_if_exception_type(
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        )
    ),
    "reraise": True,
    "before_sleep": before_sleep_log(logger, logging.WARNING),
}


class WeatherAPIError(Exception):
    """Raised when weather API request fails."""

    pass


def load_location_config(env_file: Optional[Path] = None) -> tuple[float, float]:
    """
    Load location configuration from environment variables.

    Args:
        env_file: Optional path to .env file

    Returns:
        Tuple of (latitude, longitude)

    Raises:
        ConfigurationError: If location variables are missing or invalid
    """
    # Load .env file
    if env_file:
        if not env_file.exists():
            raise ConfigurationError(f".env file not found: {env_file}")
        load_dotenv(env_file)
    else:
        # Search for .env in current working directory first, then script directory
        cwd_env_file = Path.cwd() / ".env"
        script_env_file = Path(__file__).parent / ".env"

        if cwd_env_file.exists():
            load_dotenv(cwd_env_file)
        elif script_env_file.exists():
            load_dotenv(script_env_file)

    # Get latitude
    lat_str = os.getenv("LATITUDE", "").strip()
    if not lat_str:
        raise ConfigurationError(
            "LATITUDE environment variable is not set. "
            "Please add it to your .env file (e.g., LATITUDE=37.7749)"
        )

    # Get longitude
    lon_str = os.getenv("LONGITUDE", "").strip()
    if not lon_str:
        raise ConfigurationError(
            "LONGITUDE environment variable is not set. "
            "Please add it to your .env file (e.g., LONGITUDE=-122.4194)"
        )

    # Parse as floats
    try:
        latitude = float(lat_str)
        longitude = float(lon_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid LATITUDE or LONGITUDE format: {e}. Must be decimal numbers."
        ) from e

    # Validate ranges
    if not (-90 <= latitude <= 90):
        raise ConfigurationError(
            f"LATITUDE must be between -90 and 90, got: {latitude}"
        )
    if not (-180 <= longitude <= 180):
        raise ConfigurationError(
            f"LONGITUDE must be between -180 and 180, got: {longitude}"
        )

    return latitude, longitude


def get_thresholds() -> tuple[float, float, float, float, float, float, float]:
    """
    Get weather thresholds from environment variables.

    Returns:
        Tuple of (wind_speed_threshold_mph, min_sun_altitude, min_dni,
                  max_low_cloud, max_mid_cloud, cirrus_high_threshold, min_dni_cirrus)

    Raises:
        ConfigurationError: If threshold variables are missing or invalid
    """
    # Get wind speed threshold
    wind_str = os.getenv("WIND_SPEED_THRESHOLD_MPH", "").strip()
    if not wind_str:
        raise ConfigurationError(
            "WIND_SPEED_THRESHOLD_MPH environment variable is not set. "
            "Please add it to your .env file (e.g., WIND_SPEED_THRESHOLD_MPH=10)"
        )

    # Get minimum sun altitude threshold
    altitude_str = os.getenv("MIN_SUN_ALTITUDE_DEG", "").strip()
    if not altitude_str:
        raise ConfigurationError(
            "MIN_SUN_ALTITUDE_DEG environment variable is not set. "
            "Please add it to your .env file (e.g., MIN_SUN_ALTITUDE_DEG=20)"
        )

    # Get minimum direct normal irradiance threshold
    dni_str = os.getenv("MIN_DIRECT_IRRADIANCE_WM2", "").strip()
    if not dni_str:
        raise ConfigurationError(
            "MIN_DIRECT_IRRADIANCE_WM2 environment variable is not set. "
            "Please add it to your .env file (e.g., MIN_DIRECT_IRRADIANCE_WM2=300)"
        )

    # Parse required thresholds
    try:
        wind_threshold = float(wind_str)
        altitude_threshold = float(altitude_str)
        dni_threshold = float(dni_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid threshold format: {e}. Must be numbers."
        ) from e

    # Validate required thresholds
    if wind_threshold < 0:
        raise ConfigurationError(
            f"WIND_SPEED_THRESHOLD_MPH must be positive, got: {wind_threshold}"
        )
    if not (0 <= altitude_threshold <= 90):
        raise ConfigurationError(
            f"MIN_SUN_ALTITUDE_DEG must be between 0 and 90, got: {altitude_threshold}"
        )
    if dni_threshold < 0:
        raise ConfigurationError(
            f"MIN_DIRECT_IRRADIANCE_WM2 must be non-negative, got: {dni_threshold}"
        )

    # Get low cloud cover threshold (optional, defaults to 40%)
    max_low_cloud_str = os.getenv("MAX_LOW_CLOUD_PERCENT", "40").strip()
    try:
        max_low_cloud = float(max_low_cloud_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid MAX_LOW_CLOUD_PERCENT format: {e}. Must be a number."
        ) from e
    if not (0 <= max_low_cloud <= 100):
        raise ConfigurationError(
            f"MAX_LOW_CLOUD_PERCENT must be between 0 and 100, got: {max_low_cloud}"
        )

    # Get mid cloud cover threshold for cirrus classification (optional, defaults to 20%)
    max_mid_cloud_str = os.getenv("MAX_MID_CLOUD_PERCENT", "20").strip()
    try:
        max_mid_cloud = float(max_mid_cloud_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid MAX_MID_CLOUD_PERCENT format: {e}. Must be a number."
        ) from e
    if not (0 <= max_mid_cloud <= 100):
        raise ConfigurationError(
            f"MAX_MID_CLOUD_PERCENT must be between 0 and 100, got: {max_mid_cloud}"
        )

    # Get high cloud cover threshold to classify as cirrus-dominated (optional, defaults to 60%)
    cirrus_high_str = os.getenv("CIRRUS_HIGH_CLOUD_THRESHOLD", "60").strip()
    try:
        cirrus_high_threshold = float(cirrus_high_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid CIRRUS_HIGH_CLOUD_THRESHOLD format: {e}. Must be a number."
        ) from e
    if not (0 <= cirrus_high_threshold <= 100):
        raise ConfigurationError(
            f"CIRRUS_HIGH_CLOUD_THRESHOLD must be between 0 and 100, got: {cirrus_high_threshold}"
        )

    # Get lower DNI threshold used when cirrus-dominated (optional, defaults to 30 W/m²)
    min_dni_cirrus_str = os.getenv("MIN_DNI_CIRRUS_WM2", "30").strip()
    try:
        min_dni_cirrus = float(min_dni_cirrus_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid MIN_DNI_CIRRUS_WM2 format: {e}. Must be a number."
        ) from e
    if min_dni_cirrus < 0:
        raise ConfigurationError(
            f"MIN_DNI_CIRRUS_WM2 must be non-negative, got: {min_dni_cirrus}"
        )

    return wind_threshold, altitude_threshold, dni_threshold, max_low_cloud, max_mid_cloud, cirrus_high_threshold, min_dni_cirrus


def load_telegram_config() -> tuple[Optional[str], Optional[str]]:
    """
    Load Telegram configuration from environment variables.

    Returns:
        Tuple of (bot_token, chat_id) or (None, None) if not configured
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if bot_token and chat_id:
        return bot_token, chat_id
    return None, None


def send_telegram_notification(
    bot_token: str, chat_id: str, message: str, timeout: int = 5
) -> bool:
    """
    Send a notification via Telegram Bot API.

    Args:
        bot_token: Telegram bot token
        chat_id: Chat ID to send message to
        message: Message text to send
        timeout: Request timeout in seconds

    Returns:
        True if message sent successfully, False otherwise
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}

    try:
        _send_telegram_request(url, payload, timeout)
        logger.info("Telegram notification sent")
        return True
    except requests.RequestException as e:
        logger.warning(f"Failed to send Telegram notification: {e}")
        return False


@retry(**TELEGRAM_RETRY_CONFIG)
def _send_telegram_request(url: str, payload: dict, timeout: int) -> None:
    """Make a POST request to Telegram API with retry logic."""
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()


def fetch_weather(lat: float, lon: float, timeout: int = 10) -> dict:
    """
    Fetch current weather and daily data from Open-Meteo API.

    Args:
        lat: Latitude
        lon: Longitude
        timeout: Request timeout in seconds

    Returns:
        Weather data dictionary with current conditions and daily sunrise/sunset

    Raises:
        WeatherAPIError: If API request fails
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "wind_speed_10m,precipitation,is_day,temperature_2m,direct_normal_irradiance,cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high",
        "daily": "sunrise,sunset",
        "wind_speed_unit": "mph",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "forecast_days": 1,
    }

    try:
        data = _fetch_weather_request(url, params, timeout)

        # Extract current weather
        if "current" not in data:
            raise WeatherAPIError("Weather API response missing 'current' field")

        current = data["current"]
        required_fields = ["wind_speed_10m", "precipitation", "temperature_2m", "direct_normal_irradiance"]
        for field in required_fields:
            if field not in current:
                raise WeatherAPIError(
                    f"Weather API response missing '{field}' in current data"
                )

        # Extract daily data (sunrise/sunset)
        if "daily" not in data:
            raise WeatherAPIError("Weather API response missing 'daily' field")

        daily = data["daily"]
        if "sunrise" not in daily or "sunset" not in daily:
            raise WeatherAPIError(
                "Weather API response missing sunrise or sunset in daily data"
            )

        return {
            "wind_speed_10m": current["wind_speed_10m"],
            "precipitation": current["precipitation"],
            "temperature": current["temperature_2m"],
            "dni": current["direct_normal_irradiance"],
            "cloud_cover": current.get("cloud_cover", 100),
            "cloud_cover_low": current.get("cloud_cover_low", 100),
            "cloud_cover_mid": current.get("cloud_cover_mid", 100),
            "cloud_cover_high": current.get("cloud_cover_high", 100),
            "is_day": current.get("is_day", 1),
            "time": current.get("time", "unknown"),
            "sunrise": daily["sunrise"][0],
            "sunset": daily["sunset"][0],
        }

    except requests.RequestException as e:
        raise WeatherAPIError(f"Failed to fetch weather data: {e}") from e


@retry(**WEATHER_RETRY_CONFIG)
def _fetch_weather_request(url: str, params: dict, timeout: int) -> dict:
    """Make a GET request to weather API with retry logic."""
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def calculate_sun_position(lat: float, lon: float, dt: datetime) -> dict:
    """
    Calculate sun position using pvlib.

    Args:
        lat: Latitude
        lon: Longitude
        dt: Datetime (should be timezone-aware)

    Returns:
        Dictionary with 'azimuth' and 'altitude' in degrees
    """
    # Ensure datetime is timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    # Create pandas DatetimeIndex
    time = pd.DatetimeIndex([dt])

    # Calculate solar position
    solar_pos = solarposition.get_solarposition(time, lat, lon)

    return {
        "azimuth": float(solar_pos["azimuth"].iloc[0]),
        "altitude": float(solar_pos["apparent_elevation"].iloc[0]),
    }


def is_sun_facing_window(azimuth: float) -> bool:
    """
    Check if sun is facing the window (between East and Southwest).

    Args:
        azimuth: Sun azimuth in degrees (0=North, 90=East, 180=South, 270=West)

    Returns:
        True if azimuth is between 90° (East) and 220° (Southwest)
    """
    return 90 <= azimuth <= 220


def is_daytime(current_time: datetime, sunrise_str: str, sunset_str: str) -> bool:
    """
    Check if current time is between sunrise and sunset.

    Args:
        current_time: Current datetime (timezone-aware)
        sunrise_str: Sunrise time as ISO 8601 string
        sunset_str: Sunset time as ISO 8601 string

    Returns:
        True if current time is between sunrise and sunset
    """
    # Parse sunrise and sunset strings (they come from Open-Meteo in local timezone)
    # Remove 'Z' if present and parse
    sunrise_clean = sunrise_str.replace("Z", "+00:00")
    sunset_clean = sunset_str.replace("Z", "+00:00")

    sunrise = datetime.fromisoformat(sunrise_clean)
    sunset = datetime.fromisoformat(sunset_clean)

    # Ensure all datetimes are timezone-aware and in same timezone
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    # If sunrise/sunset are naive, assume they're in local time and convert to UTC
    if sunrise.tzinfo is None:
        # Open-Meteo returns local time, but we're comparing to UTC current_time
        # Convert current_time to naive for comparison
        current_time_naive = current_time.replace(tzinfo=None)
        return sunrise <= current_time_naive <= sunset

    return sunrise <= current_time <= sunset


def should_open_awning(
    weather: dict,
    sun_position: dict,
    current_time: datetime,
    wind_threshold: float,
    altitude_threshold: float,
    dni_threshold: float,
    max_low_cloud: float,
    max_mid_cloud: float,
    cirrus_high_threshold: float,
    min_dni_cirrus: float,
) -> tuple[bool, str, dict]:
    """
    Determine if awning should be open based on ALL conditions.

    Sunshine detection is cirrus-aware:
    - Cirrus-dominated: cloud_cover_high > cirrus_high_threshold AND
      cloud_cover_low < max_low_cloud AND cloud_cover_mid < max_mid_cloud
    - When cirrus-dominated, uses min_dni_cirrus as the DNI threshold (lower bar)
    - Otherwise uses dni_threshold as the DNI threshold
    - Cloud cover gating is based on low clouds only (cloud_cover_low)

    Args:
        weather: Weather data from fetch_weather()
        sun_position: Sun position data from calculate_sun_position()
        current_time: Current datetime
        wind_threshold: Maximum wind speed (mph) for "calm"
        altitude_threshold: Minimum sun altitude (degrees) above horizon
        dni_threshold: Minimum direct normal irradiance (W/m²) for "sunny" (normal mode)
        max_low_cloud: Maximum low cloud cover (%) allowed to open awning
        max_mid_cloud: Maximum mid cloud cover (%) for cirrus classification
        cirrus_high_threshold: Minimum high cloud cover (%) to classify as cirrus-dominated
        min_dni_cirrus: Lower DNI threshold (W/m²) used when cirrus-dominated

    Returns:
        Tuple of (should_open, reason, conditions_dict)
    """
    # Extract weather data
    wind_speed = weather["wind_speed_10m"]
    precipitation = weather["precipitation"]
    temperature = weather["temperature"]
    dni = weather["dni"]
    cloud_cover_low = weather["cloud_cover_low"]
    cloud_cover_mid = weather["cloud_cover_mid"]
    cloud_cover_high = weather["cloud_cover_high"]
    sunrise = weather["sunrise"]
    sunset = weather["sunset"]

    # Extract sun position
    azimuth = sun_position["azimuth"]
    altitude = sun_position["altitude"]

    # Determine if sky is cirrus-dominated (high thin clouds, low/mid clouds minimal)
    cirrus_dominated = (
        cloud_cover_high > cirrus_high_threshold
        and cloud_cover_low < max_low_cloud
        and cloud_cover_mid < max_mid_cloud
    )

    # Select effective DNI threshold based on cirrus classification
    effective_dni_threshold = min_dni_cirrus if cirrus_dominated else dni_threshold

    # Evaluate sunny condition:
    # - DNI must meet the effective threshold (lower if cirrus-dominated)
    # - Low clouds must be below max_low_cloud (high clouds handled by cirrus logic)
    is_sunny = dni >= effective_dni_threshold and cloud_cover_low < max_low_cloud

    is_calm = wind_speed < wind_threshold
    no_rain = precipitation == 0
    above_freezing = temperature > 32
    is_day = is_daytime(current_time, sunrise, sunset)
    sun_high_enough = altitude >= altitude_threshold
    sun_facing_se = is_sun_facing_window(azimuth)

    # Build conditions dict for logging
    conditions = {
        "sunny": is_sunny,
        "calm": is_calm,
        "no_rain": no_rain,
        "above_freezing": above_freezing,
        "daytime": is_day,
        "sun_high": sun_high_enough,
        "sun_facing_window": sun_facing_se,
        "cirrus_dominated": cirrus_dominated,
    }

    # All primary conditions must be True to open awning (cirrus_dominated is informational)
    primary_conditions = {k: v for k, v in conditions.items() if k != "cirrus_dominated"}
    should_open = all(primary_conditions.values())

    # Build detailed reason string
    cirrus_label = f" [cirrus override, threshold={effective_dni_threshold:.0f} W/m²]" if cirrus_dominated else ""
    reasons = []
    if not is_sunny:
        if dni < effective_dni_threshold:
            reasons.append(
                f"Not sunny (DNI {dni:.0f} < {effective_dni_threshold:.0f} W/m²{cirrus_label})"
            )
        if cloud_cover_low >= max_low_cloud:
            reasons.append(f"Too cloudy (low clouds {cloud_cover_low:.0f}% >= {max_low_cloud:.0f}%)")
    if not is_calm:
        reasons.append(f"Too windy ({wind_speed} >= {wind_threshold} mph)")
    if not no_rain:
        reasons.append(f"Raining ({precipitation} mm/h)")
    if not above_freezing:
        reasons.append(f"Too cold ({temperature}°F <= 32°F)")
    if not is_day:
        reasons.append(
            f"Nighttime (sunrise {sunrise[11:16]}, sunset {sunset[11:16]})"
        )
    if not sun_high_enough:
        reasons.append(f"Sun too low ({altitude:.1f}° < {altitude_threshold}°)")
    if not sun_facing_se:
        reasons.append(f"Sun not facing window (azimuth {azimuth:.1f}°, need 90°-220°)")

    if should_open:
        cirrus_note = f" (cirrus override, threshold={effective_dni_threshold:.0f} W/m²)" if cirrus_dominated else ""
        reason = (
            f"All conditions met: DNI {dni:.0f} W/m²{cirrus_note}, low clouds {cloud_cover_low:.0f}%, "
            f"mid {cloud_cover_mid:.0f}%, high {cloud_cover_high:.0f}%, "
            f"{wind_speed} mph wind, {precipitation} mm/h rain, {temperature}°F, "
            f"sun azimuth {azimuth:.1f}° (altitude {altitude:.1f}°)"
        )
    else:
        reason = ", ".join(reasons)

    return should_open, reason, conditions


def _format_friendly_telegram_message(
    should_open: bool,
    conditions: dict,
    wind_speed: float,
    precipitation: float,
    temperature: float,
    cloud_cover_low: float,
    cloud_cover_high: float,
) -> str:
    """
    Format a human-friendly Telegram notification message.

    Args:
        should_open: Whether awning should be open
        conditions: Dictionary of condition flags (may include cirrus_dominated)
        wind_speed: Wind speed in mph
        precipitation: Precipitation in mm/h
        temperature: Temperature in F
        cloud_cover_low: Low cloud cover percentage
        cloud_cover_high: High cloud cover percentage

    Returns:
        Friendly message string with appropriate emoji
    """
    if should_open:
        # Opening message - simple and positive
        temp_f = int(round(temperature))
        wind_mph = int(round(wind_speed))
        cirrus_note = " (cirrus)" if conditions.get("cirrus_dominated") else ""
        return f"☀️ Awning opened - sunny{cirrus_note} & calm ({temp_f}°F, {wind_mph} mph wind)"

    # Closing message - determine primary reason and emoji
    # Priority: rain > wind > cold > cloudy > nighttime > sun position

    if not conditions["no_rain"]:
        precip = round(precipitation, 1)
        return f"🌧️ Awning closed: Rain starting ({precip} mm/h)"

    if not conditions["calm"]:
        wind_mph = int(round(wind_speed))
        return f"💨 Awning closed: Too windy ({wind_mph} mph)"

    if not conditions["above_freezing"]:
        temp_f = int(round(temperature))
        return f"❄️ Awning closed: Too cold ({temp_f}°F)"

    if not conditions["sunny"]:
        # Include cloud cover details for cloudy closures
        clouds_low = int(round(cloud_cover_low))
        clouds_high = int(round(cloud_cover_high))
        return f"☁️ Awning closed: Cloudy ({clouds_low}% low, {clouds_high}% high)"

    if not conditions["daytime"]:
        return "🌙 Awning closed: Nighttime"

    if not conditions["sun_high"] or not conditions["sun_facing_window"]:
        return "🌅 Awning closed: Sun moved past window"

    # Fallback (shouldn't happen)
    return "🌙 Awning closed: Conditions changed"


def main() -> None:
    """Main entry point for awning automation."""
    # Parse command-line arguments
    dry_run = "--dry-run" in sys.argv
    env_file = None

    # Look for --env-file argument
    for i, arg in enumerate(sys.argv):
        if arg == "--env-file" and i + 1 < len(sys.argv):
            env_file = Path(sys.argv[i + 1]).expanduser()
            break
        elif arg.startswith("--env-file="):
            env_file = Path(arg.split("=", 1)[1]).expanduser()
            break

    # Setup logging with daily rotation (must happen before any logger calls)
    log_path = setup_logging(env_file)

    if dry_run:
        logger.info("Running in DRY-RUN mode (no awning control)")

    if env_file:
        logger.info(f"Using .env file: {env_file}")

    # Initialize telegram config (will be loaded in try block)
    telegram_token, telegram_chat_id = None, None

    try:
        # Load location configuration
        latitude, longitude = load_location_config(env_file)
        logger.info(f"Location: {latitude:.4f}, {longitude:.4f}")

        # Get thresholds
        wind_threshold, altitude_threshold, dni_threshold, max_low_cloud, max_mid_cloud, cirrus_high_threshold, min_dni_cirrus = get_thresholds()
        logger.info(
            f"Thresholds: DNI >= {dni_threshold:.0f} W/m² (cirrus: {min_dni_cirrus:.0f} W/m²), "
            f"Low clouds < {max_low_cloud:.0f}%, Cirrus: high > {cirrus_high_threshold:.0f}% AND mid < {max_mid_cloud:.0f}%, "
            f"Wind < {wind_threshold} mph, Rain = 0 mm/h, Temp > 32°F, "
            f"Sun altitude >= {altitude_threshold}°, Sun facing window (90°-220°)"
        )

        # Load Telegram config (optional)
        telegram_token, telegram_chat_id = load_telegram_config()
        if telegram_token:
            logger.info("Telegram notifications enabled")

        # Fetch current weather
        logger.info("Fetching weather data...")
        weather = fetch_weather(latitude, longitude)
        logger.info(
            f"Weather: DNI {weather['dni']:.0f} W/m², {weather['wind_speed_10m']} mph wind, "
            f"{weather['precipitation']} mm/h rain, {weather['temperature']}°F (at {weather['time']})"
        )
        logger.info(
            f"Cloud cover: Total {weather['cloud_cover']}%, Low {weather['cloud_cover_low']}%, "
            f"Mid {weather['cloud_cover_mid']}%, High {weather['cloud_cover_high']}%"
        )

        # Get current time from weather API (same timezone as sunrise/sunset)
        current_time_str = weather["time"]
        current_time = datetime.fromisoformat(current_time_str.replace("Z", "+00:00"))

        # Also get UTC time for sun position calculation
        current_time_utc = datetime.now(timezone.utc)
        sun_position = calculate_sun_position(latitude, longitude, current_time_utc)
        logger.info(
            f"Sun position: Azimuth {sun_position['azimuth']:.1f}°, "
            f"Altitude {sun_position['altitude']:.1f}°"
        )

        # Log sunrise/sunset
        logger.info(
            f"Daytime window: Sunrise {weather['sunrise'][11:16]}, "
            f"Sunset {weather['sunset'][11:16]}"
        )

        # Evaluate all conditions
        should_open, reason, conditions = should_open_awning(
            weather,
            sun_position,
            current_time,
            wind_threshold,
            altitude_threshold,
            dni_threshold,
            max_low_cloud,
            max_mid_cloud,
            cirrus_high_threshold,
            min_dni_cirrus,
        )

        # Log cirrus detection state
        cirrus_dominated = conditions.get("cirrus_dominated", False)
        if cirrus_dominated:
            logger.info(
                f"Cirrus override active: high clouds {weather['cloud_cover_high']:.0f}% > {cirrus_high_threshold:.0f}%, "
                f"low {weather['cloud_cover_low']:.0f}% < {max_low_cloud:.0f}%, "
                f"mid {weather['cloud_cover_mid']:.0f}% < {max_mid_cloud:.0f}% — "
                f"using DNI threshold {min_dni_cirrus:.0f} W/m² instead of {dni_threshold:.0f} W/m²"
            )
        else:
            logger.info(
                f"Normal mode (no cirrus override): using DNI threshold {dni_threshold:.0f} W/m²"
            )

        # Log conditions with checkmarks/crosses (skip cirrus_dominated — logged separately above)
        condition_symbols = {
            "sunny": "Sunny" if conditions["sunny"] else "Not sunny",
            "calm": "Calm" if conditions["calm"] else "Windy",
            "no_rain": "No rain" if conditions["no_rain"] else "Rain",
            "above_freezing": "Above freezing" if conditions["above_freezing"] else "Freezing",
            "daytime": "Daytime" if conditions["daytime"] else "Nighttime",
            "sun_high": "Sun high" if conditions["sun_high"] else "Sun low",
            "sun_facing_window": "Sun facing window" if conditions["sun_facing_window"] else "Sun not facing window",
        }
        check_str = ", ".join(
            [f"{'✓' if conditions[k] else '✗'} {condition_symbols[k]}" for k in condition_symbols]
        )
        logger.info(f"Conditions: {check_str}")
        logger.info(f"Decision: {reason}")

        # Create controller
        controller = create_controller_from_env(env_file)

        if dry_run:
            # Only check state in dry-run mode (for reporting)
            current_state = controller.get_state()
            is_open = current_state == 1
            logger.info(f"Current awning state: {'OPEN' if is_open else 'CLOSED'}")
            logger.info(f"Would set awning to: {'OPEN' if should_open else 'CLOSED'}")
            logger.info("Dry-run complete (no action taken)")
            return

        # Get state before action
        state_before = controller.get_state()

        # Always send command based on weather decision
        # This is more reliable than state-based logic, since Bond state can be out of sync
        if should_open:
            logger.info("Opening awning...")
            controller.open()
            logger.info("Awning set to OPEN")
        else:
            logger.info("Closing awning...")
            controller.close()
            logger.info("Awning set to CLOSED")

        # Get state after action and notify only if it changed
        state_after = controller.get_state()
        if telegram_token and state_before != state_after:
            msg = _format_friendly_telegram_message(
                should_open,
                conditions,
                weather["wind_speed_10m"],
                weather["precipitation"],
                weather["temperature"],
                weather["cloud_cover_low"],
                weather["cloud_cover_high"],
            )
            send_telegram_notification(telegram_token, telegram_chat_id, msg)

        logger.info("Automation complete")

        # Cleanup old log files
        retention_days = int(os.environ.get("LOG_RETENTION_DAYS", "30"))
        cleanup_old_logs(log_path.parent, retention_days)

    except ConfigurationError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except WeatherAPIError as e:
        logger.error(f"Weather API error: {e}")
        # Fail-safe: try to close awning if we can't get weather
        if not dry_run:
            logger.warning("Attempting to close awning as fail-safe...")
            try:
                controller = create_controller_from_env()
                state = controller.get_state()
                if state == 1:  # If open
                    controller.close()
                    logger.info("Awning closed as fail-safe")
                    if telegram_token:
                        msg = f"⚠️ Awning CLOSED (fail-safe)\nWeather API error: {e}"
                        send_telegram_notification(telegram_token, telegram_chat_id, msg)
                else:
                    logger.info("Awning already closed")
            except Exception as fail_safe_error:
                logger.error(f"Fail-safe close failed: {fail_safe_error}")
                if telegram_token:
                    msg = f"🚨 ALERT: Weather API failed AND fail-safe close failed!\n{fail_safe_error}"
                    send_telegram_notification(telegram_token, telegram_chat_id, msg)
        sys.exit(1)
    except BondAPIError as e:
        logger.error(f"Bond API error: {e}")
        if telegram_token:
            msg = f"🚨 Bond API error: {e}"
            send_telegram_notification(telegram_token, telegram_chat_id, msg)
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
