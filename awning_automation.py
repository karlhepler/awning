#!/usr/bin/env python3
"""
Awning Weather Automation

Automatically opens/closes awning based on weather conditions.
- Opens awning if ALL 7 conditions are met: sunny, calm, no rain, above 40°F,
  daytime, sun high enough, and sun facing window (90°-220°)
- Closes awning if ANY condition fails

Sunshine detection uses a cross-model OR gate:
  sunny_enough = (shortwave_radiation >= MIN_GHI_WM2) OR (uv_index >= MIN_UV_INDEX)

GHI (shortwave_radiation) comes from ECMWF. UV Index comes from GFS — a completely
separate NWP model. Together they provide cross-model corroboration: if either
signal says it's bright enough to matter, the awning should be open.

The awning has two jobs: block UV (relevant even on cloudy-high-UV days) AND block
heat/brightness (relevant on sunny-low-UV days). Either condition alone warrants
opening.

Smoothing: All numeric sensor inputs are passed through a weighted rolling average
(weights [4,3,2,1] for current and up to 3 cached prior readings, normalized) before
threshold evaluation, preventing false-positive opens from momentary API spikes.

Designed to run as a cron job or Kubernetes scheduled job.
"""

import json
import logging
import os
import sys
import time
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


# Default cache path; can be overridden in tests
READINGS_CACHE_PATH = Path.home() / ".config" / "awning" / "readings-cache.json"

# Keys that are stored/loaded from the readings cache
_CACHE_KEYS = ["ghi", "uv_index", "dni", "wind", "temp", "precip", "cloud_total", "cloud_low", "cloud_mid", "cloud_high"]

# Linear weights for [current, n-1, n-2, n-3] — normalized before use
_SMOOTHING_WEIGHTS = [4, 3, 2, 1]


def load_readings_cache(cache_path: Path = READINGS_CACHE_PATH) -> list[dict]:
    """
    Load prior raw sensor readings from the local cache file.

    Returns up to 3 most-recent entries (oldest first).  Gracefully returns an
    empty list if the file is absent, empty, or malformed.

    Args:
        cache_path: Path to the JSON cache file

    Returns:
        List of reading dicts (up to 3), oldest first
    """
    if not cache_path.exists():
        return []
    try:
        text = cache_path.read_text()
        data = json.loads(text)
        if not isinstance(data, list):
            return []
        # Keep only well-formed entries that have all required keys
        valid = [
            entry for entry in data
            if isinstance(entry, dict) and all(k in entry for k in _CACHE_KEYS)
        ]
        # Return at most 3 most-recent entries, oldest first
        return valid[-3:]
    except (json.JSONDecodeError, OSError, ValueError):
        return []


def save_readings_cache(
    raw_reading: dict,
    prior_entries: list[dict],
    cache_path: Path = READINGS_CACHE_PATH,
) -> None:
    """
    Append the current raw reading to the cache and trim to 3 entries.

    The cache stores only raw values (not smoothed), so each subsequent run
    can compute a fresh weighted average from actual sensor readings.

    Args:
        raw_reading: Raw weather reading dict to append (must contain all _CACHE_KEYS)
        prior_entries: Existing entries loaded from cache (up to 3)
        cache_path: Path to the JSON cache file
    """
    entry = {k: raw_reading[k] for k in _CACHE_KEYS}
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()

    updated = list(prior_entries) + [entry]
    # Keep at most 3 entries (so next run has up to 3 priors)
    updated = updated[-3:]

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(updated, indent=2))
    except OSError as e:
        logger.warning(f"Could not write readings cache to {cache_path}: {e}")


def compute_smoothed_weather(
    raw: dict,
    prior_entries: list[dict],
) -> dict:
    """
    Compute weighted rolling average over current + up to 3 prior readings.

    Weights [4, 3, 2, 1] correspond to [current, n-1, n-2, n-3].  Only as
    many weights as available readings are used, normalized so they sum to 1.

    The cache entries are expected oldest-first; the most-recent prior is
    prior_entries[-1].

    Smoothed fields: ghi, uv_index, dni, wind, temp, precip, cloud_total,
    cloud_low, cloud_mid, cloud_high.  All other fields in ``raw`` are passed
    through unchanged.

    Args:
        raw: Current weather reading (from fetch_weather())
        prior_entries: Prior raw readings from cache (oldest first, up to 3)

    Returns:
        New weather dict with smoothed numeric sensor values
    """
    # Map from cache key → weather dict key
    field_map = {
        "ghi": "shortwave_radiation",
        "uv_index": "uv_index",
        "dni": "dni",
        "wind": "wind_speed_10m",
        "temp": "temperature",
        "precip": "precipitation",
        "cloud_total": "cloud_cover",
        "cloud_low": "cloud_cover_low",
        "cloud_mid": "cloud_cover_mid",
        "cloud_high": "cloud_cover_high",
    }

    # Build ordered list of readings: newest first (current, n-1, n-2, n-3)
    # prior_entries is oldest-first, so reverse to get newest-first
    readings_newest_first = [raw] + list(reversed(prior_entries))
    n = len(readings_newest_first)

    # Take only as many weights as we have readings
    raw_weights = _SMOOTHING_WEIGHTS[:n]
    total = sum(raw_weights)
    normalized = [w / total for w in raw_weights]

    smoothed = dict(raw)  # start with a copy of raw (preserves non-numeric fields)

    for cache_key, weather_key in field_map.items():
        # Retrieve values from each reading in newest-first order
        values = []
        for i, reading in enumerate(readings_newest_first):
            if i == 0:
                # Current reading — use weather dict key
                values.append(float(reading.get(weather_key, 0)))
            else:
                # Prior cache entry — use cache key
                values.append(float(reading.get(cache_key, 0)))

        weighted_val = sum(v * w for v, w in zip(values, normalized))
        smoothed[weather_key] = weighted_val

    return smoothed


def _raw_reading_from_weather(weather: dict) -> dict:
    """
    Extract raw sensor values from a fetch_weather() result for cache storage.

    Args:
        weather: Weather dict from fetch_weather()

    Returns:
        Dict with cache keys (ghi, uv_index, dni, wind, temp, precip, cloud_total/low/mid/high)
    """
    return {
        "ghi": weather["shortwave_radiation"],
        "uv_index": weather["uv_index"],
        "dni": weather["dni"],
        "wind": weather["wind_speed_10m"],
        "temp": weather["temperature"],
        "precip": weather["precipitation"],
        "cloud_total": weather["cloud_cover"],
        "cloud_low": weather["cloud_cover_low"],
        "cloud_mid": weather["cloud_cover_mid"],
        "cloud_high": weather["cloud_cover_high"],
    }


# Number of discrete weather measurements per automation run
_MEASUREMENT_COUNT = 3
# Seconds to wait between measurements (~1 minute apart, 2 minutes total for 3 readings)
_MEASUREMENT_INTERVAL_SECONDS = 60

# Numeric fields that are averaged across measurements (all others pass through from first reading)
_AVERAGED_FIELDS = [
    "shortwave_radiation",
    "uv_index",
    "wind_speed_10m",
    "precipitation",
    "temperature",
    "dni",
    "cloud_cover",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
]


def collect_weather_measurements(lat: float, lon: float) -> dict:
    """
    Collect 3 discrete weather measurements ~1 minute apart and return averaged values.

    Makes _MEASUREMENT_COUNT separate weather API calls, sleeping
    _MEASUREMENT_INTERVAL_SECONDS between each. Each individual API call is
    retried up to _FETCH_MAX_RETRIES times with exponential backoff on failure.
    If a single measurement fails all retries, it is logged as a warning and
    skipped. Averaging is performed over whatever measurements succeeded (1, 2,
    or 3). If ALL measurements fail, WeatherAPIError is raised so the caller's
    existing fail-safe close logic is triggered.

    Non-numeric fields (sunrise, sunset, is_day, time) are taken from the
    first successful measurement.

    Args:
        lat: Latitude
        lon: Longitude

    Returns:
        Weather dict with numeric fields averaged across all successful
        measurements, non-numeric fields from the first successful measurement.

    Raises:
        WeatherAPIError: If all weather API requests fail.
    """
    # Per-measurement retry configuration (independent of tenacity)
    _FETCH_MAX_RETRIES = 3
    _FETCH_BASE_DELAY_SECONDS = 2  # Backoff: 2s, 4s, 8s

    samples = []
    prev_measurement_end_time: Optional[float] = None

    for i in range(_MEASUREMENT_COUNT):
        # Enforce the inter-measurement interval from the end of the previous
        # measurement (success or final failure). Retry backoff within a
        # measurement does not count toward this interval.
        if prev_measurement_end_time is not None:
            elapsed = time.monotonic() - prev_measurement_end_time
            remaining = _MEASUREMENT_INTERVAL_SECONDS - elapsed
            if remaining > 0:
                logger.info(
                    f"Waiting {remaining:.0f}s before measurement {i + 1} of {_MEASUREMENT_COUNT}..."
                )
                time.sleep(remaining)

        logger.info(f"Fetching weather measurement {i + 1} of {_MEASUREMENT_COUNT}...")

        sample = None
        last_error = None
        for attempt in range(1, _FETCH_MAX_RETRIES + 1):
            try:
                sample = fetch_weather(lat, lon)
                break
            except WeatherAPIError as exc:
                last_error = exc
                if attempt < _FETCH_MAX_RETRIES:
                    backoff = _FETCH_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        f"Measurement {i + 1} attempt {attempt} failed: {exc}. "
                        f"Retrying in {backoff}s (attempt {attempt + 1} of {_FETCH_MAX_RETRIES})..."
                    )
                    time.sleep(backoff)

        # Record when this measurement resolved (success or final failure).
        # The next measurement's inter-measurement wait is measured from here,
        # so retry backoff time spent above never shortens the gap.
        prev_measurement_end_time = time.monotonic()

        if sample is None:
            logger.warning(
                f"Measurement {i + 1} failed all {_FETCH_MAX_RETRIES} retry attempts — "
                f"skipping this sample. Last error: {last_error}"
            )
            continue

        samples.append(sample)

        logger.info(
            f"Sample {i + 1}: GHI {sample['shortwave_radiation']:.0f} W/m², "
            f"UV {sample['uv_index']:.1f}, "
            f"DNI (obs) {sample['dni']:.0f} W/m², "
            f"{sample['wind_speed_10m']:.1f} mph wind, "
            f"{sample['precipitation']:.2f} mm/h rain, "
            f"{sample['temperature']:.1f}°F"
        )
        logger.info(
            f"Sample {i + 1} clouds: Total {sample['cloud_cover']:.0f}%, "
            f"Low {sample['cloud_cover_low']:.0f}%, "
            f"Mid {sample['cloud_cover_mid']:.0f}%, "
            f"High {sample['cloud_cover_high']:.0f}%"
        )

    if not samples:
        raise WeatherAPIError(
            f"All {_MEASUREMENT_COUNT} weather measurements failed after "
            f"{_FETCH_MAX_RETRIES} retry attempts each."
        )

    num_samples = len(samples)
    if num_samples < _MEASUREMENT_COUNT:
        logger.warning(
            f"Only {num_samples} of {_MEASUREMENT_COUNT} measurements succeeded — "
            f"averaging over partial samples."
        )

    # Average numeric fields across all successful samples
    averaged = dict(samples[0])  # start with copy of first sample (non-numeric fields)
    for field in _AVERAGED_FIELDS:
        values = [s[field] for s in samples]
        averaged[field] = sum(values) / num_samples

    logger.info(
        f"Average ({num_samples} samples): GHI {averaged['shortwave_radiation']:.0f} W/m², "
        f"UV {averaged['uv_index']:.1f}, "
        f"DNI (obs) {averaged['dni']:.0f} W/m², "
        f"{averaged['wind_speed_10m']:.1f} mph wind, "
        f"{averaged['precipitation']:.2f} mm/h rain, "
        f"{averaged['temperature']:.1f}°F"
    )
    logger.info(
        f"Average clouds ({num_samples} samples): Total {averaged['cloud_cover']:.0f}%, "
        f"Low {averaged['cloud_cover_low']:.0f}%, "
        f"Mid {averaged['cloud_cover_mid']:.0f}%, "
        f"High {averaged['cloud_cover_high']:.0f}%"
    )

    return averaged


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


def get_thresholds() -> tuple[float, float, float, float]:
    """
    Get weather thresholds from environment variables.

    Returns:
        Tuple of (wind_speed_threshold_mph, min_sun_altitude, min_ghi, min_uv_index)
        Types: (float, float, float, float)
            wind_speed_threshold_mph: float — mph, upper wind limit to open awning
            min_sun_altitude: float — degrees above horizon, lower sun altitude limit
            min_ghi: float — W/m², minimum global horizontal irradiance (shortwave_radiation)
                to consider it sunny; 400 W/m² is the 'enough sun to matter' threshold
            min_uv_index: float — minimum UV Index (dimensionless) to consider UV significant;
                UV 3 is moderate, 6 is high — 4 is the 'you'd recommend sunscreen' threshold

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

    # Parse required thresholds
    try:
        wind_threshold = float(wind_str)
        altitude_threshold = float(altitude_str)
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

    # Get minimum GHI threshold (optional, default 400 W/m²)
    min_ghi_str = os.getenv("MIN_GHI_WM2", "400").strip()
    try:
        min_ghi = float(min_ghi_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid MIN_GHI_WM2 format: {e}. Must be a number."
        ) from e
    if min_ghi <= 0:
        raise ConfigurationError(
            f"MIN_GHI_WM2 must be > 0; a value of 0 would always trigger the GHI arm "
            f"of the sunny gate, effectively disabling it as a discriminator. "
            f"Received: {min_ghi}"
        )

    # Get minimum UV Index threshold (optional, default 4)
    min_uv_str = os.getenv("MIN_UV_INDEX", "4").strip()
    try:
        min_uv_index = float(min_uv_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid MIN_UV_INDEX format: {e}. Must be a number."
        ) from e
    if min_uv_index <= 0:
        raise ConfigurationError(
            f"MIN_UV_INDEX must be > 0; a value of 0 would always trigger the UV arm "
            f"of the sunny gate, effectively disabling it as a discriminator. "
            f"Received: {min_uv_index}"
        )

    return wind_threshold, altitude_threshold, min_ghi, min_uv_index


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
        "current": (
            "wind_speed_10m,precipitation,is_day,temperature_2m,"
            "shortwave_radiation,uv_index,direct_normal_irradiance,"
            "cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high"
        ),
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
        required_fields = ["wind_speed_10m", "precipitation", "temperature_2m", "shortwave_radiation", "uv_index"]
        for field in required_fields:
            if field not in current:
                raise WeatherAPIError(
                    f"Weather API response missing '{field}' in current data"
                )

        # Explicitly check for null values in radiation fields.
        # Open-Meteo can return JSON null for shortwave_radiation or uv_index when
        # the field is outside the model's forecast window or GFS coverage lapses.
        # The key-presence check above accepts null values, which would later crash
        # at threshold comparisons with TypeError.
        shortwave_radiation = current["shortwave_radiation"]
        uv_index = current["uv_index"]
        if shortwave_radiation is None or uv_index is None:
            raise WeatherAPIError(
                f"Weather API returned null for required field(s): "
                f"shortwave_radiation={shortwave_radiation}, uv_index={uv_index}. "
                f"This can happen when GFS coverage is unavailable outside the forecast window."
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
            "shortwave_radiation": current["shortwave_radiation"],
            "uv_index": current["uv_index"],
            # DNI kept for observability logging only — not used in decisions
            "dni": current.get("direct_normal_irradiance", 0),
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
    min_ghi: float,
    min_uv_index: float,
) -> tuple[bool, str, dict]:
    """
    Determine if awning should be open based on ALL conditions.

    Sunshine detection uses a cross-model OR gate:
      sunny_enough = (shortwave_radiation >= min_ghi) OR (uv_index >= min_uv_index)

    GHI (shortwave_radiation) comes from ECMWF. UV Index comes from GFS — a separate
    NWP model. Either signal alone is sufficient evidence of solar activity significant
    enough to warrant shade.

    Args:
        weather: Weather data from fetch_weather()
        sun_position: Sun position data from calculate_sun_position()
        current_time: Current datetime
        wind_threshold: Maximum wind speed (mph) for "calm"
        altitude_threshold: Minimum sun altitude (degrees) above horizon
        min_ghi: Minimum global horizontal irradiance (W/m²) for sunny_enough
        min_uv_index: Minimum UV Index for sunny_enough

    Returns:
        Tuple of (should_open, reason, conditions_dict)
    """
    # Extract weather data
    wind_speed = weather["wind_speed_10m"]
    precipitation = weather["precipitation"]
    temperature = weather["temperature"]
    ghi = weather["shortwave_radiation"]
    uv_index = weather["uv_index"]
    sunrise = weather["sunrise"]
    sunset = weather["sunset"]

    # Extract sun position
    azimuth = sun_position["azimuth"]
    altitude = sun_position["altitude"]

    # Evaluate sunny condition: OR gate across two independent NWP models
    ghi_sunny = ghi >= min_ghi
    uv_sunny = uv_index >= min_uv_index
    is_sunny = ghi_sunny or uv_sunny

    is_calm = wind_speed < wind_threshold
    no_rain = precipitation == 0
    above_freezing = temperature > 40
    is_day = is_daytime(current_time, sunrise, sunset)
    sun_high_enough = altitude >= altitude_threshold
    sun_facing_se = is_sun_facing_window(azimuth)

    conditions = {
        "sunny": is_sunny,
        "calm": is_calm,
        "no_rain": no_rain,
        "above_freezing": above_freezing,
        "daytime": is_day,
        "sun_high": sun_high_enough,
        "sun_facing_window": sun_facing_se,
    }

    should_open = all(conditions.values())

    # Build sunny signal trace for logging
    if is_sunny:
        if ghi_sunny and uv_sunny:
            sunny_trace = f"GHI {ghi:.0f} W/m² >= {min_ghi:.0f} AND UV {uv_index:.1f} >= {min_uv_index:.1f}"
        elif ghi_sunny:
            sunny_trace = f"GHI only: GHI {ghi:.0f} W/m² >= {min_ghi:.0f}, UV {uv_index:.1f} < {min_uv_index:.1f}"
        else:
            sunny_trace = f"UV only: UV {uv_index:.1f} >= {min_uv_index:.1f}, GHI {ghi:.0f} W/m² < {min_ghi:.0f}"
    else:
        sunny_trace = f"GHI {ghi:.0f} W/m² < {min_ghi:.0f} AND UV {uv_index:.1f} < {min_uv_index:.1f}"

    # Build detailed reason string
    reasons = []
    if not is_sunny:
        reasons.append(f"Not sunny: {sunny_trace}")
    if not is_calm:
        reasons.append(f"Too windy ({wind_speed} >= {wind_threshold} mph)")
    if not no_rain:
        reasons.append(f"Raining ({precipitation} mm/h)")
    if not above_freezing:
        reasons.append(f"Too cold ({temperature}°F <= 40°F)")
    if not is_day:
        reasons.append(
            f"Nighttime (sunrise {sunrise[11:16]}, sunset {sunset[11:16]})"
        )
    if not sun_high_enough:
        reasons.append(f"Sun too low ({altitude:.1f}° < {altitude_threshold}°)")
    if not sun_facing_se:
        reasons.append(f"Sun not facing window (azimuth {azimuth:.1f}°, need 90°-220°)")

    if should_open:
        reason = (
            f"All conditions met: Sunny ({sunny_trace}), "
            f"wind {wind_speed} mph, rain {precipitation} mm/h, {temperature}°F, "
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
    ghi: float,
    uv_index: float,
) -> str:
    """
    Format a human-friendly Telegram notification message.

    Args:
        should_open: Whether awning should be open
        conditions: Dictionary of condition flags
        wind_speed: Wind speed in mph
        precipitation: Precipitation in mm/h
        temperature: Temperature in F
        ghi: Global horizontal irradiance (shortwave_radiation) in W/m²
        uv_index: UV Index (dimensionless)

    Returns:
        Friendly message string with appropriate emoji
    """
    if should_open:
        # Opening message - simple and positive
        temp_f = int(round(temperature))
        wind_mph = int(round(wind_speed))
        return f"☀️ Awning opened - sunny & calm ({temp_f}°F, {wind_mph} mph wind)"

    # Closing message - determine primary reason and emoji
    # Priority: rain > wind > cold > not sunny > nighttime > sun position

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
        # Report causal values (GHI and UV), not cloud_cover percentages.
        # cloud_cover is now observational only and is NOT a decision input —
        # the sunny gate is driven by GHI (ECMWF) and UV Index (GFS).
        return f"☁️ Awning closed: Not enough sun (GHI {ghi:.0f} W/m², UV {uv_index:.1f})"

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
        wind_threshold, altitude_threshold, min_ghi, min_uv_index = get_thresholds()
        logger.info(
            f"Thresholds: GHI >= {min_ghi:.0f} W/m² OR UV >= {min_uv_index:.1f}, "
            f"Wind < {wind_threshold} mph, Rain = 0 mm/h, Temp > 40°F, "
            f"Sun altitude >= {altitude_threshold}°, Sun facing window (90°-220°)"
        )

        # Load Telegram config (optional)
        telegram_token, telegram_chat_id = load_telegram_config()
        if telegram_token:
            logger.info("Telegram notifications enabled")

        # Collect 3 weather measurements ~1 minute apart, then use their average
        logger.info(
            f"Collecting {_MEASUREMENT_COUNT} weather measurements "
            f"({_MEASUREMENT_INTERVAL_SECONDS}s apart)..."
        )
        weather = collect_weather_measurements(latitude, longitude)

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

        # Log observational values (not used in decisions, for validation)
        logger.info(
            f"Observational (not used in decisions): "
            f"DNI {weather['dni']:.0f} W/m², "
            f"cloud_cover_low {weather['cloud_cover_low']:.0f}%, "
            f"cloud_cover_mid {weather['cloud_cover_mid']:.0f}%, "
            f"cloud_cover_high {weather['cloud_cover_high']:.0f}%"
        )

        # Evaluate all conditions
        should_open, reason, conditions = should_open_awning(
            weather,
            sun_position,
            current_time,
            wind_threshold,
            altitude_threshold,
            min_ghi,
            min_uv_index,
        )

        # Log conditions with checkmarks/crosses
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
                weather["shortwave_radiation"],
                weather["uv_index"],
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
