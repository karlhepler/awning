"""
Unit tests for awning_automation.py

Tests are hermetic — should_open_awning() takes all inputs as parameters so no
mocking of external I/O is required.

Run:  python3 -m unittest test_awning_automation.py -v
"""
import inspect
import os
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

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
    cloud_cover_low=10.0,
    cloud_cover_mid=5.0,
    cloud_cover_high=10.0,
    sunrise="2026-04-17T06:00:00",
    sunset="2026-04-17T20:00:00",
    # Rain gate fields — defaults represent "no rain" across all signals.
    # weather_code=0: WMO "clear sky" (not in rain/drizzle/thunderstorm set).
    # hourly_precip_prob=0: 0% ensemble rain probability (well below 20% threshold).
    # minutely_15_precip=[]: empty recent-history window (no rain in last ~30 min).
    weather_code=0,
    hourly_precip_prob=0,
    minutely_15_precip=None,
):
    # cloud_cover feeds Layer 2 (observational sunny gate).
    # cloud_cover_mid feeds Layer 3 (overcast ceiling).
    # Defaults represent a clear-to-partly-cloudy day:
    #   cloud_cover=20%, cloud_cover_mid=5% (well below thresholds).
    if minutely_15_precip is None:
        minutely_15_precip = []
    return {
        "wind_speed_10m": wind_speed,
        "precipitation": precipitation,
        "weather_code": weather_code,
        "temperature": temperature,
        "shortwave_radiation": shortwave_radiation,
        "uv_index": uv_index,
        "dni": dni,
        "cloud_cover": cloud_cover,
        "cloud_cover_low": cloud_cover_low,
        "cloud_cover_mid": cloud_cover_mid,
        "cloud_cover_high": cloud_cover_high,
        "sunrise": sunrise,
        "sunset": sunset,
        "hourly_precip_prob": hourly_precip_prob,
        "minutely_15_precip": minutely_15_precip,
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
    min_temperature_f=45.0,
    overcast_threshold=95.0,
    min_dni_cirrus=30.0,
    rain_probability_threshold=20,
)

# Daytime moment that falls between the default sunrise/sunset strings above
_DAYTIME = datetime(2026, 4, 17, 13, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Import functions under test
# ---------------------------------------------------------------------------
import awning_automation
from awning_automation import should_open_awning, ConfigurationError, get_thresholds, WeatherAPIError, fetch_weather, evaluate_rain_gate


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
    # All three signals below threshold — heavy overcast, no shade needed.
    # dni=20.0 is set explicitly (below the _weather() default of 450, which would
    # otherwise trigger the Layer 1 DNI direct-beam bypass added in card #95 and
    # falsely flip this "genuinely overcast" scenario to sunny) — 20 W/m² is
    # squarely in the overcast/rain DNI range, consistent with "no direct beam".
    # ------------------------------------------------------------------
    def test_genuinely_overcast_stays_closed(self):
        """Overcast: GHI=100, UV=1, DNI=20 all below thresholds → stays closed."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(shortwave_radiation=100.0, uv_index=1.0, dni=20.0),
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
    # dni=0.0 is set explicitly (below the _weather() default of 450, which would
    # otherwise trigger the Layer 1 DNI direct-beam bypass added in card #95 and
    # falsely flip sunny=True at night) — 0 W/m² is the physically correct DNI
    # reading at night (no direct beam at all).
    # ------------------------------------------------------------------
    def test_night_sun_below_horizon(self):
        """Night: altitude=-10, GHI=0, UV=0, DNI=0 → closed; sunny, sun_high, daytime all False."""
        # Use a time well outside the default sunrise/sunset window
        nighttime = datetime(2026, 4, 17, 2, 0, 0, tzinfo=timezone.utc)
        should_open, reason, conditions = should_open_awning(
            weather=_weather(shortwave_radiation=0.0, uv_index=0.0, dni=0.0),
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

        with patch.object(awning_automation._weather_session, "get", return_value=mock_response):
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

        with patch.object(awning_automation._weather_session, "get", return_value=mock_response):
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

        with patch.object(awning_automation._weather_session, "get", return_value=mock_response):
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

        with patch.object(awning_automation._weather_session, "get", return_value=mock_response):
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


class TestOvercastCeilingGate(unittest.TestCase):
    """Tests for the Layer 3 cloud-cover hard ceiling (OVERCAST_THRESHOLD_PCT)."""

    # ------------------------------------------------------------------
    # Test — overcast ceiling fires when cloud model says overcast AND DNI
    # is below the cirrus guard threshold (genuine mid-level overcast).
    #
    # Scenario design: isolate Layer 3 as the blocking gate:
    #   Layer 1: GHI=850 >= 400 → sunny_model=True
    #   Layer 2: cloud=60 < 80 → sunny_observed=True (cloud arm rescues low DNI=14)
    #   Layer 3: max(cloud_mid=100, cloud_high=78)=100 >= 95 ceiling
    #            AND DNI=14 < 30 (min_dni_cirrus guard) → guard does NOT rescue
    #            → not_overcast=False → sunny=False
    # ------------------------------------------------------------------
    def test_overcast_ceiling_blocks_open(self):
        """Overcast ceiling: GHI=850, UV=2.7, DNI=14, cloud=60, cloud_mid=100 → Layer 3 blocks open."""
        # Using cloud=60 (total) to pass Layer 2 via cloud arm while cloud_mid=100
        # represents true altostratus overcast that physically blocks direct sun.
        # DNI=14 is in the overcast/rain range (4-14 W/m²) — well below the
        # min_dni_cirrus guard of 30, so the guard does NOT rescue.
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=850.0,
                uv_index=2.7,
                dni=14.0,
                cloud_cover=60.0,
                cloud_cover_low=5.0,
                cloud_cover_mid=100.0,
                cloud_cover_high=78.0,
                temperature=70.0,
            ),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            conditions["sunny"],
            f"Expected sunny=False (overcast ceiling blocks) but got True. reason={reason!r}",
        )
        self.assertFalse(
            should_open,
            f"Expected awning closed (overcast ceiling) but got True. reason={reason!r}",
        )
        self.assertIn("overcast", reason.lower())

    # ------------------------------------------------------------------
    # Test — 2026-04-28 incident: high-level cirrostratus (cloud_cover_high=99%)
    # with cloud_cover_mid=53-70% fully blocked direct sun while awning was open.
    # The overcast ceiling uses max(cloud_cover_mid, cloud_cover_high) so that
    # thick high-level cloud cover alone is sufficient to fire the ceiling gate.
    #
    # NEW behavior (post DNI guard): the ceiling fires ONLY when BOTH the cloud
    # model says overcast AND DNI is below the cirrus guard threshold. This test
    # uses low DNI (14 W/m² — overcast/rain range) so the guard does NOT rescue,
    # and the ceiling correctly fires.
    #
    # Scenario: low=46, mid=5, high=100, total=100, GHI=860, UV=4.0, DNI=14
    # Layer 1: GHI=860 >= 400 → sunny_model=True
    # Layer 2: DNI=14 < 50 AND cloud=100 >= 80 → sunny_observed=False
    # (Test correctly shows ceiling + observed gate both fire; ceiling trace visible)
    #
    # Note: if DNI were >= 30 (min_dni_cirrus guard), the DNI guard would bypass
    # the ceiling — which is the intended behavior for the 2026-05-12 incident.
    # See test_dni_guard_overrides_high_cloud_false_positive for that regression.
    # ------------------------------------------------------------------
    def test_high_cirrus_triggers_overcast_ceiling(self):
        """High cirrus + low DNI: cloud_high=100%, DNI=14 < guard=30 → ceiling fires → stays closed."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=860.0,
                uv_index=4.0,
                dni=14.0,
                cloud_cover=100.0,
                cloud_cover_low=46.0,
                cloud_cover_mid=5.0,
                cloud_cover_high=100.0,
                temperature=65.0,
            ),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            conditions["sunny"],
            f"Expected sunny=False (high cirrus + low DNI should trigger ceiling) but got True. reason={reason!r}",
        )
        self.assertFalse(
            should_open,
            f"Expected awning to stay closed (high cirrus + low DNI) but got True. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # Regression test — 2026-05-12 incident:
    #   cloud_cover_high=100% (bad Open-Meteo model data), cloud_cover_mid=0%,
    #   DNI=905 W/m² (peak midday sun), direct visual observation confirmed clear sky.
    #
    # OLD behavior: not_overcast = max(0, 100)=100 >= 95 → False → closed (WRONG)
    # NEW behavior: DNI guard: 905 >= 30 (min_dni_cirrus) → not_overcast=True → opens
    #
    # Layer 1: GHI=1010 >= 400, UV=7.2 >= 4 → sunny_model=True
    # Layer 2: DNI=905 >= 50 → sunny_observed=True
    # Layer 3: max(mid=0, high=100)=100 >= 95 BUT DNI=905 >= 30 (guard) → not_overcast=True
    # All conditions pass → should_open=True
    # ------------------------------------------------------------------
    def test_dni_guard_overrides_high_cloud_false_positive(self):
        """2026-05-12 incident: cloud_cover_high=100% bad model data + DNI=905 W/m² → DNI guard fires → awning opens."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=1010.0,
                uv_index=7.2,
                dni=905.0,
                cloud_cover=100.0,
                cloud_cover_low=0.0,
                cloud_cover_mid=0.0,
                cloud_cover_high=100.0,
                temperature=72.0,
            ),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            conditions["sunny"],
            f"Expected sunny=True (DNI guard should override cloud_high=100%) but got False. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"Expected awning to open (DNI=905 confirms direct sun) but got False. reason={reason!r}",
        )
        # Verify the DNI guard trace appears in the reason
        self.assertIn("DNI guard", reason, f"Expected 'DNI guard' in reason trace. reason={reason!r}")

    def test_partly_cloudy_below_ceiling_still_opens(self):
        """Partly cloudy below ceiling: GHI=600, UV=4, DNI=300, cloud=85, cloud_mid=30 → all layers pass → opens."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=600.0,
                uv_index=4.0,
                dni=300.0,
                cloud_cover=85.0,
                cloud_cover_mid=30.0,
                temperature=70.0,
            ),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            conditions["sunny"],
            f"Expected sunny=True (below ceiling) but got False. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"Expected awning to open (below ceiling) but got False. reason={reason!r}",
        )


class TestDirectBeamBypass(unittest.TestCase):
    """
    Tests for card #95: the Layer 1 DNI direct-beam bypass (MIN_DNI_DIRECT_WM2)
    and the env-configurable sun-facing-window azimuth arc (SUN_AZIMUTH_MIN_DEG /
    SUN_AZIMUTH_MAX_DEG).

    Background — 2026-08-13 incident: at 08:30 local, GHI=277 W/m² and UV=1.7 both
    failed their thresholds (GHI is horizontal-plane irradiance, suppressed by the
    sin(altitude) projection factor at low sun elevation) while DNI=498 W/m² was
    unambiguous direct sun; separately, azimuth=87.9° was below the old hardcoded
    90° floor despite the user directly observing sun on the window. Both gates
    had to move for the awning to open at 08:30.
    """

    # ------------------------------------------------------------------
    # Test — today's real 08:30 values: GHI=277, UV=1.7, DNI=498, cloud 0/0/1/1,
    # azimuth=87.9, altitude=22.5, 78.2°F, wind=2.7mph, no rain => OPEN.
    # The DNI direct-beam bypass (DNI 498 >= 450) fires the model layer despite
    # GHI/UV both failing, and azimuth 87.9 clears the 60° floor (southeast window).
    # ------------------------------------------------------------------
    def test_direct_beam_real_0830_values_opens(self):
        """08:30 real values (GHI 277, UV 1.7, DNI 498, azimuth 87.9) => OPEN."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=277.0,
                uv_index=1.7,
                dni=498.0,
                cloud_cover=0.0,
                cloud_cover_low=0.0,
                cloud_cover_mid=1.0,
                cloud_cover_high=1.0,
                temperature=78.2,
                wind_speed=2.7,
            ),
            sun_position=_sun(azimuth=87.9, altitude=22.5),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            conditions["sunny"],
            f"Expected sunny=True via DNI direct-beam bypass but got False. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"Expected awning to open for 08:30 real values but got False. reason={reason!r}",
        )
        self.assertIn(
            "DNI direct-beam bypass", reason,
            f"Expected the DNI direct-beam bypass to be named in the reason trace. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # Test — today's real 08:15 values: GHI=226, UV=1.4, DNI=445, azimuth=85.7,
    # altitude=19.5 => still CLOSED. DNI=445 is just below MIN_DNI_DIRECT_WM2=450,
    # proving 450 is a real boundary and not a rubber stamp that opens for any
    # DNI reading in the ballpark. altitude_threshold is lowered to 15.0 for this
    # test specifically so sun_high passes and the close is attributable ONLY to
    # the sunny gate (model layer) — isolating the DNI boundary from the
    # unrelated altitude gate that the raw 08:15 altitude (19.5) would otherwise
    # also fail against the default 20° threshold. azimuth=85.7 is comfortably
    # within the 60°-249° arc, so it does not confound the DNI-boundary isolation.
    # ------------------------------------------------------------------
    def test_direct_beam_real_0815_values_stays_closed(self):
        """08:15 real values (GHI 226, UV 1.4, DNI 445) => still CLOSED (450 is a real boundary)."""
        thresholds = {**_THRESHOLDS, "altitude_threshold": 15.0}
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=226.0,
                uv_index=1.4,
                dni=445.0,
                cloud_cover=0.0,
                cloud_cover_low=0.0,
                cloud_cover_mid=1.0,
                cloud_cover_high=1.0,
                temperature=76.0,
                wind_speed=2.5,
            ),
            sun_position=_sun(azimuth=85.7, altitude=19.5),
            current_time=_DAYTIME,
            **thresholds,
        )
        self.assertFalse(
            conditions["sunny"],
            f"Expected sunny=False for DNI=445 < 450 boundary but got True. reason={reason!r}",
        )
        # Isolation: every other gate must pass, so the close is attributable
        # ONLY to the sunny gate — proving the DNI boundary itself is decisive.
        self.assertTrue(conditions["calm"], f"Expected calm=True. reason={reason!r}")
        self.assertTrue(conditions["no_rain"], f"Expected no_rain=True. reason={reason!r}")
        self.assertTrue(conditions["above_freezing"], f"Expected above_freezing=True. reason={reason!r}")
        self.assertTrue(conditions["daytime"], f"Expected daytime=True. reason={reason!r}")
        self.assertTrue(conditions["sun_high"], f"Expected sun_high=True (altitude_threshold lowered). reason={reason!r}")
        self.assertTrue(conditions["sun_facing_window"], f"Expected sun_facing_window=True (azimuth 85.7 within 60°-249° arc). reason={reason!r}")
        self.assertFalse(
            should_open,
            f"Expected awning closed for 08:15 real values (DNI=445 < 450) but got True. reason={reason!r}",
        )
        self.assertIn("Not sunny", reason)
        self.assertIn("445", reason)
        self.assertIn("450", reason)

    # ------------------------------------------------------------------
    # Test — bright-overcast morning: GHI=260, UV=1.5, DNI=20, cloud total 95%
    # => still CLOSED. Proves diffuse light (bright overcast) cannot defeat the
    # new DNI bypass — DNI=20 is squarely in the overcast/rain range, nowhere
    # near the 450 W/m² threshold.
    # ------------------------------------------------------------------
    def test_direct_beam_bright_overcast_diffuse_light_stays_closed(self):
        """Bright overcast (GHI 260, UV 1.5, DNI 20, cloud 95%) => still CLOSED."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=260.0,
                uv_index=1.5,
                dni=20.0,
                cloud_cover=95.0,
                cloud_cover_low=50.0,
                cloud_cover_mid=90.0,
                cloud_cover_high=95.0,
                temperature=70.0,
                wind_speed=5.0,
            ),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            conditions["sunny"],
            f"Expected sunny=False for bright-overcast diffuse light but got True. reason={reason!r}",
        )
        self.assertFalse(
            should_open,
            f"Expected awning closed for bright-overcast diffuse light but got True. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # Test — azimuth AT the new SUN_AZIMUTH_MIN_DEG floor (60.0°, inclusive)
    # opens, with DNI=498 (direct-beam bypass) confirming sunny so the only
    # variable under test is the azimuth arc itself. 60° reflects the
    # southeast-facing window's confirmed sunrise-adjacent acceptance edge.
    # ------------------------------------------------------------------
    def test_direct_beam_azimuth_at_floor_opens(self):
        """Azimuth exactly at the 60° floor (inclusive) => sun_facing_window=True, opens."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=277.0,
                uv_index=1.7,
                dni=498.0,
                cloud_cover=0.0,
                cloud_cover_low=0.0,
                cloud_cover_mid=1.0,
                cloud_cover_high=1.0,
                temperature=78.2,
                wind_speed=2.7,
            ),
            sun_position=_sun(azimuth=60.0, altitude=22.5),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            conditions["sun_facing_window"],
            f"Expected sun_facing_window=True at azimuth=60.0 (floor, inclusive) but got False. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"Expected awning to open at azimuth=60.0 floor but got False. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # Test — azimuth just BELOW the new SUN_AZIMUTH_MIN_DEG floor (59.9°)
    # stays closed, proving the floor is a real boundary.
    # ------------------------------------------------------------------
    def test_direct_beam_azimuth_below_floor_stays_closed(self):
        """Azimuth just below the 60° floor (59.9°) => sun_facing_window=False, stays closed."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=277.0,
                uv_index=1.7,
                dni=498.0,
                cloud_cover=0.0,
                cloud_cover_low=0.0,
                cloud_cover_mid=1.0,
                cloud_cover_high=1.0,
                temperature=78.2,
                wind_speed=2.7,
            ),
            sun_position=_sun(azimuth=59.9, altitude=22.5),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            conditions["sun_facing_window"],
            f"Expected sun_facing_window=False at azimuth=59.9 (below floor) but got True. reason={reason!r}",
        )
        self.assertFalse(
            should_open,
            f"Expected awning closed at azimuth=59.9 (below floor) but got True. reason={reason!r}",
        )
        self.assertIn("Sun not facing window", reason)
        # Discriminator: the reason string must report the CONFIGURED arc bounds
        # (60°-249°), not the old hardcoded "90°-260°" literal that pre-change
        # code always printed regardless of any threshold value.
        self.assertIn(
            "need 60°-249°", reason,
            f"Expected reason to report the configured 60° floor, not a hardcoded value. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # Test — get_thresholds() resolves SUN_AZIMUTH_MIN_DEG/SUN_AZIMUTH_MAX_DEG to
    # 60.0/249.0 when both env vars are UNSET (i.e. exercising the os.getenv(...)
    # default fallback path directly, not a caller-supplied override). Pins the
    # corrected southeast-facing-window defaults named in the card's AC 8.
    # ------------------------------------------------------------------
    def test_azimuth_default_resolves_to_60_249_when_unset(self):
        """SUN_AZIMUTH_MIN_DEG/MAX_DEG unset => defaults resolve to 60.0 floor, 249.0 ceiling."""
        env_patch = {"WIND_SPEED_THRESHOLD_MPH": "15", "MIN_SUN_ALTITUDE_DEG": "20"}
        keys_to_clear = ["SUN_AZIMUTH_MIN_DEG", "SUN_AZIMUTH_MAX_DEG"]
        original_values = {}
        for k, v in env_patch.items():
            original_values[k] = os.environ.get(k)
            os.environ[k] = v
        for k in keys_to_clear:
            original_values[k] = os.environ.get(k)
            os.environ.pop(k, None)
        try:
            result = get_thresholds()
            # sun_azimuth_min is the 14th element (index 13), sun_azimuth_max the
            # 15th (index 14) of the get_thresholds() return tuple.
            self.assertEqual(
                result[13], 60.0,
                f"Expected SUN_AZIMUTH_MIN_DEG default to resolve to 60.0, got {result[13]}",
            )
            self.assertEqual(
                result[14], 249.0,
                f"Expected SUN_AZIMUTH_MAX_DEG default to resolve to 249.0, got {result[14]}",
            )
        finally:
            for k, orig in original_values.items():
                if orig is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = orig


class TestAzimuthCeilingObservedCalibration(unittest.TestCase):
    """
    Pins the 249° ceiling to the direct observation it came from, so a future
    edit cannot quietly walk it back the way the 2026-08-13 change did.

    On 2026-08-14 the operator watched the house shadow cross the back porch and
    identified 16:01 as the moment the awning should retract. pvlib puts the sun
    at azimuth 249.5° then; the 16:00 cron log recorded 249.3°. The three
    azimuths below are the real 15-minute cron slots bracketing that moment:

        15:45  az 245.7  -> still on the porch, stay open
        16:00  az 249.3  -> shadow has arrived, close   <- the observation
        16:15  az 252.5  -> closed

    These tests deliberately do NOT pass sun_azimuth_max, so they exercise
    should_open_awning's signature default (DEFAULT_SUN_AZIMUTH_MAX_DEG). A
    regression that changes the constant fails here, not just in the
    get_thresholds() default test.
    """

    def _conditions_at(self, azimuth, altitude):
        # Weather values are today's real 16:00 readings (clear, hot, dry) so the
        # only gate that can vary across these three cases is the azimuth arc.
        return should_open_awning(
            weather=_weather(
                shortwave_radiation=700.0,
                uv_index=6.0,
                dni=780.0,
                cloud_cover=10.0,
                cloud_cover_low=0.0,
                cloud_cover_mid=0.0,
                cloud_cover_high=0.0,
                temperature=96.0,
                wind_speed=6.0,
            ),
            sun_position=_sun(azimuth=azimuth, altitude=altitude),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )

    def test_1545_slot_azimuth_2457_stays_open(self):
        """15:45 on 2026-08-14 (az 245.7) => sun still on the porch, awning open."""
        should_open, reason, conditions = self._conditions_at(245.7, 51.0)
        self.assertTrue(
            conditions["sun_facing_window"],
            f"Expected sun_facing_window=True at azimuth=245.7 (below the 249° ceiling) "
            f"but got False — the ceiling has regressed below the observed value. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"Expected awning OPEN at the 15:45 slot but got closed. reason={reason!r}",
        )

    def test_1600_slot_azimuth_2493_closes(self):
        """16:00 on 2026-08-14 (az 249.3) => house shadow has arrived, awning closes."""
        should_open, reason, conditions = self._conditions_at(249.3, 48.2)
        self.assertFalse(
            conditions["sun_facing_window"],
            f"Expected sun_facing_window=False at azimuth=249.3 (past the 249° ceiling) "
            f"but got True. reason={reason!r}",
        )
        self.assertFalse(
            should_open,
            f"Expected awning CLOSED at the 16:00 slot — this is the moment the "
            f"operator identified. reason={reason!r}",
        )
        # Isolation: every other gate passes, so the close is attributable ONLY
        # to the azimuth ceiling.
        self.assertTrue(conditions["sunny"], f"Expected sunny=True. reason={reason!r}")
        self.assertTrue(conditions["calm"], f"Expected calm=True. reason={reason!r}")
        self.assertTrue(conditions["no_rain"], f"Expected no_rain=True. reason={reason!r}")
        self.assertTrue(conditions["sun_high"], f"Expected sun_high=True. reason={reason!r}")
        self.assertIn("Sun not facing window", reason)

    def test_1615_slot_azimuth_2525_stays_closed(self):
        """16:15 on 2026-08-14 (az 252.5) => well past the ceiling, still closed."""
        should_open, reason, conditions = self._conditions_at(252.5, 45.4)
        self.assertFalse(
            conditions["sun_facing_window"],
            f"Expected sun_facing_window=False at azimuth=252.5. reason={reason!r}",
        )
        self.assertFalse(should_open, f"Expected awning CLOSED. reason={reason!r}")

    def test_old_215_ceiling_would_have_closed_1430_slot(self):
        """
        Discriminator against the pre-2026-08-14 ceiling: azimuth 220.5 is the
        14:30 slot that the old 215° ceiling actually closed on. Under 249° it
        must stay OPEN — this is the ~1.5 hours of afternoon shade the change
        recovers, and it fails loudly if 215 is ever restored.
        """
        should_open, reason, conditions = self._conditions_at(220.5, 63.3)
        self.assertTrue(
            conditions["sun_facing_window"],
            f"Expected sun_facing_window=True at azimuth=220.5 — the old 215° ceiling "
            f"closed here on 2026-08-14 and that was too early. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"Expected awning OPEN at the 14:30 slot. reason={reason!r}",
        )


class TestTemperatureThreshold(unittest.TestCase):
    """Tests for the MIN_TEMPERATURE_F threshold (default 45°F)."""

    # ------------------------------------------------------------------
    # Test — full sun + favorable sun + temp=40 → should NOT open
    # Temperature 40°F is below the 45°F threshold.
    # ------------------------------------------------------------------
    def test_temperature_below_45_does_not_open(self):
        """Temp=40°F < 45°F threshold → awning stays closed despite full sun."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=700.0,
                uv_index=7.0,
                dni=450.0,
                cloud_cover=20.0,
                temperature=40.0,
            ),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            conditions["above_freezing"],
            f"Expected above_freezing=False for temp=40 but got True. reason={reason!r}",
        )
        self.assertFalse(
            should_open,
            f"Expected awning closed for temp=40 but got True. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # Test — full sun + favorable sun + temp=50 → should open
    # Temperature 50°F is above the 45°F threshold.
    # ------------------------------------------------------------------
    def test_temperature_above_45_can_open(self):
        """Temp=50°F > 45°F threshold → awning opens (full sun + favorable conditions)."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=700.0,
                uv_index=7.0,
                dni=450.0,
                cloud_cover=20.0,
                temperature=50.0,
            ),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            conditions["above_freezing"],
            f"Expected above_freezing=True for temp=50 but got False. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"Expected awning to open for temp=50 but got False. reason={reason!r}",
        )


class TestGetThresholdsMinTemperatureFValidation(unittest.TestCase):
    """Tests for MIN_TEMPERATURE_F range validation in get_thresholds()."""

    # Required env vars (no defaults in get_thresholds)
    _REQUIRED_ENV = {"WIND_SPEED_THRESHOLD_MPH": "15", "MIN_SUN_ALTITUDE_DEG": "20"}

    def test_min_temperature_f_below_range_raises(self):
        """MIN_TEMPERATURE_F=-999 is below -50 → ConfigurationError."""
        env = {**self._REQUIRED_ENV, "MIN_TEMPERATURE_F": "-999"}
        with unittest.mock.patch.dict(os.environ, env):
            with self.assertRaises(ConfigurationError):
                get_thresholds()

    def test_min_temperature_f_above_range_raises(self):
        """MIN_TEMPERATURE_F=200 is above 120 → ConfigurationError."""
        env = {**self._REQUIRED_ENV, "MIN_TEMPERATURE_F": "200"}
        with unittest.mock.patch.dict(os.environ, env):
            with self.assertRaises(ConfigurationError):
                get_thresholds()

    def test_min_temperature_f_at_lower_bound_is_valid(self):
        """MIN_TEMPERATURE_F=-50 is at the lower bound → no error."""
        env = {**self._REQUIRED_ENV, "MIN_TEMPERATURE_F": "-50"}
        with unittest.mock.patch.dict(os.environ, env):
            _, _, _, _, _, _, min_temperature_f, _, _, _, _, _, _, _, _, _ = get_thresholds()
            self.assertEqual(min_temperature_f, -50.0)

    def test_min_temperature_f_at_upper_bound_is_valid(self):
        """MIN_TEMPERATURE_F=120 is at the upper bound → no error."""
        env = {**self._REQUIRED_ENV, "MIN_TEMPERATURE_F": "120"}
        with unittest.mock.patch.dict(os.environ, env):
            _, _, _, _, _, _, min_temperature_f, _, _, _, _, _, _, _, _, _ = get_thresholds()
            self.assertEqual(min_temperature_f, 120.0)


class TestWeatherRetryBehavior(unittest.TestCase):
    """Tests for urllib3-level retry behavior introduced in card #45.

    All four tests patch urllib3.HTTPSConnectionPool._make_request — the lowest
    level that urllib3's internal retry loop calls on each attempt.  This is the
    correct target: the retry loop lives inside urllib3.urlopen() and calls
    _make_request recursively on each attempt, so a side_effect list on
    _make_request produces one entry per attempt, exactly mirroring the real
    retry sequence.

    Patching at session.get (the existing test pattern) would bypass the retry
    adapter entirely and is not suitable for these tests.
    """

    # Shared valid weather JSON that fetch_weather() will accept.
    # Includes representative hourly and minutely_15 blocks (clear-sky values)
    # so the fixture is self-contained and reusable in future tests that call
    # evaluate_rain_gate() or should_open_awning() after fetch_weather().
    _GOOD_JSON = {
        "current": {
            "wind_speed_10m": 5.0,
            "precipitation": 0.0,
            "temperature_2m": 65.0,
            "shortwave_radiation": 500.0,
            "uv_index": 6.0,
            "direct_normal_irradiance": 400.0,
            "cloud_cover": 20,
            "cloud_cover_low": 10,
            "cloud_cover_mid": 5,
            "cloud_cover_high": 5,
            "is_day": 1,
            "time": "2026-04-17T13:00",
            "weather_code": 0,
        },
        "daily": {
            "sunrise": ["2026-04-17T06:00"],
            "sunset": ["2026-04-17T20:00"],
        },
        # Clear-sky hourly block: 0% precipitation probability for the current hour.
        "hourly": {
            "time": ["2026-04-17T13:00"],
            "precipitation_probability": [0],
        },
        # Clear-sky minutely_15 block: three 15-min windows with 0 mm precipitation.
        "minutely_15": {
            "time": [
                "2026-04-17T12:30",
                "2026-04-17T12:45",
                "2026-04-17T13:00",
            ],
            "precipitation": [0.0, 0.0, 0.0],
        },
    }

    @staticmethod
    def _make_urllib3_resp(status, body=b""):
        """Build a minimal urllib3.response.HTTPResponse suitable for retry tests."""
        import io
        import urllib3
        return urllib3.response.HTTPResponse(
            body=io.BytesIO(body),
            status=status,
            headers={"Content-Type": "application/json"},
            preload_content=False,
        )

    def _make_resp_503(self):
        return self._make_urllib3_resp(503, b"Service Unavailable")

    def _make_resp_200(self):
        import json
        return self._make_urllib3_resp(200, json.dumps(self._GOOD_JSON).encode())

    # ------------------------------------------------------------------
    # Test — 503 retry fires: first attempt returns 503, second returns 200.
    # fetch_weather() must succeed (retry kicked in) and _make_request must
    # have been called more than once, proving the retry adapter engaged.
    # ------------------------------------------------------------------
    def test_weather_retry_fires_on_503(self):
        """503 on first attempt → urllib3 retry fires → second attempt returns 200 → fetch_weather succeeds."""
        import urllib3
        from unittest.mock import patch

        with patch.object(
            urllib3.HTTPSConnectionPool,
            "_make_request",
            side_effect=[self._make_resp_503(), self._make_resp_200()],
        ) as mock_req:
            result = fetch_weather(37.7, -122.4)

        self.assertEqual(result["shortwave_radiation"], 500.0)
        self.assertGreater(
            mock_req.call_count,
            1,
            "Expected _make_request to be called more than once (retry fired), "
            f"but it was called {mock_req.call_count} time(s)",
        )

    # ------------------------------------------------------------------
    # Test — retry exhaustion raises WeatherAPIError, not raw urllib3 error.
    # All attempts return 503.  After retries exhaust, fetch_weather() must
    # raise WeatherAPIError (not MaxRetryError or requests.HTTPError).
    # ------------------------------------------------------------------
    def test_weather_retry_exhaustion_raises_WeatherAPIError(self):
        """All attempts return 503 → retries exhaust → fetch_weather raises WeatherAPIError."""
        import urllib3
        from unittest.mock import patch
        from awning_automation import _WEATHER_RETRY_TOTAL

        # One initial attempt + _WEATHER_RETRY_TOTAL retries
        responses_503 = [
            self._make_resp_503()
            for _ in range(_WEATHER_RETRY_TOTAL + 1)
        ]

        with patch.object(
            urllib3.HTTPSConnectionPool,
            "_make_request",
            side_effect=responses_503,
        ):
            with self.assertRaises(WeatherAPIError) as ctx:
                fetch_weather(37.7, -122.4)

        # Must be our domain exception, not a raw urllib3 or requests error
        self.assertIsInstance(ctx.exception, WeatherAPIError)

    # ------------------------------------------------------------------
    # Test — fail-safe close runs after retry exhaustion.
    # Compose with #2: when fetch_weather() raises WeatherAPIError, main()'s
    # exception handler must trigger the fail-safe close path.  Assert that
    # controller.close() is called and a Telegram failure notification fires
    # (Telegram fires only after full retry exhaustion — correct behavior).
    # ------------------------------------------------------------------
    def test_failsafe_close_runs_after_weather_retry_exhaustion(self):
        """WeatherAPIError in fetch_weather → main() fail-safe: close called + Telegram fires."""
        import sys
        from unittest.mock import patch, MagicMock

        mock_controller = MagicMock()
        mock_controller.get_state.return_value = 1  # Awning is open → fail-safe should close it

        mock_log_path = MagicMock()
        mock_log_path.parent = MagicMock()

        with patch.object(sys, "argv", ["awning_automation.py"]):
            with patch.object(awning_automation, "setup_logging", return_value=mock_log_path):
                with patch.object(awning_automation, "load_location_config", return_value=(37.7, -122.4)):
                    with patch.object(awning_automation, "get_thresholds", return_value=(15, 20, 400, 4, 50, 80, 60, 95, 30, 20, 400.0, 15.0, 450.0, 60.0, 249.0, True)):
                        with patch.object(awning_automation, "load_telegram_config", return_value=("fake_token", "fake_chat")):
                            with patch.object(
                                awning_automation,
                                "collect_weather_measurements",
                                side_effect=WeatherAPIError("Simulated API exhaustion"),
                            ):
                                with patch.object(awning_automation, "create_controller_from_env", return_value=mock_controller):
                                    with patch.object(awning_automation, "send_telegram_notification") as mock_telegram:
                                        with self.assertRaises(SystemExit):
                                            awning_automation.main()

        mock_controller.close.assert_called_once()

        # Telegram must fire exactly once with the fail-safe message (exhaustion case)
        mock_telegram.assert_called_once()
        call_args = mock_telegram.call_args
        self.assertIn("fail-safe", call_args[0][2].lower())

    # ------------------------------------------------------------------
    # Test — Telegram is NOT pinged on individual retries.
    # 503 once then 200: fetch_weather() retries and succeeds.  Telegram
    # must be silent during the retry — it only fires on state changes
    # (handled in main()) or final exhaustion, never on intermediate retries.
    # ------------------------------------------------------------------
    def test_telegram_not_pinged_on_individual_retries(self):
        """503 then 200 retry succeeds → send_telegram_notification never called during retry."""
        import urllib3
        from unittest.mock import patch

        with patch.object(
            urllib3.HTTPSConnectionPool,
            "_make_request",
            side_effect=[self._make_resp_503(), self._make_resp_200()],
        ):
            with patch.object(awning_automation, "send_telegram_notification") as mock_telegram:
                result = fetch_weather(37.7, -122.4)

        # Verify fetch_weather succeeded (the retry recovered)
        self.assertEqual(result["shortwave_radiation"], 500.0)

        # Telegram must NOT have been called during the retry
        mock_telegram.assert_not_called()


class TestFetchWeatherNullCloudCoverMidHigh(unittest.TestCase):
    """Tests for null guards on cloud_cover_mid and cloud_cover_high in fetch_weather()."""

    # ------------------------------------------------------------------
    # Test — Null cloud_cover_mid raises WeatherAPIError (Layer 3 input).
    # cloud_cover_mid drives the Layer 3 overcast ceiling. A JSON null must
    # raise WeatherAPIError explicitly rather than being silently coerced to
    # 100% by the .get(..., 100) default, which would lose observability.
    # ------------------------------------------------------------------
    def test_null_cloud_cover_mid_raises_weather_api_error(self):
        """fetch_weather() must raise WeatherAPIError when cloud_cover_mid is null."""
        from unittest.mock import patch, MagicMock

        null_response = {
            "current": {
                "wind_speed_10m": 5.0,
                "precipitation": 0.0,
                "temperature_2m": 65.0,
                "shortwave_radiation": 500.0,
                "uv_index": 6.0,
                "direct_normal_irradiance": 400.0,
                "cloud_cover": 20,
                "cloud_cover_low": 10,
                "cloud_cover_mid": None,  # JSON null
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

        with patch.object(awning_automation._weather_session, "get", return_value=mock_response):
            with self.assertRaises(WeatherAPIError) as ctx:
                fetch_weather(37.7, -122.4)
            self.assertIn("null", str(ctx.exception).lower())
            self.assertIn("cloud_cover_mid", str(ctx.exception))

    # ------------------------------------------------------------------
    # Test — Null cloud_cover_high raises WeatherAPIError (Layer 3 input).
    # cloud_cover_high is used alongside cloud_cover_mid in the Layer 3
    # ceiling expression max(cloud_cover_mid, cloud_cover_high). A JSON null
    # must raise WeatherAPIError rather than silently coercing to 100%.
    # ------------------------------------------------------------------
    def test_null_cloud_cover_high_raises_weather_api_error(self):
        """fetch_weather() must raise WeatherAPIError when cloud_cover_high is null."""
        from unittest.mock import patch, MagicMock

        null_response = {
            "current": {
                "wind_speed_10m": 5.0,
                "precipitation": 0.0,
                "temperature_2m": 65.0,
                "shortwave_radiation": 500.0,
                "uv_index": 6.0,
                "direct_normal_irradiance": 400.0,
                "cloud_cover": 20,
                "cloud_cover_low": 10,
                "cloud_cover_mid": 5,
                "cloud_cover_high": None,  # JSON null
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

        with patch.object(awning_automation._weather_session, "get", return_value=mock_response):
            with self.assertRaises(WeatherAPIError) as ctx:
                fetch_weather(37.7, -122.4)
            self.assertIn("null", str(ctx.exception).lower())
            self.assertIn("cloud_cover_high", str(ctx.exception))

    # ------------------------------------------------------------------
    # Test — Null cloud_cover_low raises WeatherAPIError (rain-gate veto input).
    # cloud_cover_low feeds max(cloud_cover_low, cloud_cover_mid) in the
    # provably-clear forecast veto and the radar clear-sky veto. A JSON null
    # must raise WeatherAPIError (fail-safe close path) rather than flowing
    # through to max(None, cloud_cover_mid) and raising a silent TypeError
    # that bypasses the fail-safe (fail-open bug).
    # ------------------------------------------------------------------
    def test_null_cloud_cover_low_raises_weather_api_error(self):
        """fetch_weather() must raise WeatherAPIError when cloud_cover_low is null."""
        from unittest.mock import patch, MagicMock

        null_response = {
            "current": {
                "wind_speed_10m": 5.0,
                "precipitation": 0.0,
                "temperature_2m": 65.0,
                "shortwave_radiation": 500.0,
                "uv_index": 6.0,
                "direct_normal_irradiance": 400.0,
                "cloud_cover": 20,
                "cloud_cover_low": None,  # JSON null
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

        with patch.object(awning_automation._weather_session, "get", return_value=mock_response):
            with self.assertRaises(WeatherAPIError) as ctx:
                fetch_weather(37.7, -122.4)
            self.assertIn("null", str(ctx.exception).lower())
            self.assertIn("cloud_cover_low", str(ctx.exception))


class TestGetThresholdsMinDniCirrusWM2Validation(unittest.TestCase):
    """Tests for MIN_DNI_CIRRUS_WM2 validation in get_thresholds()."""

    # Required env vars (no defaults in get_thresholds)
    _REQUIRED_ENV = {"WIND_SPEED_THRESHOLD_MPH": "15", "MIN_SUN_ALTITUDE_DEG": "20"}

    def test_min_dni_cirrus_zero_raises(self):
        """MIN_DNI_CIRRUS_WM2=0 silently disables Layer 3 ceiling → ConfigurationError."""
        env = {**self._REQUIRED_ENV, "MIN_DNI_CIRRUS_WM2": "0"}
        with unittest.mock.patch.dict(os.environ, env):
            with self.assertRaises(ConfigurationError) as ctx:
                get_thresholds()
        self.assertIn("0", str(ctx.exception))

    def test_min_dni_cirrus_above_min_dni_raises(self):
        """MIN_DNI_CIRRUS_WM2 > MIN_DIRECT_IRRADIANCE_WM2 is logically inconsistent → ConfigurationError."""
        # Default MIN_DNI_WM2=50; set MIN_DNI_CIRRUS_WM2=60 to exceed it.
        env = {**self._REQUIRED_ENV, "MIN_DNI_CIRRUS_WM2": "60", "MIN_DNI_WM2": "50"}
        with unittest.mock.patch.dict(os.environ, env):
            with self.assertRaises(ConfigurationError) as ctx:
                get_thresholds()
        self.assertIn("MIN_DIRECT_IRRADIANCE_WM2", str(ctx.exception))

    def test_min_dni_cirrus_equal_to_min_dni_is_valid(self):
        """MIN_DNI_CIRRUS_WM2 == MIN_DIRECT_IRRADIANCE_WM2 is at the boundary → no error."""
        # Both set to 50: cirrus guard == Layer 2 threshold, which is valid.
        env = {**self._REQUIRED_ENV, "MIN_DNI_CIRRUS_WM2": "50", "MIN_DNI_WM2": "50"}
        with unittest.mock.patch.dict(os.environ, env):
            result = get_thresholds()
        # min_dni_cirrus is the 9th element in the returned tuple
        self.assertEqual(result[8], 50.0)

    def test_min_dni_cirrus_below_min_dni_is_valid(self):
        """MIN_DNI_CIRRUS_WM2 < MIN_DIRECT_IRRADIANCE_WM2 is the expected configuration → no error."""
        # Default: MIN_DNI_CIRRUS_WM2=30, MIN_DNI_WM2=50 — standard deployment.
        env = {**self._REQUIRED_ENV, "MIN_DNI_CIRRUS_WM2": "30", "MIN_DNI_WM2": "50"}
        with unittest.mock.patch.dict(os.environ, env):
            result = get_thresholds()
        self.assertEqual(result[8], 30.0)


class TestRainGate(unittest.TestCase):
    """
    Tests for evaluate_rain_gate() and its integration into should_open_awning().

    Four cases (R-1 through R-4) cover:
      R-1: precipitation > 0 → gate closes (single-field baseline regression)
      R-2: all signals clear → gate opens (happy-path pin)
      R-3: precipitation=0 but secondary signal fires → gate still closes
      R-4: divergent dry+sunny case (precipitation=0, DNI=0 but GHI/UV pass) → opens
    """

    # ------------------------------------------------------------------
    # R-1 — Rain > 0 closes (single-field baseline regression)
    # Regression for awning_automation.py:945 — previously the ONLY check.
    # Even with evaluate_rain_gate the primary signal must still close.
    # ------------------------------------------------------------------
    def test_R1_precipitation_fires_rain_gate(self):
        """R-1: precipitation=0.5 → evaluate_rain_gate returns False → should_open=False."""
        # Direct unit test of evaluate_rain_gate: precipitation > 0 → close
        w = _weather(
            precipitation=0.5,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,  # "clear sky" WMO code — only precipitation fires
        )
        self.assertFalse(
            evaluate_rain_gate(w, rain_probability_threshold=20),
            "evaluate_rain_gate must return False when precipitation > 0",
        )

        # Integration: should_open_awning must honour the gate
        should_open, reason, conditions = should_open_awning(
            weather=w,
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            conditions["no_rain"],
            f"conditions['no_rain'] must be False when precipitation > 0. reason={reason!r}",
        )
        self.assertFalse(
            should_open,
            f"should_open must be False when raining. reason={reason!r}",
        )
        self.assertIn(
            "Raining",
            reason,
            f"reason string must mention 'Raining'. Got: {reason!r}",
        )

    # ------------------------------------------------------------------
    # R-2 — Zero precipitation opens (no regression on happy path)
    # All rain signals clear. Pins that the refactor does not break the
    # open path when conditions are genuinely fine.
    # ------------------------------------------------------------------
    def test_R2_no_rain_signals_opens(self):
        """R-2: precipitation=0.0, all rain signals clear → evaluate_rain_gate=True → opens."""
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
        )
        self.assertTrue(
            evaluate_rain_gate(w, rain_probability_threshold=20),
            "evaluate_rain_gate must return True when all signals are clear",
        )

        should_open, reason, conditions = should_open_awning(
            weather=w,
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            conditions["no_rain"],
            f"conditions['no_rain'] must be True when no rain signals fire. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"should_open must be True when all conditions favorable. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # R-3 — Secondary signal fires while precipitation=0 → closes (genuine rain scenario)
    # Models the 2026-06-23 incident: precipitation=0 but ensemble probability
    # indicates rain. The gate must close when rain-bearing clouds ARE present
    # (provably-clear forecast veto does NOT engage because clouds are elevated).
    # ------------------------------------------------------------------
    def test_R3_secondary_signal_fires_while_precip_zero(self):
        """R-3: precip=0, prob=30 >= 20, but rain-bearing clouds present → gate closes (forecast veto does not engage)."""
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=30,   # above RAIN_PROBABILITY_THRESHOLD=20
            minutely_15_precip=[],
            weather_code=0,
            # Rain-bearing clouds present → max(cloud_low, cloud_mid) >= 15%
            # → forecast_veto=False → Signal 2 fires normally
            cloud_cover_low=60.0,
            cloud_cover_mid=30.0,
        )
        self.assertFalse(
            evaluate_rain_gate(w, rain_probability_threshold=20),
            "evaluate_rain_gate must return False when hourly_precip_prob >= threshold and rain-bearing clouds present",
        )

        should_open, reason, conditions = should_open_awning(
            weather=w,
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            conditions["no_rain"],
            f"conditions['no_rain'] must be False when secondary signal fires. reason={reason!r}",
        )
        self.assertFalse(
            should_open,
            f"should_open must be False when secondary rain signal fires. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # R-4 — Divergent dry+sunny case: rain=0, DNI=0 but GHI/UV pass
    # Pins that should_open_awning is pure and correct: if rain signals
    # are all clear, the function reports no_rain=True regardless of DNI
    # fluctuations. The flapping behavior (open/close/open across runs) is
    # a main()-layer concern handled by anti-flapping logic (separate card),
    # not a bug in should_open_awning itself.
    # ------------------------------------------------------------------
    def test_R4_dry_sunny_dni_zero_ghi_uv_pass(self):
        """R-4: precipitation=0, DNI=0 but GHI=700 and UV=7 → no_rain=True → opens (Layer 1 passes via GHI+UV)."""
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
            shortwave_radiation=700.0,
            uv_index=7.0,
            dni=0.0,             # DNI=0: Layer 2 DNI arm fails; cloud_cover arm rescues
            cloud_cover=20.0,    # cloud_cover=20% < 80% → Layer 2 passes via cloud arm
        )
        self.assertTrue(
            evaluate_rain_gate(w, rain_probability_threshold=20),
            "evaluate_rain_gate must return True when all rain signals are clear",
        )

        should_open, reason, conditions = should_open_awning(
            weather=w,
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            conditions["no_rain"],
            f"conditions['no_rain'] must be True when no rain signals fire. reason={reason!r}",
        )
        self.assertTrue(
            should_open,
            f"should_open must be True: Layer 1 passes (GHI=700, UV=7), "
            f"Layer 2 passes (cloud=20% < 80%), rain signals clear. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # R-minutely15 — Recent rain in 15-min lookback closes gate (F-1 remedy)
    # Exercises awning_automation.py:1080 — the `any(v > 0 ...)` branch.
    # Precipitation=0 and hourly_precip_prob=0 and weather_code=0 are all
    # clear; only the minutely_15 lookback fires.
    # ------------------------------------------------------------------
    def test_R_minutely15_recent_rain_closes_gate(self):
        """minutely_15_precip=[0.0, 0.3, 0.0] (rain ~30 min ago) → evaluate_rain_gate=False → closes."""
        w = _weather(
            precipitation=0,
            hourly_precip_prob=0,
            minutely_15_precip=[0.0, 0.3, 0.0],  # non-zero value fires Signal 3
            weather_code=0,
        )
        self.assertFalse(
            evaluate_rain_gate(w, rain_probability_threshold=20),
            "evaluate_rain_gate must return False when minutely_15_precip contains a non-zero value",
        )

    # ------------------------------------------------------------------
    # R-weather_code — WMO rain code closes gate (F-2 remedy)
    # WMO code 63 = moderate rain (continuous). Rain-bearing clouds must be
    # present for the forecast signal to fire (otherwise forecast veto suppresses it).
    # ------------------------------------------------------------------
    def test_R_rain_weather_code_closes_gate(self):
        """weather_code=63 (moderate rain WMO) + rain-bearing clouds present → evaluate_rain_gate=False → closes."""
        w = _weather(
            precipitation=0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=63,  # WMO "moderate rain (continuous)" — in _RAIN_WEATHER_CODES
            # Rain-bearing clouds present → forecast_veto=False → Signal 4 fires
            cloud_cover_low=55.0,
            cloud_cover_mid=40.0,
        )
        self.assertFalse(
            evaluate_rain_gate(w, rain_probability_threshold=20),
            "evaluate_rain_gate must return False when weather_code is in _RAIN_WEATHER_CODES and rain-bearing clouds present",
        )

    # ------------------------------------------------------------------
    # R-missing_prob — None hourly_precip_prob treated conservatively as rain (F-3 remedy)
    # Exercises awning_automation.py:1074 — the `hourly_precip_prob is None` branch.
    # All other signals (precipitation, minutely_15, weather_code) are clear;
    # only the missing probability field fires.
    # ------------------------------------------------------------------
    def test_R_missing_hourly_precip_prob_closes_gate(self):
        """hourly_precip_prob=None (missing field) → evaluate_rain_gate=False → closes conservatively."""
        w = _weather(
            precipitation=0,
            hourly_precip_prob=None,  # missing field — conservative path at line 1074
            minutely_15_precip=[],
            weather_code=0,
        )
        self.assertFalse(
            evaluate_rain_gate(w, rain_probability_threshold=20),
            "evaluate_rain_gate must return False when hourly_precip_prob is None (missing data)",
        )

    # ------------------------------------------------------------------
    # R-missing_minutely15 — None minutely_15_precip treated conservatively as rain (F-4 remedy)
    # Exercises awning_automation.py:1080 — the `minutely_15_precip is None` branch.
    # The _weather() helper converts None to [] at construction time, so this test
    # constructs the weather dict directly with 'minutely_15_precip': None.
    # All other signals are clear; only the missing minutely_15 field fires.
    # ------------------------------------------------------------------
    def test_R_missing_minutely15_closes_gate(self):
        """minutely_15_precip=None (missing field) → evaluate_rain_gate=False → closes conservatively."""
        # Construct dict directly — _weather() helper would convert None to [].
        w = _weather(
            precipitation=0,
            hourly_precip_prob=0,
            weather_code=0,
        )
        w["minutely_15_precip"] = None  # Override: force None into the field directly
        self.assertFalse(
            evaluate_rain_gate(w, rain_probability_threshold=20),
            "evaluate_rain_gate must return False when minutely_15_precip is None (missing data)",
        )

    # ------------------------------------------------------------------
    # R-missing_weather_code — None weather_code treated conservatively as rain (F-6 remedy)
    # Exercises the `weather_code is None` branch.
    # The _weather() helper defaults weather_code=0, so None is never exercised via _weather().
    # All other signals are clear; only the missing weather_code field fires.
    # Note: None is missing data (not a forecast value), so the forecast veto does not apply.
    # ------------------------------------------------------------------
    def test_R_missing_weather_code_closes_gate(self):
        """weather_code=None (missing field) → evaluate_rain_gate=False → closes conservatively."""
        # Construct dict directly — _weather() defaults weather_code=0.
        w = _weather(
            precipitation=0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
        )
        w["weather_code"] = None  # Override: force None into the field directly
        self.assertFalse(
            evaluate_rain_gate(w, rain_probability_threshold=20),
            "evaluate_rain_gate must return False when weather_code is None (missing data)",
        )

    # ------------------------------------------------------------------
    # Inverse of regression test: actual observed precip DOES close even with
    # clear-sky conditions (Signal 1 is observed and never suppressed).
    # ------------------------------------------------------------------
    def test_actual_precip_closes_even_with_clear_sky(self):
        """Inverse: actual precip > 0 closes gate even if rain-bearing clouds are low (observed signal, never vetoed)."""
        w = _weather(
            precipitation=0.3,        # observed precipitation — Signal 1 fires
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
            cloud_cover_low=5.0,
            cloud_cover_mid=2.0,      # max = 5% < 15% — would trigger forecast veto
            dni=800.0,                # high DNI
        )
        self.assertFalse(
            evaluate_rain_gate(w, rain_probability_threshold=20),
            "Gate must close when actual precipitation > 0, regardless of cloud/DNI conditions.",
        )

    # ------------------------------------------------------------------
    # Inverse: observed minutely_15 rain closes gate even with clear-sky veto active.
    # Signal 3 is observed (actual measured precipitation), not a forecast.
    # ------------------------------------------------------------------
    def test_minutely15_observed_rain_closes_with_clear_sky(self):
        """Inverse: minutely_15 actual rain (Signal 3) closes gate even when forecast veto would apply."""
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[0.0, 0.8, 0.2],  # actual recent rain
            weather_code=0,
            cloud_cover_low=8.0,
            cloud_cover_mid=3.0,   # max = 8% < 15% — forecast veto active
        )
        self.assertFalse(
            evaluate_rain_gate(w, rain_probability_threshold=20),
            "Gate must close when minutely_15 shows actual recent rain (observed signal, never vetoed).",
        )

    # ------------------------------------------------------------------
    # Safety mirror: genuine thunderstorm with rain-bearing clouds closes.
    # precipitation=0 (gauge dry), prob=35 (above threshold), weather_code=95
    # (WMO thunderstorm), cloud_cover_low=40, cloud_cover_mid=30.
    # max(40, 30)=40 >= 15 → forecast_veto=False → Signals 2 and 4 fire.
    # should_open=False AND conditions['no_rain'] is False.
    # ------------------------------------------------------------------
    def test_genuine_thunderstorm_closes_awning(self):
        """Safety mirror: precip=0, prob=35, weather_code=95 + rain-bearing clouds → no_rain=False → awning closes."""
        w = _weather(
            precipitation=0,
            hourly_precip_prob=35,   # above 20% threshold — Signal 2 fires
            minutely_15_precip=[],
            weather_code=95,         # WMO thunderstorm — Signal 4 fires
            cloud_cover_low=40.0,    # rain-bearing clouds present
            cloud_cover_mid=30.0,    # max(40, 30)=40 >= 15 → forecast_veto=False
        )
        should_open, reason, conditions = should_open_awning(
            weather=w,
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            conditions["no_rain"],
            f"conditions['no_rain'] must be False when thunderstorm signals fire with rain-bearing clouds. reason={reason!r}",
        )
        self.assertFalse(
            should_open,
            f"should_open must be False for genuine thunderstorm. reason={reason!r}",
        )

    # ------------------------------------------------------------------
    # Boundary: forecast veto does NOT engage at exactly max_rain_cloud=15.0%.
    # The condition is strict < (less-than), so max(15.0, 0.0)=15.0 is NOT
    # less than 15.0 → forecast_veto=False → Signal 2 (prob=30 >= 20) fires.
    # ------------------------------------------------------------------
    def test_veto_does_not_engage_at_threshold_cloud(self):
        """Boundary: cloud_cover_low=15.0 → max(15.0, 0.0)=15.0 NOT < 15 → forecast veto off → gate closes."""
        w = _weather(
            precipitation=0,
            hourly_precip_prob=30,   # Signal 2 would fire if veto absent
            minutely_15_precip=[],
            weather_code=0,
            cloud_cover_low=15.0,    # exactly at threshold — veto does NOT engage
            cloud_cover_mid=0.0,
        )
        self.assertFalse(
            evaluate_rain_gate(w, rain_probability_threshold=20, radar_veto_cloud_pct=15.0),
            "evaluate_rain_gate must return False when max(cloud_low=15.0, cloud_mid=0.0)=15.0 is NOT < 15.0 "
            "(strict less-than: veto cannot engage at exactly the threshold)",
        )

    # ------------------------------------------------------------------
    # Missing cloud fields default to 100.0 → veto cannot engage → gate closes.
    # When cloud_cover_low and cloud_cover_mid are absent from the dict,
    # evaluate_rain_gate defaults them to 100.0. max(100, 100)=100 >= 15
    # → forecast_veto=False → Signal 2 (prob=30) fires → returns False.
    # This is the conservative bias-to-close behaviour for incomplete data.
    # ------------------------------------------------------------------
    def test_missing_cloud_fields_veto_does_not_engage(self):
        """Missing cloud_cover_low and cloud_cover_mid → default 100.0 → veto cannot engage → gate closes."""
        # Build dict directly without cloud_cover_low / cloud_cover_mid keys
        w = {
            "wind_speed_10m": 5.0,
            "precipitation": 0,
            "weather_code": 0,
            "temperature": 65.0,
            "shortwave_radiation": 500.0,
            "uv_index": 6.0,
            "dni": 450.0,
            "cloud_cover": 20.0,
            # cloud_cover_low and cloud_cover_mid intentionally absent
            "cloud_cover_high": 10.0,
            "sunrise": "2026-04-17T06:00:00",
            "sunset": "2026-04-17T20:00:00",
            "hourly_precip_prob": 30,   # above threshold — fires if veto absent
            "minutely_15_precip": [],
        }
        self.assertFalse(
            evaluate_rain_gate(w, rain_probability_threshold=20, radar_veto_cloud_pct=15.0),
            "evaluate_rain_gate must return False when cloud fields are absent (default 100.0 → max >= 15 → veto off → Signal 2 fires)",
        )

    # ------------------------------------------------------------------
    # Log attribution: evaluate_rain_gate emits 'Rain signals:' on every call,
    # and 'Forecast veto engaged' when the veto fires.
    # ------------------------------------------------------------------
    def test_rain_signals_log_attribution(self):
        """evaluate_rain_gate must log 'Rain signals:' on every call and 'Forecast veto engaged' when veto fires."""
        import logging

        # Scenario that triggers the forecast veto:
        # precip=0, prob=30 >= 20, weather_code=0, max(cloud_low=10, cloud_mid=5)=10 < 15
        # → forecast_veto=True → Signals 2 and 4 suppressed → gate returns True
        w = _weather(
            precipitation=0,
            hourly_precip_prob=30,
            minutely_15_precip=[],
            weather_code=0,
            cloud_cover_low=10.0,
            cloud_cover_mid=5.0,
        )

        with self.assertLogs("awning_automation", level="INFO") as log_ctx:
            result = evaluate_rain_gate(w, rain_probability_threshold=20, radar_veto_cloud_pct=15.0)

        # Gate should return True — forecast signals suppressed by veto
        self.assertTrue(result, "Forecast veto scenario should leave gate open (True)")

        all_messages = "\n".join(log_ctx.output)
        self.assertIn(
            "Rain signals:",
            all_messages,
            "evaluate_rain_gate must emit a 'Rain signals:' log line on every call",
        )
        self.assertIn(
            "Forecast veto engaged",
            all_messages,
            "evaluate_rain_gate must emit 'Forecast veto engaged' when the veto fires",
        )


class TestRadarGate(unittest.TestCase):
    """
    Tests for is_raining_on_radar() and its integration into evaluate_rain_gate().

    All HTTP calls and PNG loading are mocked — no real network requests are made.
    Three cases:
      RV-1: precip pixel (alpha > 0) → is_raining_on_radar True → gate closes
      RV-2: transparent pixel (alpha = 0) → is_raining_on_radar False → gate stays open
      RV-3: fetch exception → is_raining_on_radar False (fail-open)
    """

    # Minimal RainViewer weather-maps.json metadata response
    _META_JSON = {
        "version": "2.0",
        "generated": 1719164400,
        "host": "https://tilecache.rainviewer.com",
        "radar": {
            "past": [
                {"time": 1719163800, "path": "/v2/radar/1719163800"},
                {"time": 1719164400, "path": "/v2/radar/1719164400"},
            ],
            "nowcast": [],
        },
    }

    @staticmethod
    def _make_png_bytes(r: int, g: int, b: int, a: int) -> bytes:
        """Create a 1×1 PNG (scaled to 256×256 via resize) for testing pixel checks.

        We actually create a 256×256 solid-color PNG so getpixel works correctly
        regardless of what pixel coordinates the tile math produces.
        """
        from PIL import Image as PILImage
        import io as _io
        img = PILImage.new("RGBA", (256, 256), (r, g, b, a))
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _mock_requests(self, meta_json: dict, tile_png_bytes: bytes):
        """Return a mock for requests.get that serves meta JSON then tile PNG."""
        import json
        import unittest.mock as mock

        meta_resp = mock.MagicMock()
        meta_resp.raise_for_status.return_value = None
        meta_resp.json.return_value = meta_json

        tile_resp = mock.MagicMock()
        tile_resp.raise_for_status.return_value = None
        tile_resp.content = tile_png_bytes

        # First call → metadata, second call → tile
        mock_get = mock.MagicMock(side_effect=[meta_resp, tile_resp])
        return mock_get

    # ------------------------------------------------------------------
    # RV-1 — Precip pixel (alpha > 0) → is_raining_on_radar True → gate closes
    # A solid non-transparent pixel anywhere in the tile means radar return.
    # The rain gate must return False (close the awning).
    # ------------------------------------------------------------------
    def test_RV1_precip_pixel_closes_gate(self):
        """RV-1: non-transparent pixel (alpha=200) → is_raining_on_radar=True → gate closes."""
        from unittest.mock import patch
        from awning_automation import is_raining_on_radar, evaluate_rain_gate

        png_bytes = self._make_png_bytes(r=0, g=100, b=200, a=200)  # alpha > 0 = rain

        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            result = is_raining_on_radar(35.778, -78.838)

        self.assertTrue(result, "Expected is_raining_on_radar=True for non-transparent pixel")

        # Integration: evaluate_rain_gate must close when radar fires
        # All Open-Meteo signals are clear; only radar should trigger close
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
        )
        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            gate_result = evaluate_rain_gate(w, rain_probability_threshold=20, lat=35.778, lon=-78.838)

        self.assertFalse(gate_result, "evaluate_rain_gate must return False when radar detects rain")

    # ------------------------------------------------------------------
    # RV-2 — Transparent pixel (alpha = 0) → is_raining_on_radar False
    # A fully transparent pixel means no radar return at that location.
    # The gate should remain open (True = no rain).
    # ------------------------------------------------------------------
    def test_RV2_transparent_pixel_gate_stays_open(self):
        """RV-2: transparent pixel (alpha=0) → is_raining_on_radar=False → gate stays open."""
        from unittest.mock import patch
        from awning_automation import is_raining_on_radar, evaluate_rain_gate

        png_bytes = self._make_png_bytes(r=0, g=0, b=0, a=0)  # fully transparent = no rain

        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            result = is_raining_on_radar(35.778, -78.838)

        self.assertFalse(result, "Expected is_raining_on_radar=False for transparent pixel")

        # Integration: gate should stay open when all signals are clear
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
        )
        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            gate_result = evaluate_rain_gate(w, rain_probability_threshold=20, lat=35.778, lon=-78.838)

        self.assertTrue(gate_result, "evaluate_rain_gate must return True when no rain signals fire")

    # ------------------------------------------------------------------
    # RV-3 — Fetch exception → is_raining_on_radar False (fail-open)
    # A RainViewer outage must not keep the awning permanently closed.
    # Any exception during fetch returns False so Open-Meteo signals still guard.
    # ------------------------------------------------------------------
    def test_RV3_fetch_exception_fail_open(self):
        """RV-3: requests.get raises exception → is_raining_on_radar=False (fail-open)."""
        from unittest.mock import patch
        from awning_automation import is_raining_on_radar, evaluate_rain_gate

        with patch("awning_automation.requests.get", side_effect=Exception("simulated network error")):
            result = is_raining_on_radar(35.778, -78.838)

        self.assertFalse(result, "Expected is_raining_on_radar=False on fetch exception (fail-open)")

        # Integration: gate stays open on radar exception when Open-Meteo signals are clear
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
        )
        with patch("awning_automation.requests.get", side_effect=Exception("simulated network error")):
            gate_result = evaluate_rain_gate(w, rain_probability_threshold=20, lat=35.778, lon=-78.838)

        self.assertTrue(
            gate_result,
            "evaluate_rain_gate must return True (fail-open) when only radar errors and Open-Meteo is clear",
        )

    # ------------------------------------------------------------------
    # RV-missing-pillow — PIL unavailable → is_raining_on_radar False (fail-open)
    # Pillow is an optional dependency. When it is missing (e.g. the Pi venv
    # does not have it installed), the radar check must be silently skipped
    # and return False — the automation must continue on Open-Meteo signals.
    # A missing optional dependency must NEVER crash the module or awning controller.
    # ------------------------------------------------------------------
    def test_radar_missing_pillow_fails_open(self):
        """Pillow absent (PIL raises ImportError) → is_raining_on_radar=False without raising."""
        import sys
        from unittest.mock import patch, MagicMock
        from awning_automation import is_raining_on_radar

        # Serve valid metadata and a tile response so the function reaches the
        # PIL import. The tile content doesn't matter — PIL never gets to open it.
        import json as _json
        meta_resp = MagicMock()
        meta_resp.raise_for_status.return_value = None
        meta_resp.json.return_value = self._META_JSON

        tile_resp = MagicMock()
        tile_resp.raise_for_status.return_value = None
        tile_resp.content = b"fake-png-bytes"

        mock_get = MagicMock(side_effect=[meta_resp, tile_resp])

        # Simulate Pillow being uninstalled by making `import PIL` raise ImportError.
        # We patch sys.modules so that `from PIL import Image` inside the function
        # raises ImportError, exactly as it would on a Pi without Pillow.
        with patch("awning_automation.requests.get", mock_get):
            with patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
                result = is_raining_on_radar(35.778, -78.838)

        self.assertFalse(
            result,
            "is_raining_on_radar must return False (fail-open) when Pillow is not installed",
        )

    # ------------------------------------------------------------------
    # RV-veto-1 — Reproduce 2026-06-24: radar fires + high DNI + low cloud → gate stays open
    # Incident: DNI=784 W/m², cloud_cover=3%, radar pixel A=110 (biological clutter).
    # With the clear-sky veto (radar_veto_dni=400, radar_veto_cloud_pct=15), the radar
    # hit must be suppressed and the gate must return True (no rain → awning may open).
    # ------------------------------------------------------------------
    def test_RV_veto1_clear_sky_suppresses_radar_hit(self):
        """RV-veto-1: radar fires + DNI high + cloud low → veto suppresses → gate stays open."""
        from unittest.mock import patch
        from awning_automation import evaluate_rain_gate

        # Full-opacity precipitation pixel. The 2026-06-24 incident pixel was
        # RGBA (158,147,117,110), but alpha 110 now decodes as clutter and never
        # reaches the veto — this test must keep exercising the veto, so it uses a
        # pixel that genuinely clears the alpha floor.
        png_bytes = self._make_png_bytes(r=0, g=100, b=200, a=255)

        # All Open-Meteo signals are clear; radar fires but DNI/cloud veto applies
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
            dni=784.0,         # high direct-sun irradiance — provably clear sky
            cloud_cover=3.0,   # very low cloud cover — provably clear sky
        )

        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            gate_result = evaluate_rain_gate(
                w,
                rain_probability_threshold=20,
                lat=35.778,
                lon=-78.838,
                radar_veto_dni=400.0,
                radar_veto_cloud_pct=15.0,
            )

        self.assertTrue(
            gate_result,
            "evaluate_rain_gate must return True (no rain) when radar fires but clear-sky veto applies "
            "(DNI=784 >= 400 AND cloud_cover=3% < 15%)",
        )

    # ------------------------------------------------------------------
    # RV-veto-2 — Over-suppression guard: radar fires + DNI LOW → veto does NOT fire
    # Even if cloud_cover is low, if DNI is below the veto threshold (e.g. 50 W/m²),
    # the veto must NOT suppress the radar signal — gate closes.
    # ------------------------------------------------------------------
    def test_RV_veto2_low_dni_veto_does_not_fire(self):
        """RV-veto-2: radar fires + DNI low (50 W/m²) → veto does not suppress → gate closes."""
        from unittest.mock import patch
        from awning_automation import evaluate_rain_gate

        png_bytes = self._make_png_bytes(r=0, g=100, b=200, a=200)  # precipitation pixel

        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
            dni=50.0,          # low DNI — overcast/light rain territory
            cloud_cover=10.0,  # cloud_cover below 15%, but DNI doesn't qualify
        )

        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            gate_result = evaluate_rain_gate(
                w,
                rain_probability_threshold=20,
                lat=35.778,
                lon=-78.838,
                radar_veto_dni=400.0,
                radar_veto_cloud_pct=15.0,
            )

        self.assertFalse(
            gate_result,
            "evaluate_rain_gate must return False (rain) when radar fires and DNI is too low for veto",
        )

    # ------------------------------------------------------------------
    # RV-veto-3 — Over-suppression guard: radar fires + rain-bearing cloud HIGH → veto does NOT fire
    # Even if DNI is high, if max(cloud_cover_low, cloud_cover_mid) is at or above the
    # veto threshold (e.g. 20%), the veto must NOT suppress the radar signal — gate closes.
    # Note: total cloud_cover is no longer the check — only rain-bearing (low/mid) layers matter.
    # ------------------------------------------------------------------
    def test_RV_veto3_high_cloud_veto_does_not_fire(self):
        """RV-veto-3: radar fires + rain-bearing cloud_cover_low=20% → veto does not suppress → gate closes."""
        from unittest.mock import patch
        from awning_automation import evaluate_rain_gate

        png_bytes = self._make_png_bytes(r=0, g=100, b=200, a=200)  # precipitation pixel

        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
            dni=500.0,           # high DNI — qualifies for veto DNI arm
            cloud_cover=20.0,
            cloud_cover_low=20.0,  # rain-bearing low cloud >= 15% — veto does not apply
            cloud_cover_mid=0.0,
        )

        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            gate_result = evaluate_rain_gate(
                w,
                rain_probability_threshold=20,
                lat=35.778,
                lon=-78.838,
                radar_veto_dni=400.0,
                radar_veto_cloud_pct=15.0,
            )

        self.assertFalse(
            gate_result,
            "evaluate_rain_gate must return False (rain) when radar fires and max(cloud_low=20%, cloud_mid=0%)=20% >= veto threshold",
        )

    # ------------------------------------------------------------------
    # RV-veto-4 — Other rain signals unaffected: precipitation > 0 closes gate regardless of DNI
    # The clear-sky veto must ONLY affect Signal 5 (radar). If Signal 1 (precipitation > 0)
    # fires, the gate must still close even when DNI and cloud conditions would qualify for veto.
    # ------------------------------------------------------------------
    def test_RV_veto4_precipitation_signal_unaffected_by_veto(self):
        """RV-veto-4: precipitation=0.5 closes gate even when DNI+cloud qualify for veto."""
        from awning_automation import evaluate_rain_gate

        # No radar mock needed — precipitation fires first (Signal 1)
        w = _weather(
            precipitation=0.5,   # active rain signal
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
            dni=784.0,         # high DNI — would qualify for veto
            cloud_cover=3.0,   # low cloud — would qualify for veto
        )

        gate_result = evaluate_rain_gate(
            w,
            rain_probability_threshold=20,
            lat=35.778,
            lon=-78.838,
            radar_veto_dni=400.0,
            radar_veto_cloud_pct=15.0,
        )

        self.assertFalse(
            gate_result,
            "evaluate_rain_gate must return False when precipitation > 0, even if clear-sky veto conditions hold",
        )

    # ------------------------------------------------------------------
    # RV-veto-5 — Rain probability closes gate when rain-bearing clouds ARE present
    # Signal 2 (hourly_precip_prob >= threshold) closes the gate when rain-bearing
    # low/mid clouds are elevated (forecast veto does NOT engage). This confirms that
    # the forecast veto cannot suppress a signal when actual rain conditions exist.
    # ------------------------------------------------------------------
    def test_RV_veto5_rain_probability_closes_when_rain_bearing_clouds_present(self):
        """RV-veto-5: high rain probability + rain-bearing clouds present → forecast veto cannot engage → gate closes."""
        from awning_automation import evaluate_rain_gate

        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=30,  # >= 20% threshold — fires Signal 2
            minutely_15_precip=[],
            weather_code=0,
            dni=200.0,
            cloud_cover=70.0,
            cloud_cover_low=50.0,   # rain-bearing low cloud elevated
            cloud_cover_mid=30.0,   # rain-bearing mid cloud elevated
        )

        gate_result = evaluate_rain_gate(
            w,
            rain_probability_threshold=20,
            lat=35.778,
            lon=-78.838,
            radar_veto_dni=400.0,
            radar_veto_cloud_pct=15.0,
        )

        self.assertFalse(
            gate_result,
            "evaluate_rain_gate must return False when rain probability >= threshold and "
            "rain-bearing clouds present (max(cloud_low=50%, cloud_mid=30%)=50% >= 15% → forecast veto does not engage)",
        )

    # ------------------------------------------------------------------
    # RV-veto-5b — Forecast veto suppresses Signal 2 under clear-sky conditions.
    # Restores the forecast-veto-vs-Signal-2 coverage dimension: precipitation=0,
    # prob=30 >= 20, weather_code=0, cloud_cover_low=10, cloud_cover_mid=5, dni=784.
    # max(10, 5)=10 < 15 AND precip=0 → forecast_veto=True → Signal 2 suppressed.
    # All other signals clear → gate returns True (no rain).
    # ------------------------------------------------------------------
    def test_RV_veto5_forecast_veto_suppresses_signal2_with_clear_sky(self):
        """RV-veto-5b: prob=30 + clear sky (cloud_low=10, cloud_mid=5, precip=0) → forecast veto suppresses Signal 2 → gate open."""
        from awning_automation import evaluate_rain_gate

        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=30,  # above threshold — would fire Signal 2 without veto
            minutely_15_precip=[],
            weather_code=0,
            cloud_cover_low=10.0,   # rain-bearing: low
            cloud_cover_mid=5.0,    # rain-bearing: low; max(10, 5)=10 < 15 → forecast_veto=True
            dni=784.0,              # high DNI — provably clear sky
        )

        gate_result = evaluate_rain_gate(
            w,
            rain_probability_threshold=20,
            radar_veto_cloud_pct=15.0,
            # No lat/lon: radar check skipped; only forecast signals are in play
        )

        self.assertTrue(
            gate_result,
            "evaluate_rain_gate must return True when prob=30 fires Signal 2 but "
            "forecast veto engages (precip=0 AND max(cloud_low=10%, cloud_mid=5%)=10% < 15%)",
        )

    # ------------------------------------------------------------------
    # RV-cirrus-1 — Regression for 2026-06-24 CIRRUS incident:
    # Today's failure: DNI=888, cloud_cover_low=2, cloud_cover_mid=0, cloud_cover_high=51.
    # Total cloud cover was 22-52% (cirrus pushed it up), which defeated the OLD veto
    # (which checked TOTAL cloud_cover < 15%). The awning was incorrectly closed.
    #
    # NEW veto checks max(cloud_cover_low, cloud_cover_mid) < 15% (rain-bearing layers only):
    # max(2, 0) = 2% < 15% AND DNI=888 >= 650 → veto ENGAGES → radar hit suppressed → gate open.
    # ------------------------------------------------------------------
    def test_RV_cirrus1_cirrus_high_cloud_veto_engages_opens_awning(self):
        """RV-cirrus-1 CIRRUS regression: DNI=888, low=2%, mid=0%, high=51% → rain-bearing veto engages → gate open."""
        from unittest.mock import patch
        from awning_automation import evaluate_rain_gate

        # Full-opacity precipitation pixel, so the radar arm actually fires and the
        # rain-bearing-layer veto is the thing under test. (The original 2026-06-24
        # echo, RGBA 158,147,117,110, is now rejected by the alpha floor before the
        # veto is consulted, which would make this test vacuous.)
        png_bytes = self._make_png_bytes(r=0, g=100, b=200, a=255)

        # Reproduce today's failure exactly: DNI=888, thin cirrus (high=51%) but
        # rain-bearing layers near-zero (low=2%, mid=0%)
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
            dni=888.0,
            cloud_cover=22.0,       # total cloud cover — 22% (cirrus inflates this)
            cloud_cover_low=2.0,    # rain-bearing: near-zero
            cloud_cover_mid=0.0,    # rain-bearing: zero
            cloud_cover_high=51.0,  # thin cirrus — NOT rain-bearing
        )

        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            gate_result = evaluate_rain_gate(
                w,
                rain_probability_threshold=20,
                lat=35.778,
                lon=-78.838,
                radar_veto_dni=650.0,
                radar_veto_cloud_pct=15.0,
            )

        self.assertTrue(
            gate_result,
            "evaluate_rain_gate must return True (no rain) when radar fires but rain-bearing "
            "cirrus veto applies: DNI=888 >= 650 AND max(cloud_low=2%, cloud_mid=0%)=2% < 15%. "
            "High cirrus (51%) must NOT block the veto.",
        )

    # ------------------------------------------------------------------
    # RV-cirrus-2 — Safety guard: rain-bearing low cloud defeats the veto
    # Even with very high DNI, if cloud_cover_low or cloud_cover_mid is elevated
    # (indicating actual rain-producing clouds), the veto must NOT engage and
    # the radar rain signal must close the gate.
    # ------------------------------------------------------------------
    def test_RV_cirrus2_rain_bearing_low_cloud_defeats_veto(self):
        """RV-cirrus-2 CIRRUS safety guard: cloud_cover_low=40% → max(low,mid)=40% >= 15% → veto does NOT engage → gate closes."""
        from unittest.mock import patch
        from awning_automation import evaluate_rain_gate

        png_bytes = self._make_png_bytes(r=0, g=100, b=200, a=200)  # precipitation pixel

        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
            dni=888.0,           # DNI is high (would qualify for DNI arm of veto)
            cloud_cover=50.0,
            cloud_cover_low=40.0,   # rain-bearing layer elevated — real clouds present
            cloud_cover_mid=0.0,
            cloud_cover_high=10.0,
        )

        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            gate_result = evaluate_rain_gate(
                w,
                rain_probability_threshold=20,
                lat=35.778,
                lon=-78.838,
                radar_veto_dni=650.0,
                radar_veto_cloud_pct=15.0,
            )

        self.assertFalse(
            gate_result,
            "evaluate_rain_gate must return False (rain) when cloud_cover_low=40% means "
            "max(cloud_low=40%, cloud_mid=0%)=40% >= 15% — rain-bearing clouds present, veto must NOT fire.",
        )

    # ------------------------------------------------------------------
    # RV-cirrus-3 — Safety guard: rain-bearing mid cloud defeats the veto
    # Same as RV-cirrus-2 but mid layer is elevated instead of low.
    # ------------------------------------------------------------------
    def test_RV_cirrus3_rain_bearing_mid_cloud_defeats_veto(self):
        """RV-cirrus-3 CIRRUS safety guard: cloud_cover_mid=40% → max(low,mid)=40% >= 15% → veto does NOT engage → gate closes."""
        from unittest.mock import patch
        from awning_automation import evaluate_rain_gate

        png_bytes = self._make_png_bytes(r=0, g=100, b=200, a=200)  # precipitation pixel

        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
            dni=888.0,
            cloud_cover=50.0,
            cloud_cover_low=0.0,
            cloud_cover_mid=40.0,   # rain-bearing layer elevated — altostratus/altocumulus
            cloud_cover_high=10.0,
        )

        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            gate_result = evaluate_rain_gate(
                w,
                rain_probability_threshold=20,
                lat=35.778,
                lon=-78.838,
                radar_veto_dni=650.0,
                radar_veto_cloud_pct=15.0,
            )

        self.assertFalse(
            gate_result,
            "evaluate_rain_gate must return False (rain) when cloud_cover_mid=40% means "
            "max(cloud_low=0%, cloud_mid=40%)=40% >= 15% — rain-bearing clouds present, veto must NOT fire.",
        )


    # ------------------------------------------------------------------
    # test_radar_single_clutter_pixel_does_not_close
    # A lone non-zero-alpha pixel at the target location should NOT close the
    # awning. NEXRAD clear-air-mode produces single-pixel clutter echoes
    # (biological scatter, ground clutter, anomalous propagation); requiring
    # at least _RADAR_MIN_WET_PIXELS (=2) in the 3×3 neighborhood suppresses
    # these lone false positives.
    # ------------------------------------------------------------------
    def test_radar_single_clutter_pixel_does_not_close(self):
        """Single lone clutter pixel at target location → wet_count=1 < 2 → is_raining=False."""
        import math
        from unittest.mock import patch
        from awning_automation import is_raining_on_radar, evaluate_rain_gate, _RAINVIEWER_TILE_ZOOM
        from PIL import Image as PILImage
        import io as _io

        lat, lon = 35.778, -78.838
        z = _RAINVIEWER_TILE_ZOOM
        lat_rad = math.radians(lat)
        n = 2 ** z
        x_float = (lon + 180.0) / 360.0 * n
        y_float = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
        tile_x = int(x_float)
        tile_y = int(y_float)
        px = int((x_float - tile_x) * 256)
        py = int((y_float - tile_y) * 256)

        # Build a 256×256 PNG that is fully transparent EXCEPT for exactly one
        # pixel at the target location — a lone clutter echo.
        # A single FULL-OPACITY pixel, so the alpha floor cannot be what rejects it —
        # this test isolates the neighborhood-count guard on its own.
        img = PILImage.new("RGBA", (256, 256), (0, 0, 0, 0))
        img.putpixel((px, py), (0, 100, 200, 255))  # lone precip-colored pixel
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            result = is_raining_on_radar(lat, lon)

        self.assertFalse(
            result,
            "is_raining_on_radar must return False for a single lone clutter pixel "
            "(wet_count=1 < _RADAR_MIN_WET_PIXELS=2).",
        )

        # Integration: evaluate_rain_gate must stay open when only a lone clutter pixel fires
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
        )
        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            gate_result = evaluate_rain_gate(
                w,
                rain_probability_threshold=20,
                lat=lat,
                lon=lon,
            )

        self.assertTrue(
            gate_result,
            "evaluate_rain_gate must return True (no rain) when radar reports only "
            "a single lone clutter pixel in the 3×3 neighborhood.",
        )

    # ------------------------------------------------------------------
    # test_radar_neighborhood_precipitation_closes
    # A cluster of >=2 non-transparent pixels in the 3×3 neighborhood
    # represents a real precipitation cell and must close the awning.
    # Real precipitation cells light up many adjacent radar pixels, so
    # the >=2 threshold is trivially met.
    # ------------------------------------------------------------------
    def test_radar_neighborhood_precipitation_closes(self):
        """Cluster of >=2 wet pixels in 3×3 neighborhood → is_raining=True → gate closes."""
        import math
        from unittest.mock import patch
        from awning_automation import is_raining_on_radar, evaluate_rain_gate, _RAINVIEWER_TILE_ZOOM
        from PIL import Image as PILImage
        import io as _io

        lat, lon = 35.778, -78.838
        z = _RAINVIEWER_TILE_ZOOM
        lat_rad = math.radians(lat)
        n = 2 ** z
        x_float = (lon + 180.0) / 360.0 * n
        y_float = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
        tile_x = int(x_float)
        tile_y = int(y_float)
        px = int((x_float - tile_x) * 256)
        py = int((y_float - tile_y) * 256)

        # Build a 256×256 PNG where the TARGET pixel (px, py) is fully transparent
        # and exactly 2 NEIGHBOR pixels within the 3×3 window are wet.
        # This makes the test genuinely differentiating:
        #   - OLD single-pixel decode: reads alpha at (px, py) = 0 → returns False (WRONG).
        #   - NEW neighborhood decode: counts 2 wet neighbors → returns True (CORRECT).
        # For lat=35.778, lon=-78.838 at zoom 6, px≈252 and py≈92, both well
        # within the tile interior. Edge guards handle the px==0 / px==255 extremes.
        if 0 < px < 255:
            n1_x, n1_y = px - 1, py
            n2_x, n2_y = px + 1, py
        elif px == 0:
            n1_x, n1_y = px + 1, py
            n2_x, n2_y = px + 1, py + 1 if py < 255 else py - 1
        else:  # px == 255
            n1_x, n1_y = px - 1, py
            n2_x, n2_y = px - 1, py + 1 if py < 255 else py - 1

        img = PILImage.new("RGBA", (256, 256), (0, 0, 0, 0))
        img.putpixel((px, py), (0, 0, 0, 0))              # target pixel intentionally transparent
        img.putpixel((n1_x, n1_y), (0, 100, 200, 200))   # first wet neighbor — rain return
        img.putpixel((n2_x, n2_y), (0, 100, 200, 200))   # second wet neighbor — rain return
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            result = is_raining_on_radar(lat, lon)

        self.assertTrue(
            result,
            "is_raining_on_radar must return True when 2 adjacent radar pixels in "
            "the 3×3 neighborhood are non-transparent (wet_count=2 >= _RADAR_MIN_WET_PIXELS=2).",
        )

        # Integration: evaluate_rain_gate must close when radar neighborhood detects rain
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
        )
        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            gate_result = evaluate_rain_gate(
                w,
                rain_probability_threshold=20,
                lat=lat,
                lon=lon,
            )

        self.assertFalse(
            gate_result,
            "evaluate_rain_gate must return False (rain) when radar neighborhood "
            "shows >=2 wet pixels (precipitation cell).",
        )

    # ------------------------------------------------------------------
    # test_radar_faded_clutter_ramp_does_not_close
    # 2026-08-13 13:15 incident regression.
    #
    # The awning closed on a cloudless 92°F afternoon (DNI 789 W/m², cloud_low 0%,
    # precipitation 0.00 mm, precipitation_probability 0%) because the 3×3 radar
    # neighborhood held three pixels at alpha 20/25/20, RGB ~(99,97,89). Those sit
    # on RainViewer's faded sub-threshold ramp (a fixed 25-entry palette running
    # alpha 20..190 in desaturated grey-khaki), NOT on its precipitation palette
    # (alpha exactly 255, saturated blue→yellow→red). The old `alpha > 0` decode
    # could not tell them apart, so three ghosts outvoted _RADAR_MIN_WET_PIXELS.
    #
    # The clear-sky radar veto did not save it: DNI 789 cleared the 650 floor, but
    # cloud_mid was 20%, over the 15% rain-bearing ceiling — so the veto abstained.
    # The alpha floor must reject this before the veto is ever needed.
    # ------------------------------------------------------------------
    def test_radar_faded_clutter_ramp_does_not_close(self):
        """2026-08-13: three faded-ramp pixels (alpha 20/25/20) → not rain → gate stays open."""
        import math
        from unittest.mock import patch
        from awning_automation import is_raining_on_radar, evaluate_rain_gate, _RAINVIEWER_TILE_ZOOM
        from PIL import Image as PILImage
        import io as _io

        lat, lon = 35.778, -78.838
        z = _RAINVIEWER_TILE_ZOOM
        lat_rad = math.radians(lat)
        n = 2 ** z
        x_float = (lon + 180.0) / 360.0 * n
        y_float = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
        tile_x = int(x_float)
        tile_y = int(y_float)
        px = int((x_float - tile_x) * 256)
        py = int((y_float - tile_y) * 256)

        # Exactly the observed neighborhood: three horizontally adjacent faded pixels.
        img = PILImage.new("RGBA", (256, 256), (0, 0, 0, 0))
        img.putpixel((px, py), (102, 99, 90, 25))
        if px > 0:
            img.putpixel((px - 1, py), (99, 97, 89, 20))
        if px < 255:
            img.putpixel((px + 1, py), (99, 97, 89, 20))
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            result = is_raining_on_radar(lat, lon)

        self.assertFalse(
            result,
            "is_raining_on_radar must return False for three faded-ramp pixels "
            "(alpha 20/25/20 < _RADAR_MIN_ALPHA=200) — RainViewer renders real "
            "precipitation at alpha 255, so these are clear-air clutter.",
        )

        # Integration: reproduce the incident's exact sky and confirm the gate stays
        # open. cloud_mid=20% keeps the clear-sky radar veto disengaged, so the alpha
        # floor is the only thing standing between the clutter and a false close.
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=1,
            dni=789.0,
            cloud_cover=24.0,
            cloud_cover_low=0.0,
            cloud_cover_mid=20.0,
            cloud_cover_high=18.0,
        )
        with patch("awning_automation.requests.get", self._mock_requests(self._META_JSON, png_bytes)):
            gate_result = evaluate_rain_gate(
                w,
                rain_probability_threshold=20,
                lat=lat,
                lon=lon,
                radar_veto_dni=650.0,
                radar_veto_cloud_pct=15.0,
            )

        self.assertTrue(
            gate_result,
            "evaluate_rain_gate must return True (no rain) for the 2026-08-13 13:15 "
            "conditions — the awning should have stayed open.",
        )


class TestImmediateOpenClose(unittest.TestCase):
    """
    Tests confirming that the automation acts immediately on current conditions.

    No vote debounce, no state file. When all conditions are met, the awning
    opens on a single run. When any condition fails, the awning closes on that run.
    """

    # ------------------------------------------------------------------
    # I-1 — All conditions met: should_open_awning returns True immediately
    # No threshold, no vote counting. A single run with all conditions met
    # must produce should_open=True so main() opens the awning right away.
    # ------------------------------------------------------------------
    def test_I1_all_conditions_met_opens_immediately(self):
        """I-1: All 7 conditions met on a single run → should_open=True immediately."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=700.0,
                uv_index=7.0,
                dni=450.0,
                cloud_cover=20.0,
                wind_speed=5.0,
                precipitation=0.0,
                temperature=65.0,
                weather_code=0,
                hourly_precip_prob=0,
                minutely_15_precip=[],
            ),
            sun_position=_sun(azimuth=150.0, altitude=35.0),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            should_open,
            f"Expected should_open=True (immediate open) on first run when all conditions met. "
            f"reason={reason!r}",
        )
        self.assertTrue(all(conditions.values()), f"All conditions must be True. Got: {conditions!r}")

    # ------------------------------------------------------------------
    # I-2 — Close condition: should_open_awning returns False immediately
    # When any condition fails (here: rain), the function returns False so
    # main() closes the awning on that single run.
    # ------------------------------------------------------------------
    def test_I2_close_condition_closes_immediately(self):
        """I-2: Rain detected on a single run → should_open=False immediately."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                shortwave_radiation=700.0,
                uv_index=7.0,
                dni=450.0,
                cloud_cover=20.0,
                wind_speed=5.0,
                precipitation=1.0,  # rain → no_rain=False
                temperature=65.0,
            ),
            sun_position=_sun(azimuth=150.0, altitude=35.0),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            should_open,
            f"Expected should_open=False immediately when it is raining. reason={reason!r}",
        )
        self.assertFalse(conditions["no_rain"], "Expected no_rain=False when precipitation > 0")


class TestForecastVetoRegression(unittest.TestCase):
    """
    Regression tests for the 2026-06-27 provably-clear forecast veto.

    At 11:00 the sky was nearly clear but a forecast signal closed the gate.
    The forecast veto must suppress all NWP-only signals when actual
    precipitation == 0 AND max(cloud_cover_low, cloud_cover_mid) < 15%.
    """

    def test_forecast_rain_with_clear_sky_does_not_close(self):
        """
        Regression 2026-06-27 11:00 false close.

        At 11:00 the sky was nearly clear (cloud_low=10%, cloud_mid=2%, DNI=263,
        precip=0.0 mm/h) but the rain gate flipped to 'Rain' and stayed stuck.
        The culprit was a forecast signal (precipitation_probability or weather_code)
        that ticked up at the hour boundary while there were no rain-bearing clouds.

        The provably-clear forecast veto must suppress all forecast-only signals when
        actual precipitation == 0 AND max(cloud_cover_low, cloud_cover_mid) < 15%.
        Rain cannot fall from air with no low/mid clouds regardless of model forecast.
        """
        w = _weather(
            precipitation=0.0,
            hourly_precip_prob=35,    # well above 20% threshold — forecast says rain
            minutely_15_precip=[],
            weather_code=95,          # WMO thunderstorm code (in _RAIN_WEATHER_CODES)
            cloud_cover_low=10.0,     # 11:00 scenario — no rain-bearing clouds
            cloud_cover_mid=2.0,      # max(10%, 2%) = 10% < 15% → forecast_veto=True
            dni=263.0,                # moderate DNI (11:00 AM reading)
        )

        # Direct gate check: forecast veto must suppress signals 2 and 4
        result = evaluate_rain_gate(w, rain_probability_threshold=20)
        assert result, (
            "evaluate_rain_gate must return True (no rain) when actual precip=0, "
            "sky is provably clear (max(cloud_low=10%, cloud_mid=2%)=10% < 15%), "
            "and only forecast signals fire — provably-clear forecast veto must suppress them."
        )

        # Integration: should_open_awning must not close for rain in this scenario
        should_open, reason, conditions = should_open_awning(
            weather=w,
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        assert conditions["no_rain"], (
            f"no_rain must be True: forecast signals suppressed by provably-clear veto. reason={reason!r}"
        )
        # The awning may close for other reasons (DNI=263 low, cloud checks) but NOT for rain.
        assert "Raining" not in reason, (
            f"Reason must not cite rain as a close reason. reason={reason!r}"
        )


class TestDefaultConstantsSingleSource(unittest.TestCase):
    """
    Card #97: MIN_DNI_DIRECT_WM2, SUN_AZIMUTH_MIN_DEG, and SUN_AZIMUTH_MAX_DEG
    each have exactly one authoritative default value (a module-level
    DEFAULT_ constant), referenced everywhere the default is needed rather
    than restated as a bare literal at each site.

    These tests read the LIVE default parameter values off the function
    signatures via inspect.signature() and assert they equal the
    corresponding DEFAULT_ constant. This is the strongest form of the
    guard: it would fail if a future edit changed a signature default
    without updating the constant (drift), not merely if the two numbers
    happen to differ today.
    """

    def test_is_sun_facing_window_defaults_track_module_constants_single_source(self):
        sig = inspect.signature(awning_automation.is_sun_facing_window)
        self.assertEqual(
            sig.parameters["min_deg"].default,
            awning_automation.DEFAULT_SUN_AZIMUTH_MIN_DEG,
            "is_sun_facing_window's min_deg default must track "
            "DEFAULT_SUN_AZIMUTH_MIN_DEG, not a hardcoded literal.",
        )
        self.assertEqual(
            sig.parameters["max_deg"].default,
            awning_automation.DEFAULT_SUN_AZIMUTH_MAX_DEG,
            "is_sun_facing_window's max_deg default must track "
            "DEFAULT_SUN_AZIMUTH_MAX_DEG, not a hardcoded literal.",
        )

    def test_should_open_awning_defaults_track_module_constants_single_source(self):
        sig = inspect.signature(awning_automation.should_open_awning)
        self.assertEqual(
            sig.parameters["min_dni_direct"].default,
            awning_automation.DEFAULT_MIN_DNI_DIRECT_WM2,
            "should_open_awning's min_dni_direct default must track "
            "DEFAULT_MIN_DNI_DIRECT_WM2, not a hardcoded literal.",
        )
        self.assertEqual(
            sig.parameters["sun_azimuth_min"].default,
            awning_automation.DEFAULT_SUN_AZIMUTH_MIN_DEG,
            "should_open_awning's sun_azimuth_min default must track "
            "DEFAULT_SUN_AZIMUTH_MIN_DEG, not a hardcoded literal.",
        )
        self.assertEqual(
            sig.parameters["sun_azimuth_max"].default,
            awning_automation.DEFAULT_SUN_AZIMUTH_MAX_DEG,
            "should_open_awning's sun_azimuth_max default must track "
            "DEFAULT_SUN_AZIMUTH_MAX_DEG, not a hardcoded literal.",
        )

    def test_get_thresholds_getenv_fallback_tracks_module_constants_single_source(self):
        """
        get_thresholds() reads its three getenv fallbacks from the same
        DEFAULT_ constants (via str(DEFAULT_...)) rather than a restated
        literal, so clearing the env vars must yield thresholds equal to
        the module constants.
        """
        env_overrides = {
            "WIND_SPEED_THRESHOLD_MPH": "15",
            "MIN_SUN_ALTITUDE_DEG": "15",
        }
        for var in (
            "MIN_DNI_DIRECT_WM2",
            "SUN_AZIMUTH_MIN_DEG",
            "SUN_AZIMUTH_MAX_DEG",
        ):
            env_overrides.pop(var, None)

        with unittest.mock.patch.dict(
            os.environ,
            env_overrides,
            clear=False,
        ):
            for var in (
                "MIN_DNI_DIRECT_WM2",
                "SUN_AZIMUTH_MIN_DEG",
                "SUN_AZIMUTH_MAX_DEG",
            ):
                os.environ.pop(var, None)
            thresholds = get_thresholds()

        min_dni_direct = thresholds[12]
        sun_azimuth_min = thresholds[13]
        sun_azimuth_max = thresholds[14]

        self.assertEqual(min_dni_direct, awning_automation.DEFAULT_MIN_DNI_DIRECT_WM2)
        self.assertEqual(sun_azimuth_min, awning_automation.DEFAULT_SUN_AZIMUTH_MIN_DEG)
        self.assertEqual(sun_azimuth_max, awning_automation.DEFAULT_SUN_AZIMUTH_MAX_DEG)


class TestCloseNotificationAttribution(unittest.TestCase):
    """
    The Telegram close message must name the rain signal that actually fired.

    The rain gate has five signals and only one of them is `precipitation`. Reporting
    the precipitation value unconditionally meant a radar-triggered close announced
    "Rain starting (0.0 mm/h)" — truthful about precipitation, but a self-contradiction
    to the reader, and giving no clue which signal to investigate. Reported repeatedly
    by the operator; the 2026-08-13 13:15 radar-clutter close was the final instance.
    """

    _CONDITIONS_RAIN = {
        "sunny": True,
        "calm": True,
        "no_rain": False,
        "above_freezing": True,
        "daytime": True,
        "sun_high": True,
        "sun_facing_window": True,
    }

    def test_close_message_names_radar_signal(self):
        """Radar-triggered close names radar, and does NOT report a bare 0.0 mm/h."""
        from awning_automation import build_close_reason

        msg = build_close_reason(
            self._CONDITIONS_RAIN,
            wind_speed=9.7, precipitation=0.0, temperature=92.3,
            ghi=910.0, uv_index=7.5, dni=789.0, cloud_cover=24.0,
            rain_attribution="radar(NEXRAD)",
        )

        self.assertIn("radar(NEXRAD)", msg)
        self.assertNotIn(
            "0.0 mm/h", msg,
            "A radar-triggered close must not report the precipitation value — "
            "that is what made the notification read as a contradiction.",
        )

    def test_close_message_names_probability_signal(self):
        """Forecast-probability close names the probability, not the precipitation value."""
        from awning_automation import build_close_reason

        msg = build_close_reason(
            self._CONDITIONS_RAIN,
            wind_speed=5.0, precipitation=0.0, temperature=70.0,
            ghi=500.0, uv_index=5.0, dni=300.0, cloud_cover=60.0,
            rain_attribution="prob=45%>=20%",
        )

        self.assertIn("prob=45%>=20%", msg)
        self.assertNotIn("0.0 mm/h", msg)

    def test_close_message_falls_back_without_attribution(self):
        """No attribution supplied → original precipitation-based message is preserved."""
        from awning_automation import build_close_reason

        msg = build_close_reason(
            self._CONDITIONS_RAIN,
            wind_speed=5.0, precipitation=2.4, temperature=60.0,
            ghi=200.0, uv_index=2.0, dni=50.0, cloud_cover=90.0,
        )

        self.assertIn("2.4 mm/h", msg)

    def test_attribution_does_not_affect_non_rain_closes(self):
        """A windy close is unaffected by a stale attribution value."""
        from awning_automation import build_close_reason

        conditions = dict(self._CONDITIONS_RAIN, no_rain=True, calm=False)
        msg = build_close_reason(
            conditions,
            wind_speed=22.0, precipitation=0.0, temperature=70.0,
            ghi=800.0, uv_index=6.0, dni=700.0, cloud_cover=10.0,
            rain_attribution="radar(NEXRAD)",
        )

        self.assertIn("22 mph", msg)
        self.assertNotIn("radar", msg)

    def test_main_composition_root_sends_attributed_message(self):
        """
        Composition-root test: main()'s real wiring must carry the attribution
        into the Telegram message.

        The unit tests above inject rain_attribution directly, so they would stay
        green even if main() never plumbed it through. main() is also the one path
        a --dry-run cannot exercise: dry-run returns before the notification block.
        This drives main() end to end on the 2026-08-13 13:15 conditions with the
        radar forced to fire, and asserts the sent message names radar.
        """
        import sys
        from unittest.mock import patch, MagicMock
        import awning_automation

        mock_controller = MagicMock()
        mock_controller.get_state.side_effect = [1, 0]  # open before, closed after

        mock_log_path = MagicMock()
        mock_log_path.parent = MagicMock()

        weather = _weather(
            shortwave_radiation=910.0,
            uv_index=7.5,
            dni=789.0,
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=1,
            cloud_cover=24.0,
            cloud_cover_low=0.0,
            cloud_cover_mid=20.0,   # over the 15% ceiling → clear-sky radar veto abstains
            cloud_cover_high=18.0,
            wind_speed=9.7,
            temperature=92.3,
            sunrise="2026-08-13T06:32:00",
            sunset="2026-08-13T20:07:00",
        )
        # main() reads weather["time"]; _weather() does not supply it.
        weather["time"] = "2026-08-13T13:15:00"

        with patch.object(sys, "argv", ["awning_automation.py"]), \
             patch.object(awning_automation, "setup_logging", return_value=mock_log_path), \
             patch.object(awning_automation, "load_location_config", return_value=(35.778, -78.838)), \
             patch.object(awning_automation, "get_thresholds",
                          return_value=(15, 15, 400, 4, 50, 80, 45, 95, 30, 20, 650.0, 15.0, 450.0, 60.0, 249.0, True)), \
             patch.object(awning_automation, "load_telegram_config", return_value=("fake_token", "fake_chat")), \
             patch.object(awning_automation, "collect_weather_measurements", return_value=weather), \
             patch.object(awning_automation, "calculate_sun_position",
                          return_value={"azimuth": 176.6, "altitude": 68.7}), \
             patch.object(awning_automation, "is_raining_on_radar", return_value=True), \
             patch.object(awning_automation, "create_controller_from_env", return_value=mock_controller), \
             patch.object(awning_automation, "send_telegram_notification") as mock_telegram:
            awning_automation.main()

        mock_controller.close.assert_called_once()
        mock_telegram.assert_called_once()
        sent_msg = mock_telegram.call_args[0][2]
        self.assertIn(
            "radar(NEXRAD)", sent_msg,
            f"main() must plumb the rain attribution into the Telegram message; got: {sent_msg!r}",
        )
        self.assertNotIn("0.0 mm/h", sent_msg)

    def test_should_open_awning_forwards_attribution(self):
        """should_open_awning fills the _attribution out-param when the rain gate closes."""
        from unittest.mock import patch
        from awning_automation import should_open_awning

        out: list = []
        with patch("awning_automation.is_raining_on_radar", return_value=False):
            should_open, reason, conditions = should_open_awning(
                weather=_weather(
                    shortwave_radiation=700.0,
                    uv_index=7.0,
                    precipitation=0.0,
                    hourly_precip_prob=80,      # forecast probability fires
                    minutely_15_precip=[],
                    weather_code=0,
                    dni=300.0,
                    cloud_cover=70.0,
                    cloud_cover_low=60.0,       # keeps the forecast veto disengaged
                    cloud_cover_mid=40.0,
                    wind_speed=5.0,
                    temperature=75.0,
                ),
                sun_position={"azimuth": 150.0, "altitude": 50.0},
                current_time=datetime(2026, 8, 13, 13, 15),
                wind_threshold=15.0,
                altitude_threshold=15.0,
                min_ghi=400.0,
                min_uv_index=4.0,
                rain_probability_threshold=20,
                _attribution=out,
            )

        self.assertFalse(should_open, "Rain gate should have closed on 80% probability")
        self.assertTrue(out, "_attribution must be populated when the rain gate closes")
        self.assertIn("prob=", out[0])
        self.assertTrue(
            all(isinstance(v, bool) for v in conditions.values()),
            "conditions must remain all-bool — should_open is all(conditions.values())",
        )


class TestCrossModelSunnyRescue(unittest.TestCase):
    """
    Cross-model sun confirmation — the 2026-08-14 GFS DNI-collapse incident.

    Open-Meteo's best_match blend resolves to GFS/HRRR at this location. On
    2026-08-14 GFS reported DNI 0 W/m² and cloud_cover_high 100% on a cloudless
    82°F morning while ECMWF (574) and ICON (754) both showed strong direct sun.
    The awning opened at 08:30, closed at 08:45, and stayed shut all morning.
    """

    # The exact readings the Pi logged at 08:45 on 2026-08-14.
    INCIDENT_WEATHER = dict(
        shortwave_radiation=150.0,
        uv_index=2.0,
        dni=0.0,
        cloud_cover=100.0,
        cloud_cover_low=0.0,
        cloud_cover_mid=0.0,
        cloud_cover_high=100.0,
        temperature=82.1,
        wind_speed=3.6,
    )

    # Sun was up, high, and on the window — only the sunny gate failed.
    SUN = dict(azimuth=90.4, altitude=25.4)

    @staticmethod
    def _crosscheck(ecmwf, icon, slot_time="2026-08-14T08:45"):
        return {
            "dni": {"ecmwf_ifs025": ecmwf, "icon_seamless": icon},
            "ghi": {"ecmwf_ifs025": None, "icon_seamless": None},
            "slot_time": slot_time,
        }

    def _run(self, crosscheck, **overrides):
        """Evaluate the incident conditions with a given cross-check result."""
        kwargs = dict(_THRESHOLDS)
        kwargs.update(lat=35.778, lon=-78.838)
        kwargs.update(overrides)
        weather_kwargs = dict(self.INCIDENT_WEATHER)
        for key in list(weather_kwargs):
            if key in kwargs:
                weather_kwargs[key] = kwargs.pop(key)
        patcher = unittest.mock.patch.object(
            awning_automation,
            "fetch_crosscheck_irradiance",
            return_value=crosscheck,
        )
        with patcher as mock_fetch:
            result = should_open_awning(
                _weather(**weather_kwargs),
                _sun(**self.SUN),
                _DAYTIME,
                **kwargs,
            )
        return result + (mock_fetch,)

    # -- The headline regression -------------------------------------------

    def test_regression_2026_08_14_gfs_dni_collapse_reopens(self):
        """All three primary layers fail; ECMWF+ICON rescue it."""
        w = _weather(**self.INCIDENT_WEATHER)

        # Layer 1 (model): GHI 150 < 400, UV 2.0 < 4.0, DNI 0 < 450.
        self.assertLess(w["shortwave_radiation"], 400.0)
        self.assertLess(w["uv_index"], 4.0)
        self.assertLess(w["dni"], 450.0)
        # Layer 2 (consistency): DNI 0 < 50 AND cloud 100% >= 80%.
        self.assertLess(w["dni"], 50.0)
        self.assertGreaterEqual(w["cloud_cover"], 80.0)
        # Layer 3 (overcast ceiling): cloud_high 100% >= 95%, and — the crux of
        # this incident — the MIN_DNI_CIRRUS_WM2 guard that exists to override a
        # bogus ceiling is itself disarmed, because DNI is the corrupted field.
        self.assertGreaterEqual(
            max(w["cloud_cover_mid"], w["cloud_cover_high"]), 95.0
        )
        self.assertLess(w["dni"], 30.0)

        should_open, reason, conditions, _ = self._run(
            self._crosscheck(574.0, 754.0)
        )

        self.assertTrue(
            should_open,
            "Awning must open: two independent models both prove direct sun",
        )
        self.assertTrue(conditions["sunny"])
        self.assertIn("RESCUE", reason)
        self.assertIn("574", reason)
        self.assertIn("754", reason)
        # The primary feed's verdict must still be reported honestly.
        self.assertIn("primary feed disagreed", reason)

    # -- Both models must agree --------------------------------------------

    def test_rescue_requires_both_models(self):
        for ecmwf, icon, expected in [
            (574.0, 754.0, True),
            (574.0, 300.0, False),   # ICON disagrees
            (300.0, 754.0, False),   # ECMWF disagrees
            (300.0, 300.0, False),   # neither
        ]:
            with self.subTest(ecmwf=ecmwf, icon=icon):
                should_open, _, conditions, _ = self._run(
                    self._crosscheck(ecmwf, icon)
                )
                self.assertEqual(conditions["sunny"], expected)
                self.assertEqual(should_open, expected)

    def test_0830_slot_does_not_rescue(self):
        """
        Today's 08:30 numbers (ECMWF 394, ICON 479): the weakest model is below
        450, so no rescue. Pins the empirically-sized 3-4% firing rate — the
        rule is not a blanket 'trust any model that feels sunny'.
        """
        should_open, _, conditions, _ = self._run(self._crosscheck(394.0, 479.0))
        self.assertFalse(conditions["sunny"])
        self.assertFalse(should_open)

    def test_rescue_reuses_min_dni_direct(self):
        """No fourth DNI threshold: the rescue bar IS min_dni_direct."""
        _, _, conditions, _ = self._run(
            self._crosscheck(574.0, 754.0), min_dni_direct=600.0
        )
        self.assertFalse(conditions["sunny"], "574 < 600 must not rescue")

        # Inclusive comparison at exactly the threshold.
        _, _, conditions, _ = self._run(
            self._crosscheck(450.0, 450.0), min_dni_direct=450.0
        )
        self.assertTrue(conditions["sunny"], "DNI == threshold must rescue")

    # -- Fail-open ----------------------------------------------------------

    def test_fails_open_when_crosscheck_unavailable(self):
        """A None result must leave the verdict byte-identical to no-rescue."""
        should_open, reason, conditions, _ = self._run(None)
        self.assertFalse(should_open)
        self.assertFalse(conditions["sunny"])
        self.assertIn("Not sunny", reason)
        self.assertNotIn("RESCUE", reason)

    def test_fetch_exception_does_not_propagate(self):
        """
        The fetch already catches everything, but an unexpected raise must not
        escape either — main()'s fail-safe would CLOSE the awning, the exact
        outcome this whole mechanism exists to prevent.
        """
        with unittest.mock.patch.object(
            awning_automation,
            "fetch_crosscheck_irradiance",
            side_effect=Exception("boom"),
        ):
            should_open, reason, conditions = should_open_awning(
                _weather(**self.INCIDENT_WEATHER),
                _sun(**self.SUN),
                _DAYTIME,
                lat=35.778,
                lon=-78.838,
                **_THRESHOLDS,
            )
        # Degrades to primary-feed-only behavior, does not blow up.
        self.assertFalse(conditions["sunny"])
        self.assertIn("Not sunny", reason)

    # -- Scope containment: rescue must not touch any other gate ------------

    def test_rescue_never_overrides_other_gates(self):
        cases = {
            "calm": dict(wind_speed=40.0),
            "above_freezing": dict(temperature=20.0),
            "no_rain": dict(precipitation=5.0),
        }
        for key, override in cases.items():
            with self.subTest(gate=key):
                weather_kwargs = dict(self.INCIDENT_WEATHER)
                weather_kwargs.update(override)
                with unittest.mock.patch.object(
                    awning_automation,
                    "fetch_crosscheck_irradiance",
                    return_value=self._crosscheck(574.0, 754.0),
                ):
                    should_open, _, conditions = should_open_awning(
                        _weather(**weather_kwargs),
                        _sun(**self.SUN),
                        _DAYTIME,
                        lat=35.778,
                        lon=-78.838,
                        **_THRESHOLDS,
                    )
                self.assertFalse(conditions[key], f"{key} must stay False")
                self.assertFalse(should_open)

    def test_rescue_is_strictly_additive(self):
        """should_open(rescue on) >= should_open(rescue off), always."""
        matrix = [
            dict(),                                    # the incident
            dict(dni=500.0, cloud_cover=10.0,
                 cloud_cover_high=10.0),               # primary already sunny
            dict(wind_speed=40.0),                     # blocked by wind
            dict(temperature=20.0),                    # blocked by cold
        ]
        for override in matrix:
            with self.subTest(override=override):
                weather_kwargs = dict(self.INCIDENT_WEATHER)
                weather_kwargs.update(override)
                results = {}
                for enabled in (False, True):
                    with unittest.mock.patch.object(
                        awning_automation,
                        "fetch_crosscheck_irradiance",
                        return_value=self._crosscheck(574.0, 754.0),
                    ):
                        results[enabled], _, _ = should_open_awning(
                            _weather(**weather_kwargs),
                            _sun(**self.SUN),
                            _DAYTIME,
                            lat=35.778,
                            lon=-78.838,
                            sunny_crosscheck_enabled=enabled,
                            **_THRESHOLDS,
                        )
                self.assertGreaterEqual(
                    int(results[True]), int(results[False]),
                    "the rescue must never turn an open into a close",
                )

    # -- No wasted requests -------------------------------------------------

    def test_no_fetch_when_primary_already_sunny(self):
        _, _, _, mock_fetch = self._run(
            self._crosscheck(574.0, 754.0),
            dni=500.0, cloud_cover=10.0, cloud_cover_high=10.0,
        )
        mock_fetch.assert_not_called()

    def test_no_fetch_when_sun_gates_closed(self):
        """Night / low sun / sun off the window: nothing to rescue."""
        for label, sun, when in [
            ("sun too low", dict(azimuth=90.0, altitude=2.0), _DAYTIME),
            ("off window", dict(azimuth=300.0, altitude=30.0), _DAYTIME),
            ("night", dict(azimuth=90.0, altitude=25.0),
             datetime(2026, 4, 17, 3, 0, 0, tzinfo=timezone.utc)),
        ]:
            with self.subTest(case=label):
                with unittest.mock.patch.object(
                    awning_automation,
                    "fetch_crosscheck_irradiance",
                    return_value=self._crosscheck(574.0, 754.0),
                ) as mock_fetch:
                    should_open_awning(
                        _weather(**self.INCIDENT_WEATHER),
                        _sun(**sun),
                        when,
                        lat=35.778,
                        lon=-78.838,
                        **_THRESHOLDS,
                    )
                mock_fetch.assert_not_called()

    def test_no_fetch_without_coordinates(self):
        with unittest.mock.patch.object(
            awning_automation,
            "fetch_crosscheck_irradiance",
            return_value=self._crosscheck(574.0, 754.0),
        ) as mock_fetch:
            should_open_awning(
                _weather(**self.INCIDENT_WEATHER),
                _sun(**self.SUN),
                _DAYTIME,
                **_THRESHOLDS,
            )
        mock_fetch.assert_not_called()

    def test_kill_switch_skips_fetch(self):
        _, _, conditions, mock_fetch = self._run(
            self._crosscheck(574.0, 754.0), sunny_crosscheck_enabled=False
        )
        mock_fetch.assert_not_called()
        self.assertFalse(conditions["sunny"])

    # -- Invariants that would otherwise break silently ---------------------

    def test_conditions_remain_all_bool_when_rescued(self):
        _, _, conditions, _ = self._run(self._crosscheck(574.0, 754.0))
        self.assertTrue(
            all(isinstance(v, bool) for v in conditions.values()),
            "conditions must stay all-bool — should_open is all(conditions.values())",
        )

    def test_rescue_does_not_populate_attribution(self):
        """
        _attribution is consumed positionally by build_close_reason(), whose
        first branch is the RAIN branch. A sunny string appended here would make
        an unrelated close announce a sun message.
        """
        out = []
        with unittest.mock.patch.object(
            awning_automation,
            "fetch_crosscheck_irradiance",
            return_value=self._crosscheck(574.0, 754.0),
        ):
            should_open_awning(
                _weather(**self.INCIDENT_WEATHER),
                _sun(**self.SUN),
                _DAYTIME,
                lat=35.778,
                lon=-78.838,
                _attribution=out,
                **_THRESHOLDS,
            )
        self.assertEqual(out, [], "the sun rescue must not touch _attribution")

    def test_crosscheck_line_logged_every_run(self):
        for label, crosscheck, enabled, expect in [
            ("engaged", self._crosscheck(574.0, 754.0), True, "RESCUE ENGAGED"),
            ("below", self._crosscheck(394.0, 479.0), True, "no rescue"),
            ("unavailable", None, True, "unavailable"),
            ("disabled", None, False, "disabled"),
        ]:
            with self.subTest(case=label):
                with unittest.mock.patch.object(
                    awning_automation,
                    "fetch_crosscheck_irradiance",
                    return_value=crosscheck,
                ):
                    with self.assertLogs(
                        awning_automation.logger, level="INFO"
                    ) as cm:
                        should_open_awning(
                            _weather(**self.INCIDENT_WEATHER),
                            _sun(**self.SUN),
                            _DAYTIME,
                            lat=35.778,
                            lon=-78.838,
                            sunny_crosscheck_enabled=enabled,
                            **_THRESHOLDS,
                        )
                line = [x for x in cm.output if "Sun crosscheck:" in x]
                self.assertEqual(len(line), 1, "exactly one crosscheck line per run")
                self.assertIn(expect, line[0])


class TestCrossModelSunnyRescueFetch(unittest.TestCase):
    """fetch_crosscheck_irradiance() — request shape, slot selection, fail-open."""

    NOW = datetime(2026, 8, 14, 12, 47, 0, tzinfo=timezone.utc)  # 08:47 EDT

    def _response(self, **overrides):
        body = {
            "utc_offset_seconds": -14400,
            "minutely_15": {
                "time": [
                    "2026-08-14T08:15",
                    "2026-08-14T08:30",
                    "2026-08-14T08:45",
                    "2026-08-14T09:00",
                ],
                "direct_normal_irradiance_ecmwf_ifs025": [351.0, 394.0, 433.0, 473.0],
                "direct_normal_irradiance_icon_seamless": [396.0, 479.0, 547.0, 608.0],
                "shortwave_radiation_ecmwf_ifs025": [225.0, 271.0, 318.0, 367.0],
                "shortwave_radiation_icon_seamless": [214.0, 267.0, 320.0, 374.0],
            },
        }
        body.update(overrides)
        return body

    def _call(self, response=None, side_effect=None, now=None):
        mock_resp = unittest.mock.Mock()
        mock_resp.json.return_value = response
        mock_resp.raise_for_status.return_value = None
        with unittest.mock.patch.object(
            awning_automation._weather_session,
            "get",
            side_effect=side_effect,
            return_value=None if side_effect else mock_resp,
        ) as mock_get:
            result = awning_automation.fetch_crosscheck_irradiance(
                35.778, -78.838, _now_utc=now or self.NOW
            )
        return result, mock_get

    def test_request_shape(self):
        result, mock_get = self._call(self._response())
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["models"], "ecmwf_ifs025,icon_seamless")
        self.assertEqual(
            params["minutely_15"], "direct_normal_irradiance,shortwave_radiation"
        )
        self.assertNotIn("current", params)
        self.assertIsNotNone(result)

    def test_selects_slot_at_or_before_now(self):
        """08:47 local must read the 08:45 slot, not 09:00."""
        result, _ = self._call(self._response())
        self.assertEqual(result["slot_time"], "2026-08-14T08:45")
        self.assertEqual(result["dni"]["ecmwf_ifs025"], 433.0)
        self.assertEqual(result["dni"]["icon_seamless"], 547.0)

    def test_stale_slot_rejected(self):
        """A response whose newest slot is hours old must not rescue."""
        result, _ = self._call(
            self._response(),
            now=datetime(2026, 8, 14, 22, 0, 0, tzinfo=timezone.utc),  # 18:00 EDT
        )
        self.assertIsNone(result)

    def test_fail_open_cases(self):
        cases = {
            "missing minutely_15": {"utc_offset_seconds": -14400},
            "empty time array": {
                "utc_offset_seconds": -14400,
                "minutely_15": {"time": []},
            },
            "missing utc offset": {
                "minutely_15": self._response()["minutely_15"],
            },
            "no slot at or before now": {
                "utc_offset_seconds": -14400,
                "minutely_15": {
                    "time": ["2026-08-14T23:00"],
                    "direct_normal_irradiance_ecmwf_ifs025": [500.0],
                    "direct_normal_irradiance_icon_seamless": [500.0],
                },
            },
        }
        for label, body in cases.items():
            with self.subTest(case=label):
                result, _ = self._call(body)
                self.assertIsNone(result, f"{label} must fail open")

    def test_null_dni_fails_open(self):
        body = self._response()
        body["minutely_15"]["direct_normal_irradiance_icon_seamless"] = [
            396.0, 479.0, None, 608.0
        ]
        result, _ = self._call(body)
        self.assertIsNone(result)

    def test_missing_model_series_fails_open(self):
        body = self._response()
        del body["minutely_15"]["direct_normal_irradiance_icon_seamless"]
        result, _ = self._call(body)
        self.assertIsNone(result)

    def test_network_error_fails_open(self):
        import requests
        result, _ = self._call(
            side_effect=requests.exceptions.ConnectionError("no route")
        )
        self.assertIsNone(result)

    def test_malformed_json_fails_open(self):
        result, _ = self._call(side_effect=ValueError("not json"))
        self.assertIsNone(result)

    def test_primary_request_params_never_carry_models(self):
        """
        Guards the two API findings behind this design: `current=` silently
        ignores multi-model, and an explicit model nulls out uv_index. A future
        consolidation of the two requests would break the Layer 1 UV arm.
        """
        mock_resp = unittest.mock.Mock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status.return_value = None
        with unittest.mock.patch.object(
            awning_automation._weather_session, "get", return_value=mock_resp
        ) as mock_get:
            with self.assertRaises(WeatherAPIError):
                fetch_weather(35.778, -78.838)
        params = mock_get.call_args.kwargs["params"]
        self.assertNotIn("models", params)
        self.assertIn("uv_index", params["current"])


class TestCrossModelRescueCompositionRoot(unittest.TestCase):
    """
    Drive the real main() on the 2026-08-14 incident numbers.

    The unit tests above pass sunny_crosscheck_enabled directly, so they would
    stay green even if main() never plumbed the flag through — and main() is the
    one path a --dry-run cannot exercise. This is the test that catches a missing
    kwarg in the wiring.
    """

    def test_main_opens_awning_on_cross_model_rescue(self):
        import sys
        from unittest.mock import patch, MagicMock

        mock_controller = MagicMock()
        mock_controller.get_state.side_effect = [0, 1]  # closed before, open after

        mock_log_path = MagicMock()
        mock_log_path.parent = MagicMock()

        # Exactly what the Pi logged at 08:45 on 2026-08-14.
        weather = _weather(
            shortwave_radiation=150.0,
            uv_index=2.0,
            dni=0.0,
            cloud_cover=100.0,
            cloud_cover_low=0.0,
            cloud_cover_mid=0.0,
            cloud_cover_high=100.0,
            wind_speed=3.6,
            temperature=82.1,
            precipitation=0.0,
            hourly_precip_prob=0,
            minutely_15_precip=[],
            weather_code=0,
            sunrise="2026-08-14T06:33:00",
            sunset="2026-08-14T20:06:00",
        )
        weather["time"] = "2026-08-14T08:45:00"

        crosscheck = {
            "dni": {"ecmwf_ifs025": 433.0, "icon_seamless": 547.0},
            "ghi": {"ecmwf_ifs025": 318.0, "icon_seamless": 320.0},
            "slot_time": "2026-08-14T08:45",
        }

        # Deliberately NOT patching get_thresholds: this exercises the real
        # 16-tuple unpack in main(), which is where a wiring mistake would hide.
        env = {
            "WIND_SPEED_THRESHOLD_MPH": "15",
            "MIN_SUN_ALTITUDE_DEG": "15",
        }

        def run(crosscheck_result):
            mock_controller.reset_mock()
            mock_controller.get_state.side_effect = [0, 1]
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(sys, "argv", ["awning_automation.py"]), \
                 patch.object(awning_automation, "setup_logging", return_value=mock_log_path), \
                 patch.object(awning_automation, "load_location_config", return_value=(35.778, -78.838)), \
                 patch.object(awning_automation, "load_telegram_config", return_value=(None, None)), \
                 patch.object(awning_automation, "collect_weather_measurements", return_value=weather), \
                 patch.object(awning_automation, "calculate_sun_position",
                              return_value={"azimuth": 90.4, "altitude": 25.4}), \
                 patch.object(awning_automation, "is_raining_on_radar", return_value=False), \
                 patch.object(awning_automation, "fetch_crosscheck_irradiance",
                              return_value=crosscheck_result), \
                 patch.object(awning_automation, "create_controller_from_env",
                              return_value=mock_controller):
                awning_automation.main()
            return mock_controller

        # With a rescue that clears the bar (both >= 450 at the default), open.
        strong = dict(crosscheck)
        strong["dni"] = {"ecmwf_ifs025": 574.0, "icon_seamless": 754.0}
        controller = run(strong)
        controller.open.assert_called_once()
        controller.close.assert_not_called()

        # The real 08:45 slot: ECMWF 433 is below 450, so no rescue — closed.
        controller = run(crosscheck)
        controller.close.assert_called_once()
        controller.open.assert_not_called()


class TestGetThresholdsSunnyCrosscheck(unittest.TestCase):
    """SUNNY_CROSSCHECK_ENABLED parsing."""

    def _get(self, value):
        env = {
            "WIND_SPEED_THRESHOLD_MPH": "15",
            "MIN_SUN_ALTITUDE_DEG": "20",
        }
        if value is not None:
            env["SUNNY_CROSSCHECK_ENABLED"] = value
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            return get_thresholds()[15]

    def test_defaults_to_enabled(self):
        self.assertTrue(self._get(None))
        self.assertIs(
            self._get(None), awning_automation.DEFAULT_SUNNY_CROSSCHECK_ENABLED
        )

    def test_truthy_and_falsy_values(self):
        for value in ("true", "True", "1", "yes", "ON"):
            with self.subTest(value=value):
                self.assertTrue(self._get(value))
        for value in ("false", "False", "0", "no", "OFF"):
            with self.subTest(value=value):
                self.assertFalse(self._get(value))

    def test_invalid_value_raises(self):
        with self.assertRaises(ConfigurationError):
            self._get("maybe")


if __name__ == "__main__":
    unittest.main()
