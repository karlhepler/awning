#!/usr/bin/env python3
"""
Awning Weather Automation

Automatically opens/closes awning based on weather conditions.
- Opens awning if ALL 7 conditions are met: sunny, calm, no rain, above 45°F,
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

  Layer 3 — hard cloud-cover ceiling with DNI guard (overcast override):
    not_overcast = (max(cloud_cover_mid, cloud_cover_high) < OVERCAST_THRESHOLD_PCT)
                   OR (dni >= MIN_DNI_CIRRUS_WM2)
    When max(cloud_cover_mid, cloud_cover_high) is very high (≥95%), the sky has
    optically thick cloud cover that blocks direct sun. Both mid-level (altostratus/
    altocumulus) and high-level (cirrostratus) clouds can independently cause full
    overcast. The 2026-04-28 incident confirmed that cloud_cover_high=99% with
    cloud_cover_mid=53-70% fully blocked the sun while the awning remained open.
    Using max() ensures either layer alone is sufficient to fire the ceiling gate.
    The DNI guard (MIN_DNI_CIRRUS_WM2, default 30 W/m²) bypasses the ceiling when
    direct sun is demonstrably arriving — the 2026-05-12 incident showed Open-Meteo
    cloud_cover_high=100% (bad model data) closing the awning at DNI=905 W/m² during
    peak midday sun. When DNI is above the guard threshold, irradiance wins over the
    cloud-cover estimate.

  sunny_enough = sunny_model AND sunny_observed AND not_overcast

All three layers must agree before the awning opens.

Each cron run makes a single weather API call. Open-Meteo caches responses within
sub-minute windows, so the 15-minute cron cadence is the effective sampling interval.

Designed to run as a cron job or Kubernetes scheduled job.
"""

import io
import json
import logging
import math
import os
import sys
import tempfile
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


def get_thresholds() -> tuple[float, float, float, float, float, float, float, float, float, int, int]:
    """
    Get weather thresholds from environment variables.

    Returns:
        Tuple of (wind_speed_threshold_mph, min_sun_altitude, min_ghi, min_uv_index,
                  min_dni, max_cloud_cover, min_temperature_f, overcast_threshold,
                  min_dni_cirrus, rain_probability_threshold, open_vote_threshold)
        Types: (float, float, float, float, float, float, float, float, float, int, int)
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
            min_temperature_f: float — °F minimum temperature to open awning; 45°F is
                the cool-but-pleasant threshold
            overcast_threshold: float — % cloud cover ceiling (Layer 3 hard override);
                when cloud_cover >= this value, DNI is overridden and awning stays closed;
                95% is above MAX_CLOUD_COVER_PCT (80%) so it only fires for true overcast
            min_dni_cirrus: float — W/m², DNI guard for Layer 3 overcast ceiling; when
                DNI >= this value, the overcast ceiling is bypassed (direct sun is arriving
                despite high cloud_cover_high model estimate); 30 W/m² is well above
                overcast/rain (4-14) and low enough to catch any real direct-beam sun
            rain_probability_threshold: int — % ensemble precipitation probability above
                which the rain gate closes the awning even when current precipitation = 0;
                20% means "if 6+ of 30 ensemble members predict rain, stay closed"
            open_vote_threshold: int — number of consecutive "open" votes required before
                the awning actually opens; close votes are immediate (reset to 0); default 2
                means two consecutive 15-min cron runs must agree before opening

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

    # Get minimum temperature threshold (optional, default 45°F)
    # 45°F is the cool-but-pleasant threshold.
    # Prior default was 60°F (warm enough to want shade), which closed on cool-but-pleasant days.
    min_temperature_str = os.getenv("MIN_TEMPERATURE_F", "45").strip()
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

    # Get minimum DNI threshold for cirrus guard (optional, default 30 W/m²)
    # Layer 3 DNI guard: when DNI >= this value, the overcast ceiling is bypassed
    # because direct sun is demonstrably arriving despite high cloud_cover_high model
    # estimates (which are unreliable for thin cirrus). 30 W/m² is well above
    # overcast/rain (4-14 W/m²) but low enough to catch any real direct-beam sun.
    # The 2026-05-12 incident: cloud_cover_high=100% (bad model data) with DNI=905 W/m²
    # closed the awning during peak midday sun. This guard prevents that false positive.
    min_dni_cirrus_str = os.getenv("MIN_DNI_CIRRUS_WM2", "30").strip()
    try:
        min_dni_cirrus = float(min_dni_cirrus_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid MIN_DNI_CIRRUS_WM2 format: {e}. Must be a number."
        ) from e
    if min_dni_cirrus <= 0:
        raise ConfigurationError(
            f"MIN_DNI_CIRRUS_WM2 must be > 0; a value of 0 would make the DNI guard "
            f"always True (DNI is always >= 0), effectively disabling the Layer 3 "
            f"overcast ceiling entirely. Received: {min_dni_cirrus}"
        )
    if min_dni_cirrus > min_dni:
        raise ConfigurationError(
            f"MIN_DNI_CIRRUS_WM2 ({min_dni_cirrus}) must be <= MIN_DIRECT_IRRADIANCE_WM2 "
            f"({min_dni}). The Layer 3 DNI guard threshold must be at or below the "
            f"Layer 2 DNI threshold; otherwise the guard fires for a narrower range "
            f"than Layer 2, which is logically inconsistent."
        )

    # Get rain probability threshold (optional, default 20%)
    # Ensemble-based gate: if hourly.precipitation_probability >= this value,
    # the rain gate closes the awning even when current precipitation == 0.
    # 20% means "if 6+ of 30 ensemble members predict rain, stay closed."
    # Conservative by design — the 2026-06-23 incident showed that a single
    # current.precipitation field can be 0 during active rain when the model
    # was initialized before the convective cell formed.
    rain_prob_str = os.getenv("RAIN_PROBABILITY_THRESHOLD", "20").strip()
    try:
        rain_probability_threshold = int(rain_prob_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid RAIN_PROBABILITY_THRESHOLD format: {e}. Must be an integer."
        ) from e
    if not (0 <= rain_probability_threshold <= 100):
        raise ConfigurationError(
            f"RAIN_PROBABILITY_THRESHOLD must be between 0 and 100, got: {rain_probability_threshold}"
        )

    # Get open-vote threshold for anti-flapping hysteresis (optional, default 2)
    # Consecutive "open" votes required before the awning actually opens.
    # A close vote always fires immediately (safety-critical) and resets the counter.
    # N=2 covers one noisy blip at 15-min cadence (30-min confirmation window).
    # N=1 disables hysteresis (every open vote triggers immediately).
    open_vote_str = os.getenv("OPEN_VOTE_THRESHOLD", "2").strip()
    try:
        open_vote_threshold = int(open_vote_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid OPEN_VOTE_THRESHOLD format: {e}. Must be an integer."
        ) from e
    if not (1 <= open_vote_threshold <= 10):
        raise ConfigurationError(
            f"OPEN_VOTE_THRESHOLD must be between 1 and 10, got: {open_vote_threshold}"
        )

    return wind_threshold, altitude_threshold, min_ghi, min_uv_index, min_dni, max_cloud_cover, min_temperature_f, overcast_threshold, min_dni_cirrus, rain_probability_threshold, open_vote_threshold


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
            "wind_speed_10m,precipitation,weather_code,is_day,temperature_2m,"
            "shortwave_radiation,uv_index,direct_normal_irradiance,"
            "cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high"
        ),
        "hourly": "precipitation_probability",
        "minutely_15": "precipitation",
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
        # Guard all four Layer 1/2 fields (GHI, UV, DNI, cloud_cover) and the
        # two Layer 3 ceiling fields (cloud_cover_mid, cloud_cover_high). The
        # .get(..., 100) defaults below handle missing keys (network glitch),
        # but a JSON null for these fields is a distinct error condition that
        # must be surfaced explicitly rather than silently classified as 100%.
        shortwave_radiation = current["shortwave_radiation"]
        uv_index = current["uv_index"]
        direct_normal_irradiance = current.get("direct_normal_irradiance")
        cloud_cover_val = current.get("cloud_cover")
        cloud_cover_mid_val = current.get("cloud_cover_mid")
        cloud_cover_high_val = current.get("cloud_cover_high")
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
        if cloud_cover_mid_val is None:
            raise WeatherAPIError(
                f"Weather API returned null for cloud_cover_mid. "
                f"Cannot evaluate Layer 3 overcast ceiling without this value."
            )
        if cloud_cover_high_val is None:
            raise WeatherAPIError(
                f"Weather API returned null for cloud_cover_high. "
                f"Cannot evaluate Layer 3 overcast ceiling without this value."
            )

        # Extract hourly precipitation_probability for the current hour.
        # The hourly block returns a time-indexed array; we match the current
        # time's hour-boundary to find the right slot. If the block is absent
        # or the current-hour slot cannot be found, we default to None so the
        # rain gate can treat missing data conservatively.
        hourly_precip_prob: Optional[int] = None
        if "hourly" in data:
            hourly = data["hourly"]
            hourly_times = hourly.get("time", [])
            hourly_probs = hourly.get("precipitation_probability", [])
            current_time_str = current.get("time", "")
            # current_time_str format: "2026-06-23T11:30" — truncate to hour for matching
            current_hour_prefix = current_time_str[:13]  # "2026-06-23T11"
            for idx, ts in enumerate(hourly_times):
                if ts.startswith(current_hour_prefix) and idx < len(hourly_probs):
                    hourly_precip_prob = hourly_probs[idx]
                    break

        # Extract minutely_15 precipitation for the last ~3 slots (past ~30-45 min).
        # Slots are in ascending time order; we want the most-recent past slots.
        # If the block is absent we return an empty list — the rain gate treats
        # this conservatively (missing data → assume rain).
        minutely_15_precip: list = []
        if "minutely_15" in data:
            m15 = data["minutely_15"]
            m15_times = m15.get("time", [])
            m15_precip = m15.get("precipitation", [])
            current_time_str = current.get("time", "")
            # Collect all past/current slots up to current time, keep last 3.
            past_slots = []
            for idx, ts in enumerate(m15_times):
                if ts <= current_time_str and idx < len(m15_precip):
                    past_slots.append(m15_precip[idx])
            minutely_15_precip = past_slots[-3:] if past_slots else []

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
            "weather_code": current.get("weather_code"),
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
            "hourly_precip_prob": hourly_precip_prob,
            "minutely_15_precip": minutely_15_precip,
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


# Zoom level for RainViewer tile requests.
# Zoom 6 yields ~156 km/pixel tiles covering roughly 2.5° lat/lon per tile.
# That is adequate resolution for a point precipitation check: a 256×256 pixel
# tile at zoom 6 gives ~610 m/pixel at the equator.
_RAINVIEWER_TILE_ZOOM = 6
_RAINVIEWER_MAPS_URL = "https://api.rainviewer.com/public/weather-maps.json"


def is_raining_on_radar(lat: float, lon: float, timeout: int = 5) -> bool:
    """
    Check if NEXRAD radar shows active precipitation at the given lat/lon.

    Uses the RainViewer API (free, no API key) which aggregates real NEXRAD
    WSR-88D radar returns. Lag is ~8-15 minutes (NEXRAD scan cycle + RainViewer
    processing), which is vastly better than HRRR's 1-3 hour model init cycle.

    Algorithm:
      1. Fetch the latest radar frame metadata from RainViewer.
      2. Compute the Web Mercator (XYZ) tile covering the location.
      3. Fetch the 256×256 PNG tile.
      4. Extract the pixel for the exact lat/lon within the tile.
      5. Return True if the pixel alpha > 0 (non-transparent = precipitation).

    Fail-open semantics: any exception or timeout returns False.
    A RainViewer outage cannot keep the awning permanently closed — the
    Open-Meteo signals in evaluate_rain_gate() still guard independently.

    Args:
        lat: Latitude of the location to check.
        lon: Longitude of the location to check.
        timeout: HTTP request timeout in seconds (default 5).

    Returns:
        True if radar shows active precipitation; False otherwise or on error.
    """
    try:
        # Step 1: fetch latest radar frame metadata
        meta_resp = requests.get(_RAINVIEWER_MAPS_URL, timeout=timeout)
        meta_resp.raise_for_status()
        meta = meta_resp.json()

        past_frames = meta.get("radar", {}).get("past", [])
        if not past_frames:
            logger.warning("RainViewer: no past radar frames available — skipping radar check")
            return False

        latest_path = past_frames[-1]["path"]

        # Step 2: compute XYZ tile coordinates (Web Mercator / EPSG:3857)
        z = _RAINVIEWER_TILE_ZOOM
        lat_rad = math.radians(lat)
        n = 2 ** z
        x_float = (lon + 180.0) / 360.0 * n
        y_float = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
        tile_x = int(x_float)
        tile_y = int(y_float)
        # Pixel position within the 256×256 tile
        px = int((x_float - tile_x) * 256)
        py = int((y_float - tile_y) * 256)
        # Single-tile sampling note: only the tile containing the configured
        # coordinates is fetched and sampled. At zoom 6 (~610 m/pixel), the
        # location can sit near a tile boundary (~3 km buffer before the edge
        # matters). A narrow precipitation band straddling the boundary would
        # only register if it extends far enough into this tile. Adjacent tile
        # sampling is intentionally not implemented — acceptable risk for this
        # use case given the coarse resolution and typical storm-cell width.

        # Step 3: fetch tile PNG
        # Path suffix: /256/{z}/{x}/{y}/2/1_1.png
        #   256   — tile size in pixels
        #   2     — Meteored color scheme
        #   1_1   — smooth=1, snow=1 (snow returns also count as precipitation)
        tile_url = (
            f"https://tilecache.rainviewer.com{latest_path}"
            f"/256/{z}/{tile_x}/{tile_y}/2/1_1.png"
        )
        tile_resp = requests.get(tile_url, timeout=timeout)
        tile_resp.raise_for_status()

        # Step 4: decode pixel — alpha=0 means no radar return (no precipitation)
        # PIL is an optional dependency; if missing, fail open (radar disabled).
        try:
            from PIL import Image
        except ImportError:
            logger.warning("Pillow is not installed — radar check disabled, failing open")
            return False
        img = Image.open(io.BytesIO(tile_resp.content)).convert("RGBA")
        _r, _g, _b, alpha = img.getpixel((px, py))
        is_raining = alpha > 0

        if is_raining:
            logger.info(
                f"RainViewer: radar return at ({lat:.3f}, {lon:.3f}) — rain detected"
            )
        else:
            logger.debug(
                f"RainViewer: no radar return at ({lat:.3f}, {lon:.3f})"
            )

        return is_raining

    except requests.RequestException as e:
        logger.warning(f"RainViewer API error: {e} — skipping radar check")
        return False
    except Exception as e:
        logger.warning(f"RainViewer parse error: {e} — skipping radar check")
        return False


# WMO weather codes that indicate rain, drizzle, snow, showers, or thunderstorms.
# Source: WMO Code Table 4677 / Open-Meteo docs.
#   51-57: Drizzle (slight/moderate/dense, freezing drizzle)
#   61-67: Rain (slight/moderate/heavy, freezing rain)
#   71-77: Snow (slight/moderate/heavy, snow grains, ice crystals)
#   80-82: Rain showers (slight/moderate/violent)
#   95-99: Thunderstorm (slight/moderate, with hail)
_RAIN_WEATHER_CODES = frozenset({
    51, 53, 55, 56, 57,
    61, 63, 65, 66, 67,
    71, 73, 75, 77,
    80, 81, 82,
    95, 96, 99,
})


def evaluate_rain_gate(
    weather: dict,
    rain_probability_threshold: int = 20,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> bool:
    """
    Evaluate whether it is safe (no rain) based on multiple signals.

    Returns True (safe = no rain) only when ALL configured signals are clear.
    Returns False (rain detected → close) if ANY signal fires.
    Treats missing/null fields conservatively as rain (bias to close).

    Signals checked (OR-of-any → close):
      1. current.precipitation > 0 — direct current-slot precipitation
      2. hourly.precipitation_probability >= rain_probability_threshold —
         ensemble-based probability gate; catches rain the deterministic model
         missed (e.g., fast convective onset after model initialization)
      3. sum(minutely_15.precipitation[-3 slots]) > 0 — rain in the last ~30-45
         min from the recent-history lookback window; helps when the current slot
         just rolled over to 0 but rain was active moments earlier
      4. current.weather_code in RAIN_WEATHER_CODES — WMO synoptic code gate;
         a secondary cross-check derived from a different field path than precipitation
      5. is_raining_on_radar(lat, lon) — RainViewer NEXRAD radar tile check;
         a real-time observation independent of NWP model init lag.
         Skipped (fail-open) when lat/lon are not provided or on any fetch error.

    Args:
        weather: Weather dict from fetch_weather(); must contain 'precipitation'.
            May optionally contain 'hourly_precip_prob', 'minutely_15_precip',
            and 'weather_code'. Missing fields are treated as rain (conservative).
        rain_probability_threshold: % threshold for ensemble precipitation
            probability (default 20 — from RAIN_PROBABILITY_THRESHOLD env var).
        lat: Latitude for radar check (optional). When None, radar check is skipped.
        lon: Longitude for radar check (optional). When None, radar check is skipped.

    Returns:
        True if all signals are clear (no rain); False if any signal fires (rain).
    """
    # Signal 1: current precipitation > 0
    precipitation = weather.get("precipitation")
    if precipitation is None or precipitation > 0:
        return False

    # Signal 2: hourly ensemble precipitation probability >= threshold
    # None means the field was missing from the API response — treat conservatively.
    hourly_precip_prob = weather.get("hourly_precip_prob")
    if hourly_precip_prob is None or hourly_precip_prob >= rain_probability_threshold:
        return False

    # Signal 3: any precipitation in the last ~3 minutely_15 slots (past ~30-45 min)
    # None means the block was absent — treat conservatively.
    minutely_15_precip = weather.get("minutely_15_precip")
    if minutely_15_precip is None or any(v > 0 for v in minutely_15_precip):
        return False

    # Signal 4: WMO weather_code in rain/drizzle/snow/shower/thunderstorm set
    # None means the field was absent — treat conservatively.
    weather_code = weather.get("weather_code")
    if weather_code is None or weather_code in _RAIN_WEATHER_CODES:
        return False

    # Signal 5: RainViewer NEXRAD radar — real-time observation independent of NWP.
    # Fail-open: is_raining_on_radar returns False on any error/timeout, so a
    # RainViewer outage cannot keep the awning closed. Skipped when lat/lon absent.
    if lat is not None and lon is not None:
        if is_raining_on_radar(lat, lon):
            return False

    return True


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
    min_temperature_f: float = 45.0,
    overcast_threshold: float = 95.0,
    min_dni_cirrus: float = 30.0,
    rain_probability_threshold: int = 20,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
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

      Layer 3 — hard cloud-cover ceiling with DNI guard (overcast override):
        not_overcast = (max(cloud_cover_mid, cloud_cover_high) < overcast_threshold)
                       OR (dni >= min_dni_cirrus)
        When max(cloud_cover_mid, cloud_cover_high) >= overcast_threshold (default 95%),
        DNI is overridden — UNLESS dni >= min_dni_cirrus (default 30 W/m²), which proves
        direct sun is arriving despite the cloud model. Both mid-level (altostratus/
        altocumulus) and high-level (cirrostratus) clouds can independently produce full
        optical overcast — the 2026-04-28 incident confirmed this: cloud_cover_high=99%
        with cloud_cover_mid at 53-70% fully blocked the sun. max() ensures either layer
        fires the ceiling. The DNI guard handles the 2026-05-12 false positive where
        Open-Meteo returned cloud_cover_high=100% despite a clear sky (DNI=905 W/m²
        and direct visual observation both confirmed direct sun was arriving).
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
        min_dni_cirrus: DNI guard threshold (W/m²) for Layer 3; when DNI >= this value,
            the overcast ceiling is bypassed because direct sun is demonstrably arriving

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

    # Layer 3: hard cloud-cover ceiling with DNI guard — overcast override
    # max(cloud_cover_mid, cloud_cover_high) fires the ceiling if EITHER mid-level
    # (altostratus/altocumulus) OR high-level (cirrostratus) clouds are thick enough.
    # The 2026-04-28 incident confirmed that cloud_cover_high=99% with cloud_cover_mid
    # at 53-70% fully blocked direct sun. Total cloud cover is NOT used because it
    # saturates to 100% with any cirrus, making it too aggressive.
    #
    # DNI guard: if DNI >= min_dni_cirrus, direct sun is demonstrably arriving and
    # the cloud model estimate is wrong. The ceiling fires ONLY when BOTH the cloud
    # model says overcast AND DNI is too low to confirm direct beam. This prevents
    # the 2026-05-12 false positive where cloud_cover_high=100% (bad model data)
    # closed the awning during peak midday sun (DNI=905 W/m²).
    cloud_ceiling_clear = max(cloud_cover_mid, cloud_cover_high) < overcast_threshold
    dni_confirms_sun = dni >= min_dni_cirrus
    not_overcast = cloud_ceiling_clear or dni_confirms_sun

    is_sunny = sunny_model and sunny_observed and not_overcast

    is_calm = wind_speed < wind_threshold
    no_rain = evaluate_rain_gate(weather, rain_probability_threshold, lat=lat, lon=lon)
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

    # Layer 3: hard cloud-cover ceiling with DNI guard
    _overcast_driver = max(cloud_cover_mid, cloud_cover_high)
    if cloud_ceiling_clear:
        overcast_trace = (
            f"max(cloud_mid={cloud_cover_mid:.0f}%,cloud_high={cloud_cover_high:.0f}%)"
            f"={_overcast_driver:.0f}% < {overcast_threshold:.0f}% ceiling"
        )
    elif dni_confirms_sun:
        overcast_trace = (
            f"DNI guard: DNI {dni:.0f} W/m² >= {min_dni_cirrus:.0f} overrides "
            f"cloud ceiling (max={_overcast_driver:.0f}% >= {overcast_threshold:.0f}%)"
        )
    else:
        overcast_trace = (
            f"max(cloud_mid={cloud_cover_mid:.0f}%,cloud_high={cloud_cover_high:.0f}%)"
            f"={_overcast_driver:.0f}% >= {overcast_threshold:.0f}% ceiling "
            f"AND DNI {dni:.0f} W/m² < {min_dni_cirrus:.0f} guard"
        )

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


def apply_hysteresis(
    should_open: bool,
    current_votes: int,
    threshold: int,
) -> tuple[str, int]:
    """
    Apply asymmetric hysteresis to an open/close vote.

    Close is immediate (safety-critical); open requires N consecutive votes.

    Args:
        should_open: Whether current conditions say the awning should open.
        current_votes: Accumulated consecutive open-vote count from prior runs.
        threshold: Number of consecutive open votes required before opening.

    Returns:
        Tuple of (action, new_vote_count) where action is 'open' | 'close' | 'hold'.
        - 'close': should_open is False — close immediately, counter reset to 0.
        - 'hold':  should_open is True but votes < threshold — stay closed, counter +1.
        - 'open':  should_open is True and votes+1 >= threshold — open, counter kept.
    """
    if not should_open:
        return "close", 0
    new_votes = current_votes + 1
    if new_votes >= threshold:
        return "open", new_votes
    return "hold", new_votes


def get_vote_state_path(log_dir: Path) -> Path:
    """
    Return the path for the open-vote state file alongside the log directory.

    Args:
        log_dir: Directory where log files are stored.

    Returns:
        Path to awning-open-votes.json within that directory.
    """
    return log_dir / "awning-open-votes.json"


def read_vote_state(state_path: Path) -> int:
    """
    Read the consecutive open-vote count from disk.

    Returns 0 on any error (file missing, corrupt JSON, wrong schema).  The
    safe default is "no prior open votes" — the awning stays closed on doubt.

    Args:
        state_path: Path to the vote-state JSON file.

    Returns:
        Consecutive open vote count (>= 0).
    """
    try:
        data = json.loads(state_path.read_text())
        count = int(data["consecutive_open_votes"])
        return max(count, 0)
    except Exception:
        return 0


def write_vote_state(state_path: Path, count: int) -> None:
    """
    Persist the consecutive open-vote count to disk atomically.

    Uses a temp-file + os.replace pattern so the file is never partially
    written (POSIX rename is atomic on the same filesystem).

    Args:
        state_path: Destination path for the vote-state JSON file.
        count: Consecutive open-vote count to persist.
    """
    payload = {
        "consecutive_open_votes": count,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    # Write to a sibling temp file, then atomically rename into place.
    dir_ = state_path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".vote-tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, state_path)
    except Exception:
        # Clean up orphaned temp file on failure; re-raise so the caller knows.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
        wind_threshold, altitude_threshold, min_ghi, min_uv_index, min_dni, max_cloud_cover, min_temperature_f, overcast_threshold, min_dni_cirrus, rain_probability_threshold, open_vote_threshold = get_thresholds()
        logger.info(
            f"Thresholds: model=(GHI >= {min_ghi:.0f} W/m² OR UV >= {min_uv_index:.1f}), "
            f"consistency=(DNI >= {min_dni:.0f} W/m² OR cloud < {max_cloud_cover:.0f}%), "
            f"overcast ceiling=cloud < {overcast_threshold:.0f}% (DNI guard >= {min_dni_cirrus:.0f} W/m²), "
            f"Wind < {wind_threshold} mph, Rain precip=0 AND prob < {rain_probability_threshold}%, "
            f"Temp > {min_temperature_f:.0f}°F, "
            f"Sun altitude >= {altitude_threshold}°, Sun facing window (90°-260°), "
            f"open_vote_threshold={open_vote_threshold}"
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
            min_dni_cirrus,
            rain_probability_threshold,
            lat=latitude,
            lon=longitude,
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
            vote_state_path = get_vote_state_path(log_path.parent)
            current_votes = read_vote_state(vote_state_path)
            action, new_votes = apply_hysteresis(should_open, current_votes, open_vote_threshold)
            logger.info(f"Current awning state: {'OPEN' if is_open else 'CLOSED'}")
            logger.info(f"Would set awning to: {'OPEN' if should_open else 'CLOSED'}")
            logger.info(
                f"Hysteresis: vote {current_votes} → {new_votes}/{open_vote_threshold}, "
                f"action={action} (dry-run: no state written)"
            )
            logger.info("Dry-run complete (no action taken)")
            return

        # Get state before action
        state_before = controller.get_state()

        # Apply anti-flapping hysteresis: close is immediate; open requires N consecutive votes
        vote_state_path = get_vote_state_path(log_path.parent)
        current_votes = read_vote_state(vote_state_path)
        action, new_votes = apply_hysteresis(should_open, current_votes, open_vote_threshold)
        write_vote_state(vote_state_path, new_votes)

        if action == "close":
            logger.info("Closing awning...")
            controller.close()
            logger.info("Awning set to CLOSED")
        elif action == "open":
            logger.info(f"Open vote {new_votes}/{open_vote_threshold} reached — opening awning...")
            controller.open()
            logger.info("Awning set to OPEN")
        else:
            # action == "hold": open vote accumulating, do not change awning state
            logger.info(
                f"Holding closed (open vote {new_votes}/{open_vote_threshold} — "
                f"waiting for {open_vote_threshold - new_votes} more consecutive open vote(s))"
            )

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
