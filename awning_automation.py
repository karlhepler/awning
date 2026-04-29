#!/usr/bin/env python3
"""
Awning Weather Automation

Automatically opens/closes awning based on weather conditions.
- Opens awning if ALL 7 conditions are met: sunny, calm, no rain, above 60°F,
  daytime, sun high enough, and sun facing window (90°-260°)
- Closes awning if ANY condition fails

Sunshine detection uses a two-layer gate plus a hard cloud-cover ceiling:

  Layer 1 — model forecast ('do we want shade?'):
    sunny_model = (shortwave_radiation >= MIN_GHI_WM2) OR (uv_index >= MIN_UV_INDEX)
    GHI (shortwave_radiation) comes from ECMWF. UV Index comes from GFS — a completely
    separate NWP model. Either signal alone is sufficient: the awning has two jobs —
    block UV (relevant even on cloudy-high-UV days) AND block heat/brightness.

  Layer 2 — multi-variable consistency check (independent model variable cross-check):
    sunny_observed = (direct_normal_irradiance >= MIN_DNI_WM2) OR (cloud_cover < MAX_CLOUD_COVER_PCT)
    DNI and cloud_cover come from different NWP model schemes:
      - DNI/GHI: derived from the model's radiative transfer scheme
      - cloud_cover: derived from the model's humidity field (Sundqvist 1989)
    When they disagree, one is wrong — the OR allows either to confirm sunny,
    preventing false-closes when DNI is intermittent on partly-cloudy days.

  Layer 3 — hard cloud-cover ceiling (overcast override):
    not_overcast = max(cloud_cover_mid, cloud_cover_high) < OVERCAST_THRESHOLD_PCT
    When max(cloud_cover_mid, cloud_cover_high) is very high (≥95%), the sky has
    optically thick cloud cover that blocks direct sun. Both mid-level (altostratus/
    altocumulus) and high-level (cirrostratus) clouds can independently cause full
    overcast. The 2026-04-28 incident confirmed that cloud_cover_high=99% with
    cloud_cover_mid=53-70% fully blocked the sun while the awning remained open.
    Using max() ensures either layer alone is sufficient to fire the ceiling gate.

  sunny_enough = sunny_model AND sunny_observed AND not_overcast

All three layers must agree before the awning opens.

Each cron run makes a single weather API call. Open-Meteo caches responses within
sub-minute windows, so the 15-minute cron cadence is the effective sampling interval.

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
from requests.adapters import HTTPAdapter
# NOTE: tenacity is retained for Telegram POST retries; weather/Bond migrated to urllib3.Retry.
# The split is intentional — POST methods require additional urllib3 Retry config we don't need elsewhere.
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from urllib3.util.retry import Retry

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

# Weather API retry configuration
# Retries on 5xx server errors (including 503), 429 rate-limit, and
# connection-level errors with exponential backoff.
# Approximate retry delays: 0s, 2s, 4s, 8s, 16s (wall-clock cap ~30s).
_WEATHER_RETRY_TOTAL = 5
_WEATHER_RETRY_BACKOFF_FACTOR = 1.0
_WEATHER_RETRY_STATUS_FORCELIST = [429, 500, 502, 503, 504]


class _WeatherLoggingRetry(Retry):
    """Retry subclass that logs each weather API retry attempt at WARNING level."""

    def new(self, **kw):
        # Mirror _LoggingRetry.new() for defensive consistency: urllib3 calls
        # new() to produce successive Retry objects in the retry chain. There is
        # no instance state to propagate here today, but overriding keeps the
        # pattern symmetric with _LoggingRetry and prevents a latent trap if
        # state is added later.
        return super().new(**kw)

    def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
        attempt_num = len(self.history) + 1

        if response is not None:
            status = response.status
            logger.warning(
                f"weather API returned {status}, retrying "
                f"(attempt {attempt_num}/{_WEATHER_RETRY_TOTAL}) ..."
            )
        elif error is not None:
            logger.warning(
                f"weather API connection error ({error}), retrying "
                f"(attempt {attempt_num}/{_WEATHER_RETRY_TOTAL}) ..."
            )

        return super().increment(
            method=method,
            url=url,
            response=response,
            error=error,
            _pool=_pool,
            _stacktrace=_stacktrace,
        )


def _make_weather_session() -> requests.Session:
    """
    Create a requests.Session with exponential-backoff retry for the Open-Meteo API.

    Retries on 5xx server errors (including 503), 429 rate-limit, and
    connection-level errors. GET-only — Open-Meteo uses GET for all requests.

    Approximate retry delays: 0s, 2s, 4s, 8s, 16s (wall-clock cap ~30s).
    """
    retry = _WeatherLoggingRetry(
        total=_WEATHER_RETRY_TOTAL,
        backoff_factor=_WEATHER_RETRY_BACKOFF_FACTOR,
        status_forcelist=_WEATHER_RETRY_STATUS_FORCELIST,
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,  # let raise_for_status() decide after retries
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# Module-level session shared across all weather API calls in this process.
# Tests may replace _weather_session.get via patch.object().
_weather_session = _make_weather_session()

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


def collect_weather_measurements(lat: float, lon: float) -> dict:
    """
    Fetch a single weather measurement from Open-Meteo.

    The cron job runs every 15 minutes — that cadence is the sampling interval.
    Open-Meteo caches API responses within sub-minute windows, so multiple calls
    spaced 1 minute apart return identical values. A single call per cron run is
    sufficient and avoids wasted work.

    The underlying fetch_weather() call is retried by tenacity with exponential
    backoff on transient network errors (ConnectionError, Timeout,
    ChunkedEncodingError) — see WEATHER_RETRY_CONFIG.

    Args:
        lat: Latitude
        lon: Longitude

    Returns:
        Weather dict from fetch_weather().

    Raises:
        WeatherAPIError: If the weather API request fails.
    """
    logger.info("Fetching weather measurement...")
    weather = fetch_weather(lat, lon)
    logger.info(
        f"Weather: GHI {weather['shortwave_radiation']:.0f} W/m², "
        f"UV {weather['uv_index']:.1f}, "
        f"DNI {weather['dni']:.0f} W/m², "
        f"{weather['wind_speed_10m']:.1f} mph wind, "
        f"{weather['precipitation']:.2f} mm/h rain, "
        f"{weather['temperature']:.1f}°F"
    )
    logger.info(
        f"Clouds: Total {weather['cloud_cover']:.0f}%, "
        f"Low {weather['cloud_cover_low']:.0f}%, "
        f"Mid {weather['cloud_cover_mid']:.0f}%, "
        f"High {weather['cloud_cover_high']:.0f}%"
    )
    return weather


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


def get_thresholds() -> tuple[float, float, float, float, float, float, float, float]:
    """
    Get weather thresholds from environment variables.

    Returns:
        Tuple of (wind_speed_threshold_mph, min_sun_altitude, min_ghi, min_uv_index,
                  min_dni, max_cloud_cover, min_temperature_f, overcast_threshold)
        Types: (float, float, float, float, float, float, float, float)
            wind_speed_threshold_mph: float — mph, upper wind limit to open awning
            min_sun_altitude: float — degrees above horizon, lower sun altitude limit
            min_ghi: float — W/m², minimum global horizontal irradiance (shortwave_radiation)
                to consider it sunny; 400 W/m² is the 'enough sun to matter' threshold
            min_uv_index: float — minimum UV Index (dimensionless) to consider UV significant;
                UV 3 is moderate, 6 is high — 4 is the 'you'd recommend sunscreen' threshold
            min_dni: float — W/m², minimum direct normal irradiance (NWP radiative
                transfer scheme) for Layer 2 consistency check; 50 W/m² is above
                overcast/rain (4-14) but well below typical clear-sky values (300-900)
            max_cloud_cover: float — % maximum total cloud cover (NWP humidity-based
                scheme) for Layer 2 consistency check; 80% allows partly-cloudy opens
                while blocking most overcast days
            min_temperature_f: float — °F minimum temperature to open awning; 60°F is
                the 'warm enough to want shade' threshold
            overcast_threshold: float — % cloud cover ceiling (Layer 3 hard override);
                when cloud_cover >= this value, DNI is overridden and awning stays closed;
                95% is above MAX_CLOUD_COVER_PCT (80%) so it only fires for true overcast

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

    # Get minimum DNI threshold (optional, default 50 W/m²)
    # DNI comes from the NWP model's radiative transfer scheme — 4-14 W/m² during rain/overcast
    min_dni_str = os.getenv("MIN_DNI_WM2", "50").strip()
    try:
        min_dni = float(min_dni_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid MIN_DNI_WM2 format: {e}. Must be a number."
        ) from e
    if min_dni < 0:
        raise ConfigurationError(
            f"MIN_DNI_WM2 must be >= 0, got: {min_dni}"
        )

    # Get maximum cloud cover threshold (optional, default 80%)
    # Layer 2 consistency check: allows partly-cloudy opens while blocking most overcast days
    max_cloud_cover_str = os.getenv("MAX_CLOUD_COVER_PCT", "80").strip()
    try:
        max_cloud_cover = float(max_cloud_cover_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid MAX_CLOUD_COVER_PCT format: {e}. Must be a number."
        ) from e
    if not (0 <= max_cloud_cover <= 100):
        raise ConfigurationError(
            f"MAX_CLOUD_COVER_PCT must be between 0 and 100, got: {max_cloud_cover}"
        )

    # Get minimum temperature threshold (optional, default 60°F)
    # 60°F is the 'warm enough to want shade' threshold.
    # Prior default was 40°F (safety-above-freezing), which allowed opens in cold weather.
    min_temperature_str = os.getenv("MIN_TEMPERATURE_F", "60").strip()
    try:
        min_temperature_f = float(min_temperature_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid MIN_TEMPERATURE_F format: {e}. Must be a number."
        ) from e
    if not (-50 <= min_temperature_f <= 120):
        raise ConfigurationError(
            f"MIN_TEMPERATURE_F must be between -50 and 120°F, got: {min_temperature_f}"
        )

    # Get overcast threshold (optional, default 95%)
    # Layer 3 hard ceiling: when cloud_cover_mid >= this value, DNI is overridden and
    # awning stays closed regardless of DNI. Uses MID-level cloud cover (altostratus/
    # altocumulus), NOT total cloud cover. Total saturates to 100% when high cirrus
    # is present even when the sun is visibly shining (cirrus is thin and does not
    # block awning-relevant sun). Mid-level clouds are the optical layer that
    # determines whether direct sun reaches the ground. Set above MAX_CLOUD_COVER_PCT
    # (80%) so it only fires for true overcast cases (95-100%), not partly-cloudy days.
    overcast_threshold_str = os.getenv("OVERCAST_THRESHOLD_PCT", "95").strip()
    try:
        overcast_threshold = float(overcast_threshold_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid OVERCAST_THRESHOLD_PCT format: {e}. Must be a number."
        ) from e
    if not (0 <= overcast_threshold <= 100):
        raise ConfigurationError(
            f"OVERCAST_THRESHOLD_PCT must be between 0 and 100, got: {overcast_threshold}"
        )

    return wind_threshold, altitude_threshold, min_ghi, min_uv_index, min_dni, max_cloud_cover, min_temperature_f, overcast_threshold


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

        # Explicitly check for null values in decision-input fields.
        # Open-Meteo can return JSON null for numeric fields when the model's
        # forecast window or GFS/ECMWF coverage lapses. The key-presence check
        # above accepts null values, which would later crash at threshold
        # comparisons with TypeError — bypassing the fail-safe close path.
        # Guard all four fields used in the sunny gate (Layer 1: GHI, UV;
        # Layer 2: DNI, cloud_cover).
        shortwave_radiation = current["shortwave_radiation"]
        uv_index = current["uv_index"]
        direct_normal_irradiance = current.get("direct_normal_irradiance")
        cloud_cover_val = current.get("cloud_cover")
        if shortwave_radiation is None or uv_index is None:
            raise WeatherAPIError(
                f"Weather API returned null for required field(s): "
                f"shortwave_radiation={shortwave_radiation}, uv_index={uv_index}. "
                f"This can happen when GFS coverage is unavailable outside the forecast window."
            )
        if direct_normal_irradiance is None or cloud_cover_val is None:
            raise WeatherAPIError(
                f"Weather API returned null for cross-check field(s): "
                f"direct_normal_irradiance={direct_normal_irradiance}, cloud_cover={cloud_cover_val}. "
                f"Cannot evaluate Layer 2/3 sunny gate without these values."
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


def _fetch_weather_request(url: str, params: dict, timeout: int) -> dict:
    """Make a GET request to weather API using the retry-equipped session."""
    response = _weather_session.get(url, params=params, timeout=timeout)
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
        True if azimuth is between 90° (East) and 260° (West)
    """
    return 90 <= azimuth <= 260


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
    min_dni: float = 50.0,
    max_cloud_cover: float = 80.0,
    min_temperature_f: float = 60.0,
    overcast_threshold: float = 95.0,
) -> tuple[bool, str, dict]:
    """
    Determine if awning should be open based on ALL conditions.

    Sunshine detection uses a two-layer gate plus a hard cloud-cover ceiling:

      Layer 1 — model forecast ('do we want shade?'):
        sunny_model = (shortwave_radiation >= min_ghi) OR (uv_index >= min_uv_index)
        GHI comes from ECMWF; UV Index from GFS — cross-model OR gate.

      Layer 2 — multi-variable consistency check (independent model variable cross-check):
        sunny_observed = (dni >= min_dni) OR (cloud_cover < max_cloud_cover)
        DNI and cloud_cover come from different NWP model schemes:
          - DNI/GHI: derived from the model's radiative transfer scheme
          - cloud_cover: derived from the model's humidity field (Sundqvist 1989)
        The OR allows either to confirm sunny — preventing false-closes when DNI
        is intermittent on partly-cloudy days and cloud_cover is wrongly high.

      Layer 3 — hard cloud-cover ceiling (overcast override):
        not_overcast = max(cloud_cover_mid, cloud_cover_high) < overcast_threshold
        When max(cloud_cover_mid, cloud_cover_high) >= overcast_threshold (default 95%),
        DNI is overridden. Both mid-level (altostratus/altocumulus) and high-level
        (cirrostratus) clouds can independently produce full optical overcast — the
        2026-04-28 incident confirmed this: cloud_cover_high=99% with cloud_cover_mid
        at 53-70% fully blocked the sun. max() ensures either layer fires the ceiling.
        Total cloud cover is NOT used because it saturates to 100% for any cirrus,
        making it too aggressive for partly-cloudy conditions.

      sunny_enough = sunny_model AND sunny_observed AND not_overcast

    Args:
        weather: Weather data from fetch_weather()
        sun_position: Sun position data from calculate_sun_position()
        current_time: Current datetime
        wind_threshold: Maximum wind speed (mph) for "calm"
        altitude_threshold: Minimum sun altitude (degrees) above horizon
        min_ghi: Minimum global horizontal irradiance (W/m²) for sunny_model
        min_uv_index: Minimum UV Index for sunny_model
        min_dni: Minimum direct normal irradiance (W/m²) for sunny_observed (Layer 2)
        max_cloud_cover: Maximum total cloud cover (%) for sunny_observed (Layer 2)
        min_temperature_f: Minimum temperature (°F) to open awning
        overcast_threshold: Cloud cover ceiling (%) for not_overcast (Layer 3)

    Returns:
        Tuple of (should_open, reason, conditions_dict)
    """
    # Extract weather data
    wind_speed = weather["wind_speed_10m"]
    precipitation = weather["precipitation"]
    temperature = weather["temperature"]
    ghi = weather["shortwave_radiation"]
    uv_index = weather["uv_index"]
    dni = weather.get("dni", 0.0)
    cloud_cover = weather.get("cloud_cover", 100.0)
    cloud_cover_mid = weather.get("cloud_cover_mid", 100.0)
    cloud_cover_high = weather.get("cloud_cover_high", 100.0)
    sunrise = weather["sunrise"]
    sunset = weather["sunset"]

    # Extract sun position
    azimuth = sun_position["azimuth"]
    altitude = sun_position["altitude"]

    # Layer 1: model forecast — 'do we want shade?' (GHI or UV above threshold)
    ghi_sunny = ghi >= min_ghi
    uv_sunny = uv_index >= min_uv_index
    sunny_model = ghi_sunny or uv_sunny

    # Layer 2: multi-variable consistency check — independent model variable cross-check
    # DNI and cloud_cover come from different NWP model schemes:
    #   - DNI/GHI: radiative transfer scheme
    #   - cloud_cover: humidity-based scheme (Sundqvist 1989)
    # The OR allows either to confirm sunny, preventing false-closes when DNI is
    # intermittent on partly-cloudy days and cloud_cover is wrongly high.
    dni_sunny = dni >= min_dni
    cloud_sunny = cloud_cover < max_cloud_cover
    sunny_observed = dni_sunny or cloud_sunny

    # Layer 3: hard cloud-cover ceiling — overcast override
    # max(cloud_cover_mid, cloud_cover_high) fires the ceiling if EITHER mid-level
    # (altostratus/altocumulus) OR high-level (cirrostratus) clouds are thick enough.
    # The 2026-04-28 incident confirmed that cloud_cover_high=99% with cloud_cover_mid
    # at 53-70% fully blocked direct sun. Total cloud cover is NOT used because it
    # saturates to 100% with any cirrus, making it too aggressive.
    not_overcast = max(cloud_cover_mid, cloud_cover_high) < overcast_threshold

    is_sunny = sunny_model and sunny_observed and not_overcast

    is_calm = wind_speed < wind_threshold
    no_rain = precipitation == 0
    above_freezing = temperature > min_temperature_f
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

    # Build sunny signal trace for logging (three-layer gate)
    # Layer 1: model forecast signal
    if sunny_model:
        if ghi_sunny and uv_sunny:
            model_trace = f"GHI {ghi:.0f} W/m² >= {min_ghi:.0f} AND UV {uv_index:.1f} >= {min_uv_index:.1f}"
        elif ghi_sunny:
            model_trace = f"GHI only: GHI {ghi:.0f} W/m² >= {min_ghi:.0f}, UV {uv_index:.1f} < {min_uv_index:.1f}"
        else:
            model_trace = f"UV only: UV {uv_index:.1f} >= {min_uv_index:.1f}, GHI {ghi:.0f} W/m² < {min_ghi:.0f}"
    else:
        model_trace = f"GHI {ghi:.0f} W/m² < {min_ghi:.0f} AND UV {uv_index:.1f} < {min_uv_index:.1f}"

    # Layer 2: multi-variable consistency check signal
    if sunny_observed:
        if dni_sunny and cloud_sunny:
            obs_trace = f"DNI {dni:.0f} W/m² >= {min_dni:.0f} AND cloud {cloud_cover:.0f}% < {max_cloud_cover:.0f}%"
        elif dni_sunny:
            obs_trace = f"DNI {dni:.0f} W/m² >= {min_dni:.0f} (cloud {cloud_cover:.0f}% >= {max_cloud_cover:.0f}%)"
        else:
            obs_trace = f"cloud {cloud_cover:.0f}% < {max_cloud_cover:.0f}% (DNI {dni:.0f} W/m² < {min_dni:.0f})"
    else:
        obs_trace = f"DNI {dni:.0f} W/m² < {min_dni:.0f} AND cloud {cloud_cover:.0f}% >= {max_cloud_cover:.0f}%"

    # Layer 3: hard cloud-cover ceiling
    _overcast_driver = max(cloud_cover_mid, cloud_cover_high)
    overcast_trace = f"max(cloud_mid={cloud_cover_mid:.0f}%,cloud_high={cloud_cover_high:.0f}%)={_overcast_driver:.0f}% {'<' if not_overcast else '>='} {overcast_threshold:.0f}% ceiling"

    if is_sunny:
        sunny_trace = f"model=({model_trace}), consistency=({obs_trace}), overcast=({overcast_trace})"
    elif not sunny_model:
        sunny_trace = f"model failed: {model_trace}"
    elif not sunny_observed:
        sunny_trace = f"observed failed: {obs_trace} (model ok: {model_trace})"
    else:
        sunny_trace = f"overcast ceiling blocked: {overcast_trace} (model ok: {model_trace}, consistency ok: {obs_trace})"

    # Build detailed reason string
    reasons = []
    if not is_sunny:
        reasons.append(f"Not sunny: {sunny_trace}")
    if not is_calm:
        reasons.append(f"Too windy ({wind_speed} >= {wind_threshold} mph)")
    if not no_rain:
        reasons.append(f"Raining ({precipitation} mm/h)")
    if not above_freezing:
        reasons.append(f"Too cold ({temperature}°F <= {min_temperature_f:.0f}°F)")
    if not is_day:
        reasons.append(
            f"Nighttime (sunrise {sunrise[11:16]}, sunset {sunset[11:16]})"
        )
    if not sun_high_enough:
        reasons.append(f"Sun too low ({altitude:.1f}° < {altitude_threshold}°)")
    if not sun_facing_se:
        reasons.append(f"Sun not facing window (azimuth {azimuth:.1f}°, need 90°-260°)")

    if should_open:
        reason = (
            f"All conditions met: Sunny ({sunny_trace}), "
            f"wind {wind_speed} mph, rain {precipitation} mm/h, {temperature}°F, "
            f"sun azimuth {azimuth:.1f}° (altitude {altitude:.1f}°)"
        )
    else:
        reason = ", ".join(reasons)

    return should_open, reason, conditions


def build_close_reason(
    conditions: dict,
    wind_speed: float,
    precipitation: float,
    temperature: float,
    ghi: float,
    uv_index: float,
    dni: float,
    cloud_cover: float,
) -> str:
    """
    Build a human-readable close reason string for Telegram notifications.

    Surfaces all four sunny-gate inputs (GHI, UV from Layer 1; DNI, cloud_cover
    from Layer 2) when the sunny condition is the blocking reason, so the message
    clearly indicates which layer failed without needing to consult the full log.

    Priority order: rain > wind > cold > not sunny > nighttime > sun position.
    """
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
        return (
            f"☁️ Awning closed: Not enough sun "
            f"(GHI {ghi:.0f} W/m², UV {uv_index:.1f}, "
            f"DNI {dni:.0f} W/m², cloud {cloud_cover:.0f}%)"
        )

    if not conditions["daytime"]:
        return "🌙 Awning closed: Nighttime"

    if not conditions["sun_high"] or not conditions["sun_facing_window"]:
        return "🌅 Awning closed: Sun moved past window"

    # Fallback (shouldn't happen)
    return "🌙 Awning closed: Conditions changed"


def _format_friendly_telegram_message(
    should_open: bool,
    conditions: dict,
    wind_speed: float,
    precipitation: float,
    temperature: float,
    ghi: float,
    uv_index: float,
    dni: float = 0.0,
    cloud_cover: float = 100.0,
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
        dni: Direct normal irradiance in W/m² (Layer 2 observational gate)
        cloud_cover: Total cloud cover percentage (Layer 2 observational gate)

    Returns:
        Friendly message string with appropriate emoji
    """
    if should_open:
        # Opening message - simple and positive
        temp_f = int(round(temperature))
        wind_mph = int(round(wind_speed))
        return f"☀️ Awning opened - sunny & calm ({temp_f}°F, {wind_mph} mph wind)"

    return build_close_reason(
        conditions, wind_speed, precipitation, temperature,
        ghi, uv_index, dni, cloud_cover,
    )


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
        wind_threshold, altitude_threshold, min_ghi, min_uv_index, min_dni, max_cloud_cover, min_temperature_f, overcast_threshold = get_thresholds()
        logger.info(
            f"Thresholds: model=(GHI >= {min_ghi:.0f} W/m² OR UV >= {min_uv_index:.1f}), "
            f"consistency=(DNI >= {min_dni:.0f} W/m² OR cloud < {max_cloud_cover:.0f}%), "
            f"overcast ceiling=cloud < {overcast_threshold:.0f}%, "
            f"Wind < {wind_threshold} mph, Rain = 0 mm/h, Temp > {min_temperature_f:.0f}°F, "
            f"Sun altitude >= {altitude_threshold}°, Sun facing window (90°-260°)"
        )

        # Load Telegram config (optional)
        telegram_token, telegram_chat_id = load_telegram_config()
        if telegram_token:
            logger.info("Telegram notifications enabled")

        # Fetch current weather (cron runs every 15 minutes — that's the sampling cadence)
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

        # Log consistency-check values (DNI from radiative transfer scheme;
        # cloud_cover from humidity-based scheme — independent model variables)
        logger.info(
            f"Cross-check: "
            f"DNI {weather['dni']:.0f} W/m², "
            f"cloud_cover {weather['cloud_cover']:.0f}% total "
            f"(low {weather['cloud_cover_low']:.0f}%, "
            f"mid {weather['cloud_cover_mid']:.0f}%, "
            f"high {weather['cloud_cover_high']:.0f}%)"
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
            min_dni,
            max_cloud_cover,
            min_temperature_f,
            overcast_threshold,
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
                weather.get("dni", 0.0),
                weather.get("cloud_cover", 100.0),
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
