"""
Unit tests for awning_automation.py

Tests are hermetic — should_open_awning() takes all inputs as parameters so no
mocking of external I/O is required.

Run:  python3 -m unittest test_awning_automation.py -v
"""
import os
import unittest
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Helpers — build minimal weather / sun_position dicts
# ---------------------------------------------------------------------------

def _weather(
    *,
    wind_speed=5.0,
    precipitation=0,
    temperature=65.0,
    shortwave_radiation=500.0,
    uv_index=6.0,
    dni=450.0,
    cloud_cover=20.0,
    sunrise="2026-04-17T06:00:00",
    sunset="2026-04-17T20:00:00",
):
    # cloud_cover is included because it feeds Layer 2 (observational sunny gate).
    # Default 20% represents a clear-to-partly-cloudy day (well below MAX_CLOUD_COVER_PCT=80).
    return {
        "wind_speed_10m": wind_speed,
        "precipitation": precipitation,
        "temperature": temperature,
        "shortwave_radiation": shortwave_radiation,
        "uv_index": uv_index,
        "dni": dni,
        "cloud_cover": cloud_cover,
        "sunrise": sunrise,
        "sunset": sunset,
    }


def _sun(*, azimuth=150.0, altitude=35.0):
    return {"azimuth": azimuth, "altitude": altitude}


# Standard thresholds used in most tests — match card defaults
_THRESHOLDS = dict(
    wind_threshold=15.0,
    altitude_threshold=20.0,
    min_ghi=400.0,
    min_uv_index=4.0,
    min_dni=50.0,
    max_cloud_cover=80.0,
)

# Daytime moment that falls between the default sunrise/sunset strings above
_DAYTIME = datetime(2026, 4, 17, 13, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Import functions under test
# ---------------------------------------------------------------------------
from awning_automation import should_open_awning, ConfigurationError, get_thresholds, WeatherAPIError, fetch_weather


class TestShouldOpenAwningOrGate(unittest.TestCase):
    """Tests for the GHI-OR-UV sunny gate in should_open_awning()."""

    # ------------------------------------------------------------------
    # Test 1 — Clear sunny day: GHI=700, UV=7 → sunny_enough=True, opens
    # Both signals above threshold — a classic clear-sky midday scenario.
    # ------------------------------------------------------------------
    def test_clear_sunny_day_opens(self):
        """Clear day: GHI=700 and UV=7 both exceed thresholds → opens."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(shortwave_radiation=700.0, uv_index=7.0),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            conditions["sunny"],
            f"Expected sunny=True but got False. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"Expected awning to open but got False. reason={reason!r}",
        )
        # Verify both signals appear in reason trace
        self.assertIn("GHI", reason)
        self.assertIn("UV", reason)

    # ------------------------------------------------------------------
    # Test 2 — Genuinely overcast: GHI=100, UV=1 → sunny_enough=False, stays closed
    # Both signals below threshold — heavy overcast, no shade needed.
    # ------------------------------------------------------------------
    def test_genuinely_overcast_stays_closed(self):
        """Overcast: GHI=100 and UV=1 both below thresholds → stays closed."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(shortwave_radiation=100.0, uv_index=1.0),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            conditions["sunny"],
            f"Expected sunny=False for overcast but got True. reason={reason!r}",
        )
        self.assertFalse(
            should_open,
            f"Expected awning to stay closed but got True. reason={reason!r}",
        )
        self.assertIn("Not sunny", reason)

    # ------------------------------------------------------------------
    # Test 3 — Cloudy but high UV (cirrus passing UV): GHI=300, UV=5
    # GHI below threshold but UV above → sunny_enough=True via UV, opens.
    # Physical scenario: cirrus or thin cloud scatter UV but attenuate GHI.
    # ------------------------------------------------------------------
    def test_cloudy_high_uv_opens_via_uv(self):
        """High UV despite suppressed GHI → opens via UV signal alone."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(shortwave_radiation=300.0, uv_index=5.0),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            conditions["sunny"],
            f"Expected sunny=True via UV but got False. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"Expected awning to open via UV but got False. reason={reason!r}",
        )
        # Should mention UV-only path
        self.assertIn("UV only", reason)

    # ------------------------------------------------------------------
    # Test 4 — Sunny but low UV (unusual but physically possible):
    # GHI=500, UV=2 → sunny_enough=True via GHI, opens.
    # Physical scenario: high solar elevation, less atmosphere, UV suppressed
    # by aerosols but direct beam still strong.
    # ------------------------------------------------------------------
    def test_sunny_low_uv_opens_via_ghi(self):
        """High GHI despite low UV → opens via GHI signal alone."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(shortwave_radiation=500.0, uv_index=2.0),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            conditions["sunny"],
            f"Expected sunny=True via GHI but got False. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"Expected awning to open via GHI but got False. reason={reason!r}",
        )
        # Should mention GHI-only path
        self.assertIn("GHI only", reason)

    # ------------------------------------------------------------------
    # Test 5 — Today's NWP-bug scenario: GHI=658, UV=6.8 — the case that
    # triggered this refactor. DNI and cloud_cover fields co-failed in the
    # NWP pipeline (DNI showed low, cloud_mid showed high, but GHI and UV
    # from different derivation paths both showed it was clearly sunny).
    # Both signals now well above threshold → opens.
    # ------------------------------------------------------------------
    def test_nwp_bug_scenario_both_high_opens(self):
        """Card's original bug: GHI=658, UV=6.8, DNI=45 but cloud=30% → opens via cloud_cover gate."""
        # DNI=45 is just below min_dni=50, but cloud=30% < max_cloud_cover=80%
        # so sunny_observed is True via cloud_cover. Both layers pass → opens.
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=658.0,
                uv_index=6.8,
                dni=45.0,
                cloud_cover=30.0,
            ),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            conditions["sunny"],
            f"Expected sunny=True for bug scenario but got False. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"Expected awning to open for bug scenario but got False. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # Test 6 — Non-sunny conditions block open even when GHI and UV are high
    # Wind is above threshold → awning stays closed despite sunny weather.
    # ------------------------------------------------------------------
    def test_high_wind_overrides_sunny(self):
        """High wind overrides sunny signal → stays closed."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(shortwave_radiation=700.0, uv_index=7.0, wind_speed=20.0),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(conditions["sunny"], "Expected sunny=True")
        self.assertFalse(conditions["calm"], "Expected calm=False for wind=20 mph")
        self.assertFalse(
            should_open,
            f"Expected awning to stay closed due to wind but got True. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # Test 7 — get_thresholds() validates MIN_GHI_WM2 > 0
    # ------------------------------------------------------------------
    def test_zero_min_ghi_rejected_by_validation(self):
        """MIN_GHI_WM2=0 must raise ConfigurationError."""
        env_patch = {
            "WIND_SPEED_THRESHOLD_MPH": "15",
            "MIN_SUN_ALTITUDE_DEG": "20",
            "MIN_GHI_WM2": "0",
            "MIN_UV_INDEX": "4",
        }
        original_values = {}
        for k, v in env_patch.items():
            original_values[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            with self.assertRaises(ConfigurationError) as ctx:
                get_thresholds()
            self.assertIn("MIN_GHI_WM2", str(ctx.exception))
        finally:
            for k, orig in original_values.items():
                if orig is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = orig

    # ------------------------------------------------------------------
    # Test 8 — get_thresholds() validates MIN_UV_INDEX > 0
    # ------------------------------------------------------------------
    def test_zero_min_uv_rejected_by_validation(self):
        """MIN_UV_INDEX=0 must raise ConfigurationError."""
        env_patch = {
            "WIND_SPEED_THRESHOLD_MPH": "15",
            "MIN_SUN_ALTITUDE_DEG": "20",
            "MIN_GHI_WM2": "400",
            "MIN_UV_INDEX": "0",
        }
        original_values = {}
        for k, v in env_patch.items():
            original_values[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            with self.assertRaises(ConfigurationError) as ctx:
                get_thresholds()
            self.assertIn("MIN_UV_INDEX", str(ctx.exception))
        finally:
            for k, orig in original_values.items():
                if orig is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = orig

    # ------------------------------------------------------------------
    # Test 9 — conditions dict has exactly 7 keys (no diagnostic bloat)
    # Also asserts all values are booleans to catch refactors that store
    # numbers in place of flags.
    # ------------------------------------------------------------------
    def test_conditions_dict_has_exactly_7_keys(self):
        """conditions dict must contain only the 7 real decision fields, all booleans."""
        _, _, conditions = should_open_awning(
            weather=_weather(),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        expected_keys = {"sunny", "calm", "no_rain", "above_freezing", "daytime", "sun_high", "sun_facing_window"}
        self.assertEqual(
            set(conditions.keys()),
            expected_keys,
            f"Unexpected conditions keys: {set(conditions.keys()) - expected_keys}",
        )
        self.assertTrue(
            all(isinstance(v, bool) for v in conditions.values()),
            f"All conditions values must be bool; got: {[(k, type(v).__name__) for k, v in conditions.items() if not isinstance(v, bool)]}",
        )

    # ------------------------------------------------------------------
    # Test 10 — Night scenario: altitude=-10, GHI=0, UV=0 → awning stays
    # closed, with sunny=False, sun_high=False, and daytime=False all firing.
    # Regression guard: losing any one of these gates would still produce
    # should_open=False (due to AND logic), but this test pins the specific
    # gate flags so a refactor that drops a gate is caught.
    # ------------------------------------------------------------------
    def test_night_sun_below_horizon(self):
        """Night: altitude=-10, GHI=0, UV=0 → closed; sunny, sun_high, daytime all False."""
        # Use a time well outside the default sunrise/sunset window
        nighttime = datetime(2026, 4, 17, 2, 0, 0, tzinfo=timezone.utc)
        should_open, reason, conditions = should_open_awning(
            weather=_weather(shortwave_radiation=0.0, uv_index=0.0),
            sun_position=_sun(altitude=-10.0, azimuth=0.0),  # below horizon, north
            current_time=nighttime,
            **_THRESHOLDS,
        )
        self.assertFalse(should_open, f"Expected awning closed at night but got True. reason={reason!r}")
        self.assertFalse(conditions["sunny"], f"Expected sunny=False at night but got True")
        self.assertFalse(conditions["sun_high"], f"Expected sun_high=False for altitude=-10 but got True")
        self.assertFalse(conditions["daytime"], f"Expected daytime=False at 02:00 UTC but got True")

    # ------------------------------------------------------------------
    # Test 11 — H-1: null shortwave_radiation raises WeatherAPIError
    # Open-Meteo can return JSON null for numeric fields when GFS coverage
    # lapses. fetch_weather() must catch this before threshold comparisons.
    # ------------------------------------------------------------------
    def test_null_shortwave_radiation_raises_weather_api_error(self):
        """fetch_weather() must raise WeatherAPIError when shortwave_radiation is null."""
        from unittest.mock import patch, MagicMock

        null_response = {
            "current": {
                "wind_speed_10m": 5.0,
                "precipitation": 0.0,
                "temperature_2m": 65.0,
                "shortwave_radiation": None,  # JSON null
                "uv_index": 6.0,
                "direct_normal_irradiance": 400.0,
                "cloud_cover": 20,
                "cloud_cover_low": 10,
                "cloud_cover_mid": 5,
                "cloud_cover_high": 5,
                "is_day": 1,
                "time": "2026-04-17T13:00",
            },
            "daily": {
                "sunrise": ["2026-04-17T06:00"],
                "sunset": ["2026-04-17T20:00"],
            },
        }

        mock_response = MagicMock()
        mock_response.json.return_value = null_response
        mock_response.raise_for_status.return_value = None

        with patch("awning_automation.requests.get", return_value=mock_response):
            with self.assertRaises(WeatherAPIError) as ctx:
                fetch_weather(37.7, -122.4)
            self.assertIn("null", str(ctx.exception).lower())
            self.assertIn("shortwave_radiation", str(ctx.exception))

    # ------------------------------------------------------------------
    # Test 12 — H-1: null uv_index raises WeatherAPIError
    # ------------------------------------------------------------------
    def test_null_uv_index_raises_weather_api_error(self):
        """fetch_weather() must raise WeatherAPIError when uv_index is null."""
        from unittest.mock import patch, MagicMock

        null_response = {
            "current": {
                "wind_speed_10m": 5.0,
                "precipitation": 0.0,
                "temperature_2m": 65.0,
                "shortwave_radiation": 500.0,
                "uv_index": None,  # JSON null
                "direct_normal_irradiance": 400.0,
                "cloud_cover": 20,
                "cloud_cover_low": 10,
                "cloud_cover_mid": 5,
                "cloud_cover_high": 5,
                "is_day": 1,
                "time": "2026-04-17T13:00",
            },
            "daily": {
                "sunrise": ["2026-04-17T06:00"],
                "sunset": ["2026-04-17T20:00"],
            },
        }

        mock_response = MagicMock()
        mock_response.json.return_value = null_response
        mock_response.raise_for_status.return_value = None

        with patch("awning_automation.requests.get", return_value=mock_response):
            with self.assertRaises(WeatherAPIError) as ctx:
                fetch_weather(37.7, -122.4)
            self.assertIn("null", str(ctx.exception).lower())
            self.assertIn("uv_index", str(ctx.exception))

    # ------------------------------------------------------------------
    # Test 16 — Null DNI from Open-Meteo raises WeatherAPIError (fail-safe)
    # If direct_normal_irradiance is JSON null, fetch_weather() must raise
    # WeatherAPIError rather than returning None, which would crash later
    # at `dni >= min_dni` with TypeError — bypassing the fail-safe close.
    # ------------------------------------------------------------------
    def test_null_direct_normal_irradiance_raises_weather_api_error(self):
        """fetch_weather() must raise WeatherAPIError when direct_normal_irradiance is null."""
        from unittest.mock import patch, MagicMock

        null_response = {
            "current": {
                "wind_speed_10m": 5.0,
                "precipitation": 0.0,
                "temperature_2m": 65.0,
                "shortwave_radiation": 500.0,
                "uv_index": 6.0,
                "direct_normal_irradiance": None,  # JSON null
                "cloud_cover": 20,
                "cloud_cover_low": 10,
                "cloud_cover_mid": 5,
                "cloud_cover_high": 5,
                "is_day": 1,
                "time": "2026-04-17T13:00",
            },
            "daily": {
                "sunrise": ["2026-04-17T06:00"],
                "sunset": ["2026-04-17T20:00"],
            },
        }

        mock_response = MagicMock()
        mock_response.json.return_value = null_response
        mock_response.raise_for_status.return_value = None

        with patch("awning_automation.requests.get", return_value=mock_response):
            with self.assertRaises(WeatherAPIError) as ctx:
                fetch_weather(37.7, -122.4)
            self.assertIn("null", str(ctx.exception).lower())
            self.assertIn("direct_normal_irradiance", str(ctx.exception))

    # ------------------------------------------------------------------
    # Test 17 — Null cloud_cover from Open-Meteo raises WeatherAPIError
    # Same fail-safe concern: cloud_cover is a Layer 2 decision input.
    # JSON null must raise WeatherAPIError, not propagate as None.
    # ------------------------------------------------------------------
    def test_null_cloud_cover_raises_weather_api_error(self):
        """fetch_weather() must raise WeatherAPIError when cloud_cover is null."""
        from unittest.mock import patch, MagicMock

        null_response = {
            "current": {
                "wind_speed_10m": 5.0,
                "precipitation": 0.0,
                "temperature_2m": 65.0,
                "shortwave_radiation": 500.0,
                "uv_index": 6.0,
                "direct_normal_irradiance": 400.0,
                "cloud_cover": None,  # JSON null
                "cloud_cover_low": 10,
                "cloud_cover_mid": 5,
                "cloud_cover_high": 5,
                "is_day": 1,
                "time": "2026-04-17T13:00",
            },
            "daily": {
                "sunrise": ["2026-04-17T06:00"],
                "sunset": ["2026-04-17T20:00"],
            },
        }

        mock_response = MagicMock()
        mock_response.json.return_value = null_response
        mock_response.raise_for_status.return_value = None

        with patch("awning_automation.requests.get", return_value=mock_response):
            with self.assertRaises(WeatherAPIError) as ctx:
                fetch_weather(37.7, -122.4)
            self.assertIn("null", str(ctx.exception).lower())
            self.assertIn("cloud_cover", str(ctx.exception))


class TestObservationalSunnyGate(unittest.TestCase):
    """Tests for the new Layer 2 observational sunny gate (DNI + cloud_cover)."""

    # ------------------------------------------------------------------
    # Test 13 — Today's failure scenario: GHI=500, UV=1.1, DNI=10, cloud=95
    # Model gate: GHI=500 >= 400 → sunny_model=True
    # Observed gate: DNI=10 < 50 AND cloud=95 >= 80 → sunny_observed=False
    # is_sunny = True AND False = False → awning must NOT open
    # ------------------------------------------------------------------
    def test_today_failure_high_ghi_low_dni_high_cloud_does_not_open(self):
        """Today's failure: GHI=500 triggers model gate but DNI=10 + cloud=95 block observational gate."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=500.0,
                uv_index=1.1,
                dni=10.0,
                cloud_cover=95.0,
            ),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            conditions["sunny"],
            f"Expected sunny=False (observational gate blocks) but got True. reason={reason!r}",
        )
        self.assertFalse(
            should_open,
            f"Expected awning to stay closed but got True. reason={reason!r}",
        )
        self.assertIn("observed failed", reason)

    # ------------------------------------------------------------------
    # Test 14 — Partly cloudy morning: GHI=450, UV=4.5, DNI=80, cloud=60
    # Model gate: GHI=450 >= 400 AND UV=4.5 >= 4 → sunny_model=True
    # Observed gate: DNI=80 >= 50 → sunny_observed=True
    # is_sunny = True → awning should open
    # ------------------------------------------------------------------
    def test_partly_cloudy_morning_high_dni_opens(self):
        """Partly cloudy: GHI=450, UV=4.5, DNI=80, cloud=60 → both layers pass → opens."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=450.0,
                uv_index=4.5,
                dni=80.0,
                cloud_cover=60.0,
            ),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            conditions["sunny"],
            f"Expected sunny=True for partly-cloudy morning but got False. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"Expected awning to open for partly-cloudy morning but got False. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # Test 15 — Overcast with low DNI: GHI=755, UV=1.1, DNI=14, cloud=100
    # Mirrors exact verbatim log data from today (2026-04-28).
    # Model gate: GHI=755 >= 400 → sunny_model=True
    # Observed gate: DNI=14 < 50 AND cloud=100 >= 80 → sunny_observed=False
    # is_sunny = False → awning must NOT open
    # ------------------------------------------------------------------
    def test_overcast_low_dni_high_cloud_does_not_open(self):
        """Today's verbatim log data: GHI=755, UV=1.1, DNI=14, cloud=100 → observed gate blocks open."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=755.0,
                uv_index=1.1,
                dni=14.0,
                cloud_cover=100.0,
            ),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            conditions["sunny"],
            f"Expected sunny=False for overcast (DNI=14, cloud=100) but got True. reason={reason!r}",
        )
        self.assertFalse(
            should_open,
            f"Expected awning to stay closed for overcast but got True. reason={reason!r}",
        )
        self.assertIn("observed failed", reason)


if __name__ == "__main__":
    unittest.main()
