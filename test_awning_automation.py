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
    dni=320.0,
    cloud_cover_low=10.0,
    cloud_cover_mid=10.0,
    cloud_cover_high=5.0,
    sunrise="2026-04-17T06:00:00",
    sunset="2026-04-17T20:00:00",
):
    return {
        "wind_speed_10m": wind_speed,
        "precipitation": precipitation,
        "temperature": temperature,
        "dni": dni,
        "cloud_cover_low": cloud_cover_low,
        "cloud_cover_mid": cloud_cover_mid,
        "cloud_cover_high": cloud_cover_high,
        "sunrise": sunrise,
        "sunset": sunset,
    }


def _sun(*, azimuth=150.0, altitude=35.0):
    return {"azimuth": azimuth, "altitude": altitude}


# Standard thresholds used in most tests — match typical .env defaults
_THRESHOLDS = dict(
    wind_threshold=15.0,
    altitude_threshold=20.0,
    dni_threshold=300.0,
    max_low_cloud=50.0,
    max_mid_cloud=30.0,
    cirrus_high_threshold=60.0,
    min_dni_cirrus=50.0,
    mid_cloud_threshold=73.0,
    dni_mid_cloud_override=400.0,
)

# Daytime moment that falls between the default sunrise/sunset strings above
_DAYTIME = datetime(2026, 4, 17, 13, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Import function under test (late import so module-level side effects are
# contained; ConfigurationError is also imported for the H-2 validation test)
# ---------------------------------------------------------------------------
from awning_automation import should_open_awning, ConfigurationError, get_thresholds


class TestShouldOpenAwning(unittest.TestCase):
    """Tests for the should_open_awning() decision function."""

    # ------------------------------------------------------------------
    # L-3 Test 1 — Normal open (B-1 regression guard)
    # DNI=320 < override=400, cloud_mid=10% below gate=73%, all other
    # conditions pass → should_open must be True.
    # Before B-1 fix, mid_cloud_gate_vetoed=False was included in
    # primary_conditions, making all(...) False even when mid clouds are fine.
    # ------------------------------------------------------------------
    def test_normal_open_dni_below_override(self):
        """B-1: awning opens when DNI < override and all clear conditions pass."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(dni=320.0, cloud_cover_mid=10.0),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            should_open,
            f"Expected awning to open but got False. reason={reason!r}, conditions={conditions}",
        )
        # mid_cloud_gate_vetoed must be False (DNI 320 < override 400) — confirms the
        # test is actually exercising the pre-fix code path that caused B-1.
        self.assertFalse(conditions["mid_cloud_gate_vetoed"])
        # cirrus_dominated should also be False (low high-cloud cover)
        self.assertFalse(conditions["cirrus_dominated"])

    # ------------------------------------------------------------------
    # L-3 Test 2 — Override active: DNI >= override, cloud_mid above gate
    # DNI=500 >= override=400 → mid_cloud_gate_vetoed=True → awning opens
    # despite cloud_mid=80% exceeding mid_cloud_threshold=73%.
    # ------------------------------------------------------------------
    def test_override_active_high_mid_cloud(self):
        """Override path: high DNI vetoes mid-cloud gate → awning opens."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(dni=500.0, cloud_cover_mid=80.0),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertTrue(
            should_open,
            f"Expected awning to open with override active but got False. reason={reason!r}",
        )
        self.assertTrue(conditions["mid_cloud_gate_vetoed"])

    # ------------------------------------------------------------------
    # L-3 Test 3 — Override inactive, cloud_mid too high → awning closes
    # DNI=200 < override=400 → no veto; cloud_mid=80% >= gate=73% → closed.
    # ------------------------------------------------------------------
    def test_override_inactive_high_mid_cloud_blocks_open(self):
        """No override + high mid-cloud → mid-cloud gate fires → awning stays closed."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(dni=200.0, cloud_cover_mid=80.0),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        self.assertFalse(
            should_open,
            f"Expected awning to stay closed due to mid-cloud gate but got True. reason={reason!r}",
        )
        self.assertFalse(conditions["mid_cloud_gate_vetoed"])
        # is_sunny must be False (mid-cloud gate fires, DNI also below threshold)
        self.assertFalse(conditions["sunny"])

    # ------------------------------------------------------------------
    # L-3 Test 4 — Zero DNI_MID_CLOUD_OVERRIDE_WM2 rejected during validation
    # get_thresholds() reads from env; we set the env var to "0" and expect
    # ConfigurationError to be raised (H-2 fix).
    # ------------------------------------------------------------------
    def test_zero_dni_override_rejected_by_validation(self):
        """H-2: DNI_MID_CLOUD_OVERRIDE_WM2=0 must raise ConfigurationError."""
        original = os.environ.get("DNI_MID_CLOUD_OVERRIDE_WM2")
        # Also set all other required thresholds so we reach the override check
        env_patch = {
            "WIND_SPEED_THRESHOLD_MPH": "15",
            "MIN_SUN_ALTITUDE_DEG": "20",
            "MIN_DIRECT_IRRADIANCE_WM2": "300",
            "DNI_MID_CLOUD_OVERRIDE_WM2": "0",
        }
        original_values = {}
        for k, v in env_patch.items():
            original_values[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            with self.assertRaises(ConfigurationError) as ctx:
                get_thresholds()
            self.assertIn("0", str(ctx.exception))
        finally:
            # Restore env state
            for k, orig in original_values.items():
                if orig is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = orig

    # ------------------------------------------------------------------
    # L-3 Test 5 — Cirrus + override interaction
    # cirrus_dominated=True (high=80%, mid=10% < max_mid_cloud=30%),
    # DNI=100 W/m² — well below override (400 W/m²) → mid_cloud_gate_vetoed=False.
    # In cirrus mode effective_dni_threshold=min_dni_cirrus=50 → DNI 100 >= 50 → sunny.
    # Decision should be driven by the cirrus path, not the override.
    # ------------------------------------------------------------------
    def test_cirrus_override_does_not_fire_in_cirrus_mode(self):
        """H-1: cirrus mode with DNI=100 < override=400 — override branch silent, cirrus path governs."""
        should_open, reason, conditions = should_open_awning(
            weather=_weather(
                dni=100.0,
                cloud_cover_low=5.0,
                cloud_cover_mid=10.0,   # < max_mid_cloud=30 → qualifies for cirrus classification
                cloud_cover_high=80.0,  # > cirrus_high_threshold=60 → cirrus_dominated=True
            ),
            sun_position=_sun(),
            current_time=_DAYTIME,
            **_THRESHOLDS,
        )
        # Cirrus-dominated → DNI threshold relaxed to min_dni_cirrus=50 → sunny=True
        self.assertTrue(conditions["cirrus_dominated"])
        # Override NOT active — DNI 100 < override 400
        self.assertFalse(conditions["mid_cloud_gate_vetoed"])
        # All other conditions are fine → awning should open via cirrus path
        self.assertTrue(
            should_open,
            f"Expected awning to open via cirrus path but got False. reason={reason!r}",
        )


if __name__ == "__main__":
    unittest.main()
