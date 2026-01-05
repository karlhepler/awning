#!/usr/bin/env python3
"""
Awning Weather Automation

Automatically opens/closes awning based on weather conditions.
- Opens awning if: sunny (cloud cover < 30%) AND calm (wind < 10 mph)
- Closes awning otherwise

Designed to run as a cron job or Kubernetes scheduled job.
"""

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv
from pvlib import solarposition

from awning_controller import (
    BondAPIError,
    BondAwningController,
    ConfigurationError,
    create_controller_from_env,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


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


def get_thresholds() -> tuple[int, float]:
    """
    Get weather thresholds from environment variables.

    Returns:
        Tuple of (cloud_cover_threshold, wind_speed_threshold_mph)

    Raises:
        ConfigurationError: If threshold variables are missing or invalid
    """
    # Get cloud cover threshold
    cloud_str = os.getenv("CLOUD_COVER_THRESHOLD", "").strip()
    if not cloud_str:
        raise ConfigurationError(
            "CLOUD_COVER_THRESHOLD environment variable is not set. "
            "Please add it to your .env file (e.g., CLOUD_COVER_THRESHOLD=30)"
        )

    # Get wind speed threshold
    wind_str = os.getenv("WIND_SPEED_THRESHOLD_MPH", "").strip()
    if not wind_str:
        raise ConfigurationError(
            "WIND_SPEED_THRESHOLD_MPH environment variable is not set. "
            "Please add it to your .env file (e.g., WIND_SPEED_THRESHOLD_MPH=10)"
        )

    # Parse as numbers
    try:
        cloud_threshold = int(cloud_str)
        wind_threshold = float(wind_str)
    except ValueError as e:
        raise ConfigurationError(
            f"Invalid threshold format: {e}. Must be numbers."
        ) from e

    # Validate ranges
    if not (0 <= cloud_threshold <= 100):
        raise ConfigurationError(
            f"CLOUD_COVER_THRESHOLD must be between 0 and 100, got: {cloud_threshold}"
        )
    if wind_threshold < 0:
        raise ConfigurationError(
            f"WIND_SPEED_THRESHOLD_MPH must be positive, got: {wind_threshold}"
        )

    return cloud_threshold, wind_threshold


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
        "current": "cloud_cover,wind_speed_10m,precipitation,is_day",
        "daily": "sunrise,sunset",
        "wind_speed_unit": "mph",
        "timezone": "auto",
        "forecast_days": 1,
    }

    try:
        response = requests.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        data = response.json()

        # Extract current weather
        if "current" not in data:
            raise WeatherAPIError("Weather API response missing 'current' field")

        current = data["current"]
        required_fields = ["cloud_cover", "wind_speed_10m", "precipitation"]
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
            "cloud_cover": current["cloud_cover"],
            "wind_speed_10m": current["wind_speed_10m"],
            "precipitation": current["precipitation"],
            "is_day": current.get("is_day", 1),
            "time": current.get("time", "unknown"),
            "sunrise": daily["sunrise"][0],
            "sunset": daily["sunset"][0],
        }

    except requests.RequestException as e:
        raise WeatherAPIError(f"Failed to fetch weather data: {e}") from e


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


def is_sun_facing_southeast(azimuth: float) -> bool:
    """
    Check if sun is facing southeast (between East and South).

    Args:
        azimuth: Sun azimuth in degrees (0=North, 90=East, 180=South, 270=West)

    Returns:
        True if azimuth is between 90° (East) and 180° (South)
    """
    return 90 <= azimuth <= 180


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
    latitude: float,
    longitude: float,
    cloud_threshold: int,
    wind_threshold: float,
) -> tuple[bool, str, dict]:
    """
    Determine if awning should be open based on ALL conditions.

    Args:
        weather: Weather data from fetch_weather()
        sun_position: Sun position data from calculate_sun_position()
        current_time: Current datetime
        latitude: Location latitude
        longitude: Location longitude
        cloud_threshold: Maximum cloud cover percentage for "sunny"
        wind_threshold: Maximum wind speed (mph) for "calm"

    Returns:
        Tuple of (should_open, reason, conditions_dict)
    """
    # Extract weather data
    cloud_cover = weather["cloud_cover"]
    wind_speed = weather["wind_speed_10m"]
    precipitation = weather["precipitation"]
    sunrise = weather["sunrise"]
    sunset = weather["sunset"]

    # Extract sun position
    azimuth = sun_position["azimuth"]
    altitude = sun_position["altitude"]

    # Evaluate each condition
    is_sunny = cloud_cover < cloud_threshold
    is_calm = wind_speed < wind_threshold
    no_rain = precipitation == 0
    is_day = is_daytime(current_time, sunrise, sunset)
    sun_facing_se = is_sun_facing_southeast(azimuth)

    # Build conditions dict for logging
    conditions = {
        "sunny": is_sunny,
        "calm": is_calm,
        "no_rain": no_rain,
        "daytime": is_day,
        "sun_facing_se": sun_facing_se,
    }

    # All conditions must be True to open awning
    should_open = all(conditions.values())

    # Build detailed reason string
    reasons = []
    if not is_sunny:
        reasons.append(f"Too cloudy ({cloud_cover}% >= {cloud_threshold}%)")
    if not is_calm:
        reasons.append(f"Too windy ({wind_speed} >= {wind_threshold} mph)")
    if not no_rain:
        reasons.append(f"Raining ({precipitation} mm/h)")
    if not is_day:
        reasons.append(
            f"Nighttime (sunrise {sunrise[11:16]}, sunset {sunset[11:16]})"
        )
    if not sun_facing_se:
        reasons.append(f"Sun not facing SE (azimuth {azimuth:.1f}°, need 90°-180°)")

    if should_open:
        reason = (
            f"All conditions met: {cloud_cover}% clouds, {wind_speed} mph wind, "
            f"{precipitation} mm/h rain, sun azimuth {azimuth:.1f}° (altitude {altitude:.1f}°)"
        )
    else:
        reason = ", ".join(reasons)

    return should_open, reason, conditions


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

    if dry_run:
        logger.info("Running in DRY-RUN mode (no awning control)")

    if env_file:
        logger.info(f"Using .env file: {env_file}")

    try:
        # Load location configuration
        latitude, longitude = load_location_config(env_file)
        logger.info(f"Location: {latitude:.4f}, {longitude:.4f}")

        # Get thresholds
        cloud_threshold, wind_threshold = get_thresholds()
        logger.info(
            f"Thresholds: Cloud < {cloud_threshold}%, Wind < {wind_threshold} mph, "
            f"Rain = 0 mm/h, Daytime only, Sun facing SE (90°-180°)"
        )

        # Fetch current weather
        logger.info("Fetching weather data...")
        weather = fetch_weather(latitude, longitude)
        logger.info(
            f"Weather: {weather['cloud_cover']}% clouds, "
            f"{weather['wind_speed_10m']} mph wind, "
            f"{weather['precipitation']} mm/h rain (at {weather['time']})"
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
            latitude,
            longitude,
            cloud_threshold,
            wind_threshold,
        )

        # Log conditions with checkmarks/crosses
        condition_symbols = {
            "sunny": "Sunny" if conditions["sunny"] else "Cloudy",
            "calm": "Calm" if conditions["calm"] else "Windy",
            "no_rain": "No rain" if conditions["no_rain"] else "Rain",
            "daytime": "Daytime" if conditions["daytime"] else "Nighttime",
            "sun_facing_se": "Sun facing SE" if conditions["sun_facing_se"] else "Sun not facing SE",
        }
        check_str = ", ".join(
            [f"{'✓' if v else '✗'} {condition_symbols[k]}" for k, v in conditions.items()]
        )
        logger.info(f"Conditions: {check_str}")
        logger.info(f"Decision: {reason}")

        # Create controller and get current state
        controller = create_controller_from_env(env_file)
        current_state = controller.get_state()

        # Interpret state (1 = open, 0 = closed)
        is_open = current_state == 1
        logger.info(f"Current awning state: {'OPEN' if is_open else 'CLOSED'}")

        if dry_run:
            logger.info(f"Would set awning to: {'OPEN' if should_open else 'CLOSED'}")
            logger.info("Dry-run complete (no action taken)")
            return

        # Take action ONLY if state needs to change (CRITICAL)
        if should_open and not is_open:
            logger.info("Opening awning...")
            controller.open()
            logger.info("Awning opened successfully")
        elif not should_open and is_open:
            logger.info("Closing awning...")
            controller.close()
            logger.info("Awning closed successfully")
        else:
            logger.info(
                f"No action needed - awning already {'OPEN' if is_open else 'CLOSED'}"
            )

        logger.info("Automation complete")

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
                else:
                    logger.info("Awning already closed")
            except Exception as fail_safe_error:
                logger.error(f"Fail-safe close failed: {fail_safe_error}")
        sys.exit(1)
    except BondAPIError as e:
        logger.error(f"Bond API error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
