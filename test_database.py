#!/usr/bin/env python3
"""Isolated tests for SQLite persistence and legacy data migration."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import database
from game_logic import GameLogic
from nostr.key import PrivateKey


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name) / "data"
        data_patch = patch.object(database, "DATA_DIR", self.data_dir)
        data_patch.start()
        self.addCleanup(data_patch.stop)

    def test_create_and_lookup_player_without_explicit_init(self):
        npub = "a" * 64
        player_id = database.get_or_create_player(npub)
        self.assertEqual(database.get_or_create_player(npub), player_id)
        self.assertNotEqual(database.get_or_create_player("b" * 64), player_id)
        self.assertEqual(database.get_player_info(player_id), {
            'id': player_id, 'npub': npub,
            'fiat_balance': 0.0, 'bitcoin_balance': 0.0
        })
        self.assertIsNone(database.get_player_info(999))
        database.init_database()
        self.assertEqual(database.get_or_create_player(npub), player_id)

    def test_payment_persists_and_can_be_updated(self):
        player_id = database.get_or_create_player("a" * 64)
        self.assertIs(database.check_payment_status(player_id), False)
        self.assertIs(database.check_payment_status(999), False)
        database.mark_payment_paid(player_id, "first")
        self.assertIs(database.check_payment_status(player_id), True)
        database.mark_payment_paid(player_id, "second")
        with sqlite3.connect(self.data_dir / "galaxy_resources.db") as connection:
            self.assertEqual(connection.execute(
                "SELECT invoice_hash FROM payments WHERE player_id = ?", (player_id,)
            ).fetchall(), [("second",)])
        with self.assertRaises(sqlite3.IntegrityError):
            database.mark_payment_paid(999, "invalid")
        self.assertIs(database.check_payment_status(player_id), True)

    def test_concurrent_creation_returns_same_player(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            ids = list(executor.map(database.get_or_create_player, ["a" * 64] * 8))
        self.assertEqual(len(set(ids)), 1)

    def test_missing_player_cannot_credit_another_account(self):
        player_id = database.get_or_create_player("a" * 64)
        with self.assertRaises(ValueError):
            database.mark_payment_paid(None, "invalid")
        self.assertFalse(GameLogic().complete_registration("invalid"))
        self.assertIs(database.check_payment_status(player_id), False)

    def issue_challenge(self):
        challenge = {
            "id": "test-challenge", "pubkey": "a" * 64,
            "challenge": "test challenge", "created_at": 100, "expires_at": 700
        }
        database.create_verification_challenge(challenge)
        return challenge

    def test_challenge_persists_and_expires(self):
        challenge = self.issue_challenge()
        stored = database.get_verification_challenge(challenge['id'], 100)
        self.assertEqual(stored, dict(challenge, status="issued"))
        self.assertEqual(database.get_verification_challenge(
            challenge['id'], 700)['status'], "expired")
        self.assertIsNone(database.consume_challenge_and_create_player(
            challenge['id'], challenge['pubkey']))
        self.assertIsNone(database.get_verification_challenge("missing", 100))
        self.assertIsNone(database.get_player_info(1))

    def test_only_one_concurrent_challenge_redemption_succeeds(self):
        challenge = self.issue_challenge()
        with patch("database.time.time", return_value=101), \
                ThreadPoolExecutor(max_workers=4) as executor:
            ids = list(executor.map(
                lambda _: database.consume_challenge_and_create_player(
                    challenge['id'], challenge['pubkey']), range(8)
            ))
        self.assertEqual(sum(player_id is not None for player_id in ids), 1)
        self.assertEqual(database.get_verification_challenge(
            challenge['id'], 101)['status'], "used")

    def test_challenge_upgrade_preserves_existing_sqlite_data(self):
        player_id = database.get_or_create_player("a" * 64)
        with sqlite3.connect(self.data_dir / "galaxy_resources.db") as connection:
            connection.execute("DROP TABLE verification_challenges")
            connection.execute("PRAGMA user_version = 1")
        self.issue_challenge()
        self.assertEqual(database.get_player_info(player_id)['npub'], "a" * 64)

    def write_legacy_data(self):
        self.data_dir.mkdir()
        (self.data_dir / "players.json").write_text(json.dumps({
            "a" * 64: {
                "id": 7, "created_at": "2026-01-01T00:00:00",
                "balance": {"fiat": 12.5, "bitcoin": 0.001}
            }
        }))
        (self.data_dir / "payments.json").write_text(json.dumps({
            "7": {"paid": True, "invoice_hash": "legacy", "timestamp": "2026-01-02"}
        }))

    def test_legacy_data_is_imported_only_once(self):
        self.write_legacy_data()
        database.init_database()
        self.assertEqual(database.get_or_create_player("a" * 64), 7)
        self.assertEqual(database.get_player_info(7)['fiat_balance'], 12.5)
        self.assertEqual(database.get_player_info(7)['bitcoin_balance'], 0.001)
        self.assertIs(database.check_payment_status(7), True)
        (self.data_dir / "players.json").write_text("invalid JSON")
        database.init_database()
        self.assertGreater(database.get_or_create_player("b" * 64), 7)

    def test_failed_migration_rolls_back_and_can_be_retried(self):
        self.write_legacy_data()
        payments = self.data_dir / "payments.json"
        original = payments.read_text()
        payments.write_text("invalid JSON")
        with self.assertRaises(json.JSONDecodeError):
            database.init_database()
        payments.write_text(original)
        database.init_database()
        self.assertEqual(database.get_or_create_player("a" * 64), 7)
        self.assertIs(database.check_payment_status(7), True)

    def test_game_registration_flow(self):
        game = GameLogic()
        key = PrivateKey()
        challenge = game.begin_registration(key.public_key.hex())
        signature = key.sign_message_hash(bytes.fromhex(challenge['signing_hash']))
        self.assertTrue(game.create_player_account(
            key.public_key.hex(), signature, challenge['id']))
        self.assertFalse(game.check_payment())
        self.assertTrue(game.complete_registration("test_invoice"))
        self.assertTrue(game.check_payment())
        self.assertEqual(game.get_player_info()['npub'], key.public_key.hex())


if __name__ == "__main__":
    unittest.main()
