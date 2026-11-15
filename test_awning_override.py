"""
Tests for awning_override.py — the Mac-side half of the schedule override.

Run:  python3 -m unittest test_awning_override -v
"""
import subprocess
import unittest
import unittest.mock
from datetime import datetime, timedelta

import awning_override
from awning_override import OverrideError, parse_until

# A fixed "now" in a zone with no clock change on this day: Sunday 2026-11-15, 12:00.
NOW = datetime(2026, 11, 15, 12, 0).astimezone()


class TestParseUntil(unittest.TestCase):
    def test_a_length_is_added_to_now(self):
        self.assertEqual(parse_until("3h", NOW), NOW + timedelta(hours=3))
        self.assertEqual(parse_until("90m", NOW), NOW + timedelta(minutes=90))
        self.assertEqual(parse_until("1h30m", NOW), NOW + timedelta(hours=1, minutes=30))

    def test_a_clock_time_later_today_is_today(self):
        for text in ("5pm", "5:30pm", "5:30 pm", "17:30", "5PM"):
            with self.subTest(text=text):
                expected = NOW.replace(hour=17, minute=30 if "30" in text else 0)
                self.assertEqual(parse_until(text, NOW), expected)

    def test_a_clock_time_already_past_is_tomorrow(self):
        self.assertEqual(parse_until("8am", NOW), (NOW + timedelta(days=1)).replace(hour=8))

    def test_the_current_minute_counts_as_past(self):
        self.assertEqual(parse_until("12:00", NOW), (NOW + timedelta(days=1)).replace(hour=12))

    def test_twelve_oclock_means_noon_and_midnight(self):
        self.assertEqual(parse_until("12pm", NOW.replace(hour=9)).hour, 12)
        self.assertEqual(parse_until("12am", NOW).hour, 0)

    def test_the_result_carries_a_zone(self):
        self.assertIsNotNone(parse_until("5pm", NOW).tzinfo)
        self.assertIsNotNone(parse_until("2h", NOW).tzinfo)

    def test_garbage_is_refused_with_a_hint(self):
        for text in ("", "soon", "5", "25:00", "13pm", "0pm", "5:75pm", "0h", "0m", "-3h", "3x"):
            with self.subTest(text=text):
                with self.assertRaises(OverrideError) as cm:
                    parse_until(text, NOW)
                self.assertIn("5pm", str(cm.exception))

    def test_a_clock_time_across_a_clock_change_keeps_its_wall_time(self):
        # 2026-11-01 is the autumn clock change in the US; the day after is a 25-hour day.
        start = datetime(2026, 10, 31, 23, 0).astimezone()
        result = parse_until("8am", start)
        self.assertEqual((result.month, result.day, result.hour), (11, 1, 8))


class TestRemoteCommand(unittest.TestCase):
    def test_set_without_end_time_has_no_until_flag_and_does_not_expect_one(self):
        cmd = awning_override.remote_set_command("closed", None)
        self.assertIn("--set-override=closed", cmd)
        self.assertNotIn("--until", cmd)
        self.assertIn("[ 0 = 1 ]", cmd)

    def test_set_with_end_time_passes_it_and_checks_the_pi_stored_it(self):
        until = datetime(2026, 11, 15, 17, 0).astimezone()
        cmd = awning_override.remote_set_command("open", until)
        self.assertIn(f"--until={until.isoformat(timespec='seconds')}", cmd)
        self.assertIn("[ 1 = 1 ]", cmd)

    def test_auto_never_expects_an_end_time(self):
        until = datetime(2026, 11, 15, 17, 0).astimezone()
        self.assertIn("[ 0 = 1 ]", awning_override.remote_set_command("auto", until))

    def test_the_automation_runs_only_after_the_check(self):
        cmd = awning_override.remote_set_command("closed", None)
        self.assertLess(cmd.index("exit 1"), cmd.rindex("awning_automation.py"))


class TestSsh(unittest.TestCase):
    def _run(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_set_override_returns_what_the_pi_decided(self):
        with unittest.mock.patch.object(awning_override, "pi_host", return_value="u@pi"), \
             unittest.mock.patch.object(awning_override, "_ssh", return_value=self._run(stdout="Decision: x\n")) as ssh:
            self.assertEqual(awning_override.set_override("closed"), "Decision: x")
        self.assertEqual(ssh.call_args[0][0], "u@pi")

    def test_a_failed_pi_is_an_error_with_its_message(self):
        with unittest.mock.patch.object(awning_override, "pi_host", return_value="u@pi"), \
             unittest.mock.patch.object(awning_override, "_ssh", return_value=self._run(1, stderr="ERROR: nope")):
            with self.assertRaises(OverrideError) as cm:
                awning_override.set_override("closed")
        self.assertIn("nope", str(cm.exception))

    def test_show_override_returns_the_pi_line(self):
        with unittest.mock.patch.object(awning_override, "pi_host", return_value="u@pi"), \
             unittest.mock.patch.object(awning_override, "_ssh", return_value=self._run(stdout="closed until Sun 5:00 PM\n")):
            self.assertEqual(awning_override.show_override(), "closed until Sun 5:00 PM")

    def test_show_override_with_no_answer_is_an_error(self):
        with unittest.mock.patch.object(awning_override, "pi_host", return_value="u@pi"), \
             unittest.mock.patch.object(awning_override, "_ssh", return_value=self._run(0, stdout="")):
            with self.assertRaises(OverrideError):
                awning_override.show_override()

    def test_ssh_goes_through_lan_run_when_present(self):
        with unittest.mock.patch.object(awning_override.shutil, "which", return_value="/bin/lan-run"), \
             unittest.mock.patch.object(awning_override.subprocess, "run", return_value=self._run()) as run:
            awning_override._ssh("u@pi", "echo hi")
        self.assertEqual(run.call_args[0][0], ["lan-run", "ssh", "u@pi", "echo hi"])

    def test_ssh_without_lan_run_is_plain_ssh(self):
        with unittest.mock.patch.object(awning_override.shutil, "which", return_value=None), \
             unittest.mock.patch.object(awning_override.subprocess, "run", return_value=self._run()) as run:
            awning_override._ssh("u@pi", "echo hi")
        self.assertEqual(run.call_args[0][0], ["ssh", "u@pi", "echo hi"])

    def test_pi_host_uses_the_env_value_or_the_default(self):
        with unittest.mock.patch.dict(awning_override.os.environ, {"PI_HOST": "10.0.0.9"}):
            self.assertEqual(awning_override.pi_host(), "karlhepler@10.0.0.9")
        with unittest.mock.patch.dict(awning_override.os.environ, {"PI_HOST": ""}):
            self.assertEqual(awning_override.pi_host(), "karlhepler@orangepi3-lts")


if __name__ == "__main__":
    unittest.main()
