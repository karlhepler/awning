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
