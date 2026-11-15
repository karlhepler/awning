"""
Tests for awning.py — the manual command-line interface.

Run:  python3 -m unittest test_awning_cli -v
"""
import io
import unittest
import unittest.mock

from rich.console import Console

import awning
from awning_controller import BondAPIError, ConfigurationError


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.buffer = io.StringIO()
        patcher = unittest.mock.patch.object(
            awning, "console", Console(file=self.buffer, force_terminal=False, width=140)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controller = unittest.mock.MagicMock()
        self.cli = awning.AwningCLI(self.controller)

    @property
    def out(self):
        return self.buffer.getvalue()

    def run_cli(self, *argv, controller=None):
        """Run main() as `awning <argv>`; return the exit code (None = fell off the end)."""
        with unittest.mock.patch.object(awning.sys, "argv", ["awning", *argv]), \
             unittest.mock.patch.object(
                 awning, "create_controller_from_env", return_value=controller or self.controller
             ):
            try:
                awning.main()
            except SystemExit as e:
                return e.code
        return None


class TestCommands(CliTestCase):
    def test_each_command_calls_its_controller_method(self):
        for command, method, text in (
            ("open", "open", "Awning is opening"), ("close", "close", "Awning is closing"),
            ("stop", "stop", "Awning stopped"), ("toggle", "toggle", "Awning toggled"),
        ):
            with self.subTest(command=command):
                self.buffer.truncate(0), self.buffer.seek(0)
                self.controller.reset_mock()
                self.assertIsNone(self.run_cli(command))
                getattr(self.controller, method).assert_called_once_with()
                self.assertIn(text, self.out)

    def test_a_bond_error_is_one_line_and_exit_1(self):
        for command in ("open", "close", "stop", "toggle", "status", "info"):
            with self.subTest(command=command):
                self.buffer.truncate(0), self.buffer.seek(0)
                controller = unittest.mock.MagicMock()
                for name in ("open", "close", "stop", "toggle", "get_state", "get_info"):
                    getattr(controller, name).side_effect = BondAPIError("bridge unreachable")
                self.assertEqual(self.run_cli(command, controller=controller), 1)
                self.assertIn("bridge unreachable", self.out)
                self.assertNotIn("Traceback", self.out)

    def test_status_reports_open_closed_and_unknown(self):
        for state, text in ((1, "OPEN"), (0, "CLOSED"), (None, "Awning state")):
            with self.subTest(state=state):
                self.buffer.truncate(0), self.buffer.seek(0)
                self.controller.get_state.return_value = state
                self.assertIsNone(self.run_cli("status"))
                self.assertIn(text, self.out)

    def test_info_prints_known_and_unknown_fields(self):
        self.controller.get_info.return_value = {
            "name": "Awning", "type": "SH", "actions": ["Open", "Close"], "custom_thing": 7,
        }
        self.assertIsNone(self.run_cli("info"))
        for expected in ("Awning", "Open, Close", "Custom Thing"):
            self.assertIn(expected, self.out)

    def test_info_with_a_non_dict_reply_is_an_error_not_an_attribute_error(self):
        for reply in (["x"], "oops", None):
            with self.subTest(reply=reply):
                self.buffer.truncate(0), self.buffer.seek(0)
                self.controller.get_info.return_value = reply
                self.assertEqual(self.run_cli("info"), 1)
                self.assertIn("Unexpected device info response", self.out)


class TestMain(CliTestCase):
    def test_no_arguments_and_help_flags_show_help_and_exit_0(self):
        for argv in ((), ("--help",), ("-h",), ("help",)):
            with self.subTest(argv=argv):
                self.buffer.truncate(0), self.buffer.seek(0)
                self.assertEqual(self.run_cli(*argv), 0)
                self.assertIn("Awning Controller", self.out)

    def test_unknown_command_and_wrong_arity_exit_1(self):
        self.assertEqual(self.run_cli("explode"), 1)
        self.assertIn("Unknown command 'explode'", self.out)
        self.buffer.truncate(0), self.buffer.seek(0)
        self.assertEqual(self.run_cli("open", "close"), 1)
        self.assertIn("Invalid number of arguments", self.out)

    def test_missing_configuration_is_reported_and_exit_1(self):
        with unittest.mock.patch.object(awning.sys, "argv", ["awning", "open"]), \
             unittest.mock.patch.object(
                 awning, "create_controller_from_env", side_effect=ConfigurationError("BOND_TOKEN missing")
             ):
            with self.assertRaises(SystemExit) as ctx:
                awning.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("BOND_TOKEN missing", self.out)

    def test_an_unexpected_exception_becomes_one_line_not_a_traceback(self):
        self.controller.get_state.side_effect = AttributeError("'list' object has no attribute 'get'")
        self.assertEqual(self.run_cli("status"), 1)
        self.assertIn("Unexpected error", self.out)
        self.assertIn("AttributeError", self.out)
        self.assertNotIn("Traceback", self.out)


if __name__ == "__main__":
    unittest.main()


class TestHoldFlags(CliTestCase):
    """`open|close --until T | --forever`, `auto`, `override`: set on the Pi, never on Bond."""

    def setUp(self):
        super().setUp()
        patcher = unittest.mock.patch.object(awning.awning_override, "set_override", return_value="Decision: ok")
        self.set_override = patcher.start()
        self.addCleanup(patcher.stop)

    def test_close_forever_holds_closed_with_no_end_and_never_touches_bond(self):
        self.assertIsNone(self.run_cli("close", "--forever"))
        self.set_override.assert_called_once_with("closed", None)
        self.controller.close.assert_not_called()
        self.assertIn("Held closed", self.out)

    def test_open_until_holds_open_until_the_parsed_time(self):
        self.assertIsNone(self.run_cli("open", "--until", "5pm"))
        mode, until = self.set_override.call_args[0]
        self.assertEqual((mode, until.hour, until.minute), ("open", 17, 0))
        self.controller.open.assert_not_called()

    def test_until_accepts_the_equals_form_and_a_length(self):
        self.assertIsNone(self.run_cli("close", "--until=2h"))
        self.assertEqual(self.set_override.call_args[0][0], "closed")
        self.assertIsNotNone(self.set_override.call_args[0][1])

    def test_plain_open_and_close_are_unchanged(self):
        self.assertIsNone(self.run_cli("close"))
        self.controller.close.assert_called_once_with()
        self.set_override.assert_not_called()

    def test_auto_hands_back_to_the_weather(self):
        self.assertIsNone(self.run_cli("auto"))
        self.set_override.assert_called_once_with("auto")
        self.assertIn("Following the weather", self.out)

    def test_override_shows_the_pi_line(self):
        with unittest.mock.patch.object(awning.awning_override, "show_override", return_value="closed until Sun 5:00 PM"):
            self.assertIsNone(self.run_cli("override"))
        self.assertIn("closed until Sun 5:00 PM", self.out)

    def test_both_flags_together_are_refused(self):
        self.assertEqual(self.run_cli("close", "--until", "5pm", "--forever"), 1)
        self.set_override.assert_not_called()
        self.assertIn("not both", self.out)

    def test_flags_on_other_commands_are_refused(self):
        for command in ("stop", "toggle", "status", "info", "auto", "override"):
            with self.subTest(command=command):
                self.assertEqual(self.run_cli(command, "--forever"), 1)
        self.set_override.assert_not_called()

    def test_until_with_no_value_or_a_bad_value_is_refused(self):
        self.assertEqual(self.run_cli("close", "--until"), 1)
        self.assertEqual(self.run_cli("close", "--until", "soon"), 1)
        self.set_override.assert_not_called()
        self.controller.close.assert_not_called()

    def test_an_unknown_extra_argument_is_refused(self):
        self.assertEqual(self.run_cli("close", "--bogus"), 1)

    def test_a_pi_failure_is_one_line_and_exit_1(self):
        self.set_override.side_effect = awning.OverrideError("Pi unreachable")
        self.assertEqual(self.run_cli("close", "--forever"), 1)
        self.assertIn("Pi unreachable", self.out)
        self.assertNotIn("Traceback", self.out)
