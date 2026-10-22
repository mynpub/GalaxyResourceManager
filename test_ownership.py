"""Isolated cryptographic ownership and registration CLI tests."""

import contextlib
import io
import json
import os
from pathlib import Path
import site
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from nostr import bech32
from nostr.key import PrivateKey

import database
from game_logic import GameLogic
import main
from ownership import normalize_pubkey


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.data_dir = self.home / ".galaxy_resources"
        data_patch = patch.object(database, "DATA_DIR", self.data_dir)
        data_patch.start()
        self.addCleanup(data_patch.stop)
        self.key = PrivateKey()
        self.pubkey = self.key.public_key.hex()
        self.npub = self.key.public_key.bech32()
        self.game = GameLogic()

    def issue(self):
        return self.game.begin_registration(self.npub)

    def sign(self, challenge, key=None):
        return (key or self.key).sign_message_hash(bytes.fromhex(challenge['signing_hash']))

    def assert_no_players(self):
        with database._connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM players").fetchone()[0], 0)

    def run_cli(self, *args):
        output = io.StringIO()
        with patch.object(sys, "argv", ["main.py", *args]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            main.main()
        return output.getvalue()

    def test_valid_proof_persists_and_normalizes_across_instances(self):
        challenge = self.issue()
        self.assertEqual(challenge['status'], "issued")
        self.assertEqual(challenge['pubkey'], self.pubkey)
        self.assertEqual(len(challenge['id']), 64)
        self.assertEqual(challenge['expires_at'] - challenge['created_at'], 600)
        self.assert_no_players()
        game = GameLogic()
        self.assertTrue(game.create_player_account(
            self.pubkey.upper(), self.sign(challenge), challenge['id']))
        player_id = game.player_id
        self.assertEqual(game.get_player_info()['npub'], self.pubkey)
        self.assertEqual(game.generate_lightning_invoice()['amount'], 5000)
        second = self.issue()
        self.assertNotEqual(second['id'], challenge['id'])
        self.assertTrue(self.game.create_player_account(
            self.npub, self.sign(second), second['id']))
        self.assertEqual(self.game.player_id, player_id)

    def test_invalid_signature_does_not_create_account_or_consume_challenge(self):
        challenge = self.issue()
        for signature in (self.sign(challenge, PrivateKey()), "0" * 128,
                          "", "ff", "g" * 128, " " * 128, None, 123):
            with self.subTest(signature=signature):
                self.assertFalse(self.game.create_player_account(
                    self.npub, signature, challenge['id']))
        self.assert_no_players()
        stored = database.get_verification_challenge(challenge['id'], challenge['created_at'])
        self.assertEqual(stored['status'], "issued")
        self.assertTrue(self.game.create_player_account(
            self.npub, self.sign(challenge), challenge['id']))

    def test_signature_is_bound_to_exact_challenge_and_public_key(self):
        challenge = self.issue()
        other = self.issue()
        self.assertFalse(self.game.create_player_account(
            self.npub, self.sign(challenge), other['id']))
        self.assertFalse(self.game.create_player_account(
            PrivateKey().public_key.hex(), self.sign(challenge), challenge['id']))
        altered = dict(challenge, signing_hash="00" * 32)
        self.assertFalse(self.game.create_player_account(
            self.npub, self.sign(altered), challenge['id']))
        self.assert_no_players()

    def test_expired_challenge_fails_at_exact_deadline(self):
        with patch("game_logic.time.time", return_value=1000):
            challenge = self.issue()
        with patch("game_logic.time.time", return_value=challenge['expires_at']):
            self.assertFalse(self.game.create_player_account(
                self.npub, self.sign(challenge), challenge['id']))
        self.assertEqual(database.get_verification_challenge(
            challenge['id'], challenge['expires_at'])['status'], "expired")
        self.assert_no_players()

    def test_expiry_is_rechecked_after_signature_verification(self):
        challenge = self.issue()
        with patch("game_logic.time.time", side_effect=[
                challenge['expires_at'] - 1, challenge['expires_at']]):
            self.assertFalse(self.game.create_player_account(
                self.npub, self.sign(challenge), challenge['id']))
        self.assert_no_players()

    def test_expiry_is_checked_after_acquiring_sqlite_write_lock(self):
        challenge = self.issue()
        original_connection = database._connection

        @contextlib.contextmanager
        def delayed_write_lock():
            with original_connection() as connection:
                def trace(statement):
                    if statement == "BEGIN IMMEDIATE":
                        # Simulate the write lock becoming available at expiry.
                        clock.return_value = challenge['expires_at']
                connection.set_trace_callback(trace)
                yield connection

        with patch("database.time.time", return_value=challenge['expires_at'] - 1) as clock, \
                patch.object(database, "_connection", delayed_write_lock):
            self.assertFalse(self.game.create_player_account(
                self.npub, self.sign(challenge), challenge['id']))
        self.assert_no_players()

    def test_replay_fails_even_from_another_instance(self):
        challenge = self.issue()
        signature = self.sign(challenge)
        self.assertTrue(self.game.create_player_account(self.npub, signature, challenge['id']))
        self.assertFalse(GameLogic().create_player_account(self.npub, signature, challenge['id']))
        self.assertFalse(self.game.create_player_account(self.npub, signature, challenge['id']))
        self.assertIsNone(self.game.player_id)
        with self.assertRaises(ValueError):
            self.game.generate_lightning_invoice()
        self.assertEqual(database.get_verification_challenge(
            challenge['id'], challenge['expires_at'] + 1)['status'], "used")

    def test_missing_challenge_or_proof_cannot_create_account(self):
        self.assertFalse(self.game.create_player_account(self.pubkey))
        self.assertFalse(self.game.create_player_account(self.pubkey, "0" * 128, "missing"))
        self.assertFalse(self.game.complete_registration("unverified"))
        with self.assertRaises(ValueError):
            self.game.generate_lightning_invoice()
        self.assert_no_players()

    def test_hex_and_npub_normalization(self):
        for value in (self.pubkey, self.pubkey.upper(), self.npub,
                      self.npub.upper(), f" {self.npub}\n"):
            with self.subTest(value=value):
                self.assertEqual(normalize_pubkey(value), self.pubkey)
                self.assertTrue(self.game.validate_npub(value))

    def test_invalid_key_formats(self):
        data = bech32.convertbits(bytes.fromhex(self.pubkey), 8, 5)
        invalid = [
            None, 123, "", "a" * 63, "g" * 64, "+" + "a" * 63,
            "0x" + "a" * 62, "f" * 64, "aa " * 32,
            self.npub[:-1] + ("q" if self.npub[-1] != "q" else "p"),
            "NPUB" + self.npub[4:],
            bech32.bech32_encode("nsec", data, bech32.Encoding.BECH32),
            bech32.bech32_encode("npub", data, bech32.Encoding.BECH32M),
            bech32.bech32_encode("npub", data[:-1], bech32.Encoding.BECH32),
            bech32.bech32_encode("npub", data[:-1] + [data[-1] | 1],
                                 bech32.Encoding.BECH32),
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertFalse(self.game.validate_npub(value))
                with self.assertRaises(ValueError):
                    self.game.begin_registration(value)

    def test_existing_uppercase_hex_account_is_reused(self):
        player_id = database.get_or_create_player(self.pubkey.upper())
        database.mark_payment_paid(player_id, "existing-payment")
        challenge = self.issue()
        self.assertTrue(self.game.create_player_account(
            self.npub, self.sign(challenge), challenge['id']))
        self.assertEqual(self.game.player_id, player_id)
        self.assertEqual(self.game.get_player_info()['npub'], self.pubkey)
        self.assertTrue(self.game.check_payment())

    def test_cli_alias_only_issues_challenge_and_errors_block_invoice(self):
        output = self.run_cli("--register", "--npub", self.npub)
        self.assertIn('"status": "issued"', output)
        self.assertNotIn("lnbc", output)
        self.assert_no_players()
        for args in [
            ["--complete-register", "--npub", self.npub],
            ["--complete-register", "--npub", self.npub, "--challenge-id", "missing",
             "--signature", "0" * 128],
            ["--begin-register", "--npub", "invalid"],
            ["--begin-register"],
        ]:
            with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                self.run_cli(*args)
            self.assertNotEqual(error.exception.code, 0)
        self.assert_no_players()
        self.assertIn("Welcome", self.run_cli())

    def test_cli_begin_complete_and_replay_across_processes(self):
        def run(*args):
            return subprocess.run(
                [sys.executable, str(Path(main.__file__).resolve()), *args],
                env=dict(os.environ, HOME=str(self.home), PYTHONUSERBASE=site.getuserbase()),
                capture_output=True, text=True, check=False)

        begun = run("--begin-register", "--npub", self.npub)
        self.assertEqual(begun.returncode, 0, begun.stderr)
        challenge, _ = json.JSONDecoder().raw_decode(begun.stdout[begun.stdout.index("{"):])
        args = ("--complete-register", "--npub", self.pubkey.upper(),
                "--challenge-id", challenge['id'], "--signature", self.sign(challenge))
        completed = run(*args)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Account created successfully", completed.stdout)
        self.assertIn("lnbc", completed.stdout)
        replay = run(*args)
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("used", replay.stdout)
        self.assertNotIn("lnbc", replay.stdout)
        with sqlite3.connect(self.data_dir / "galaxy_resources.db") as connection:
            self.assertEqual(connection.execute("SELECT npub FROM players").fetchall(),
                             [(self.pubkey,)])


if __name__ == "__main__":
    unittest.main()
