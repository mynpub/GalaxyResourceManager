"""Isolated username/password authentication and CLI tests."""

import contextlib
import getpass
import io
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import database
import game_logic
import main
from authentication import ITERATIONS, hash_password, verify_password
from game_logic import GameLogic

PASSWORD = "a long test passphrase"


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        data_patch = patch.object(database, "DATA_DIR", self.data_dir)
        data_patch.start()
        self.addCleanup(data_patch.stop)
        self.game = GameLogic()

    def assert_logged_out(self):
        self.assertIsNone(self.game.player_id)
        self.assertIsNone(self.game.username)
        self.assertIsNone(self.game.get_player_info())

    def test_signup_and_login_across_instances_without_payment(self):
        self.assertTrue(self.game.create_player_account("Explorer_1", PASSWORD))
        player_id = self.game.player_id
        self.assertEqual(self.game.username, "explorer_1")
        self.assertEqual(self.game.get_player_info(), {
            "id": player_id, "username": "explorer_1", "npub": None,
            "fiat_balance": 0.0, "bitcoin_balance": 0.0,
        })
        other = GameLogic()
        self.assertTrue(other.login("EXPLORER_1", PASSWORD))
        self.assertEqual(other.player_id, player_id)
        with database._connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM payments").fetchone()[0], 0)
        for obsolete in ("generate_lightning_invoice", "check_payment",
                         "complete_registration", "begin_registration"):
            self.assertFalse(hasattr(self.game, obsolete))

    def test_failed_login_clears_prior_authentication_and_uses_safe_errors(self):
        self.assertTrue(self.game.create_player_account("explorer", PASSWORD))
        for username, password in [
            ("explorer", "the wrong password"), ("missing", PASSWORD),
            ("bad' OR 1=1 --", PASSWORD), (None, PASSWORD),
            ("explorer", ""), ("explorer", None),
        ]:
            with self.subTest(username=username):
                self.assertTrue(self.game.login("explorer", PASSWORD))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertFalse(self.game.login(username, password))
                self.assert_logged_out()
                self.assertEqual(output.getvalue(),
                                 "Unable to log in. Invalid username or password, or storage unavailable.\n")
                self.assertNotIn(PASSWORD, output.getvalue())

    def test_duplicate_signup_never_logs_in_or_changes_credentials(self):
        self.assertTrue(self.game.create_player_account("explorer", PASSWORD))
        original_id = self.game.player_id
        self.assertFalse(self.game.create_player_account("EXPLORER", "different password"))
        self.assert_logged_out()
        self.assertFalse(self.game.login("explorer", "different password"))
        self.assertTrue(self.game.login("explorer", PASSWORD))
        self.assertEqual(self.game.player_id, original_id)

    def test_invalid_usernames_and_passwords_do_not_create_accounts(self):
        for username in ("", "ab", "a" * 33, " space", "space ", "étoile",
                         "a-b", "a.b", "a\nb", "name';--", None, 123):
            with self.subTest(username=username):
                self.assertFalse(self.game.create_player_account(username, PASSWORD))
                self.assert_logged_out()
        for password in ("", "a" * 11, "a" * 129, "contains\nnewline",
                         "contains\x00null", "\ud800" * 12, None, 123):
            with self.subTest(length=len(password) if isinstance(password, str) else None):
                self.assertFalse(self.game.create_player_account("valid_name", password))
                self.assert_logged_out()
        with database._connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM players").fetchone()[0], 0)

    def test_password_boundaries_unicode_and_whitespace_are_exact(self):
        for index, password in enumerate(("x" * 12, "x" * 128, " 密碼 passphrase ")):
            username = f"boundary_{index}"
            self.assertTrue(self.game.create_player_account(username, password))
            self.assertTrue(self.game.login(username, password))
            self.assertFalse(self.game.login(username, password + "x"))
        self.assertFalse(self.game.login("boundary_2", "密碼 passphrase"))
        self.assertFalse(self.game.login("boundary_1", "x" * 127 + "y"))

    def test_hashes_are_salted_nonplaintext_and_not_exposed_in_player_info(self):
        first = database.create_player("first", PASSWORD)
        database.create_player("second", PASSWORD)
        with database._connection() as connection:
            hashes = [row[0] for row in connection.execute("SELECT password_hash FROM players")]
        self.assertNotEqual(*hashes)
        for encoded in hashes:
            scheme, iterations, salt, digest = encoded.split("$")
            self.assertEqual(scheme, "pbkdf2_sha256")
            self.assertEqual(int(iterations), ITERATIONS)
            self.assertEqual(len(bytes.fromhex(salt)), 16)
            self.assertEqual(len(bytes.fromhex(digest)), 32)
            self.assertNotIn(PASSWORD, encoded)
            self.assertTrue(verify_password(PASSWORD, encoded))
            self.assertFalse(verify_password("wrong password", encoded))
        self.assertNotIn("password_hash", database.get_player_info(first))
        self.assertNotIn(PASSWORD.encode(), (self.data_dir / "galaxy_resources.db").read_bytes())

    def test_corrupt_or_unknown_hashes_fail_closed(self):
        for encoded in ("plaintext", "unknown$600000$00$00",
                        "pbkdf2_sha256$1$00$00", "pbkdf2_sha256$600000$zz$zz"):
            self.assertFalse(verify_password(PASSWORD, encoded))
        self.assertFalse(verify_password(PASSWORD, None))
        self.assertNotEqual(hash_password(PASSWORD), hash_password(PASSWORD))

    def test_storage_errors_clear_state_and_do_not_leak_details(self):
        for method, target in ((self.game.login, "authenticate_player"),
                               (self.game.create_player_account, "create_player")):
            self.assertTrue(self.game.create_player_account(f"user_{target}", PASSWORD))
            with patch(f"game_logic.{target}", side_effect=sqlite3.OperationalError(PASSWORD)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertFalse(method("explorer", PASSWORD))
                self.assertNotIn(PASSWORD, output.getvalue())
                self.assert_logged_out()

    def run_cli(self, args, passwords=(), usernames=()):
        output = io.StringIO()
        with patch("main.getpass.getpass", side_effect=passwords) as prompt, \
                patch("builtins.input", side_effect=usernames), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = main.main(args)
        return result, output.getvalue(), prompt

    def test_cli_signup_confirms_and_login_prompts_privately(self):
        result, output, prompt = self.run_cli(
            ["--register"], [PASSWORD, PASSWORD], ["Explorer"])
        self.assertEqual(result, 0)
        self.assertEqual([call.args[0] for call in prompt.call_args_list],
                         ["Password: ", "Confirm password: "])
        self.assertIn("Welcome, explorer!", output)
        result, login_output, prompt = self.run_cli(
            ["--login", "--username", "EXPLORER"], [PASSWORD])
        self.assertEqual(result, 0)
        prompt.assert_called_once_with("Password: ")
        for text in (output, login_output):
            for forbidden in (PASSWORD, "invoice", "Nostr", "challenge", "5000"):
                self.assertNotIn(forbidden, text)

    def test_cli_mismatch_incorrect_password_and_cancel_fail(self):
        result, output, _ = self.run_cli(
            ["--register", "--username", "explorer"], [PASSWORD, PASSWORD + "different"])
        self.assertEqual(result, 1)
        self.assertIn("Passwords do not match", output)
        self.assertIsNone(database.authenticate_player("explorer", PASSWORD))
        self.game.create_player_account("explorer", PASSWORD)
        result, output, _ = self.run_cli(
            ["--login", "--username", "explorer"], ["wrong password"])
        self.assertEqual(result, 1)
        self.assertNotIn(PASSWORD, output)
        for error in (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
            result, output, _ = self.run_cli(["--login", "--username", "explorer"], error)
            self.assertEqual(result, 1)
            self.assertIn("Secure password entry unavailable or cancelled", output)

    def test_cli_rejects_echo_fallback_before_reading_password(self):
        def unsafe_prompt(*args):
            warnings.warn("Cannot control echo", getpass.GetPassWarning)
            self.fail("Must not read echoed input")

        with patch("main.getpass.getpass", side_effect=unsafe_prompt), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(["--login", "--username", "explorer"]), 1)

    def test_cli_help_welcome_init_and_obsolete_arguments(self):
        self.assertIn("Welcome", self.run_cli([])[1])
        self.assertEqual(self.run_cli(["--init"])[0], 0)
        self.assertEqual(self.run_cli(["--init"])[0], 0)
        with self.assertRaises(SystemExit) as error:
            self.run_cli(["--help"])
        self.assertEqual(error.exception.code, 0)
        for args in (["--npub", "key"], ["--signature", "sig"], ["--challenge-id", "id"],
                     ["--begin-register"], ["--complete-register"], ["--password", PASSWORD],
                     ["--register", "--login"], ["--username", "explorer"],
                     ["--init", "--register"]):
            with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                self.run_cli(args)
            self.assertEqual(error.exception.code, 2)

    def test_direct_game_logic_execution_uses_secure_registration_and_login(self):
        for action in ("register", "login"):
            with patch("sys.argv", ["game_logic.py"]), \
                    patch("builtins.input", side_effect=[action, "explorer"]), \
                    patch("main.getpass.getpass", side_effect=[PASSWORD, PASSWORD]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(game_logic.main(), 0)


if __name__ == "__main__":
    unittest.main()
