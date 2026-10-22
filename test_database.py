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

PASSWORD = "a long test passphrase"


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name) / "data"
        data_patch = patch.object(database, "DATA_DIR", self.data_dir)
        data_patch.start()
        self.addCleanup(data_patch.stop)

    def test_create_and_lookup_player_without_explicit_init(self):
        player_id = database.create_player("explorer", PASSWORD)
        self.assertEqual(database.authenticate_player("EXPLORER", PASSWORD), player_id)
        self.assertNotEqual(database.create_player("another", PASSWORD), player_id)
        self.assertEqual(database.get_player_info(player_id), {
            'id': player_id, 'npub': None, 'username': 'explorer',
            'fiat_balance': 0.0, 'bitcoin_balance': 0.0
        })
        self.assertIsNone(database.get_player_info(999))
        database.init_database()
        self.assertEqual(database.authenticate_player("explorer", PASSWORD), player_id)

    def test_concurrent_duplicate_signup_only_creates_one_player(self):
        def register(_):
            try:
                return database.create_player("Explorer", PASSWORD)
            except sqlite3.IntegrityError:
                return None

        with ThreadPoolExecutor(max_workers=4) as executor:
            ids = list(executor.map(register, range(8)))
        self.assertEqual(sum(player_id is not None for player_id in ids), 1)
        self.assertEqual(database.authenticate_player("explorer", PASSWORD),
                         next(player_id for player_id in ids if player_id is not None))

    def test_concurrent_initialization_is_idempotent(self):
        self.write_legacy_data()
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: database.init_database(), range(8)))
        self.assert_legacy_preserved()

    def test_database_enforces_unique_normalized_usernames_and_foreign_keys(self):
        database.create_player("explorer", PASSWORD)
        with self.assertRaises(sqlite3.IntegrityError):
            database.create_player("EXPLORER", PASSWORD)
        for username in ("Uppercase", "ab", "a" * 33, "with space", "étoile"):
            with self.subTest(username=username), self.assertRaises(sqlite3.IntegrityError):
                with database._connection() as connection:
                    connection.execute(
                        "INSERT INTO players (username, password_hash, created_at) VALUES (?, ?, ?)",
                        (username, "not-used", "2026-01-01"))
        with database._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO payments VALUES (999, 1, 'invalid', 'date')")

    def assert_legacy_preserved(self):
        self.assertEqual(database.get_player_info(7), {
            "id": 7, "npub": "a" * 64, "username": None,
            "fiat_balance": 12.5, "bitcoin_balance": 0.001,
        })
        with database._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(tuple(connection.execute("SELECT * FROM payments").fetchone()),
                             (7, 1, "legacy", "2026-01-02"))
            self.assertIsNone(connection.execute(
                "SELECT password_hash FROM players WHERE id = 7").fetchone()[0])
            self.assertEqual(connection.execute(
                "SELECT created_at FROM players WHERE id = 7").fetchone()[0],
                "2026-01-01T00:00:00")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def create_earlier_sqlite(self, version):
        self.data_dir.mkdir()
        with sqlite3.connect(self.data_dir / "galaxy_resources.db") as connection:
            connection.executescript("""
                CREATE TABLE players (
                    id INTEGER PRIMARY KEY, npub TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, fiat_balance REAL NOT NULL DEFAULT 0,
                    bitcoin_balance REAL NOT NULL DEFAULT 0);
                CREATE TABLE payments (
                    player_id INTEGER PRIMARY KEY REFERENCES players(id),
                    paid INTEGER NOT NULL CHECK (paid IN (0, 1)),
                    invoice_hash TEXT, timestamp TEXT NOT NULL);
            """)
            connection.execute("INSERT INTO players VALUES (?, ?, ?, ?, ?)",
                               (7, "a" * 64, "2026-01-01T00:00:00", 12.5, 0.001))
            connection.execute("INSERT INTO payments VALUES (7, 1, 'legacy', '2026-01-02')")
            if version == 2:
                connection.executescript("""
                    CREATE TABLE verification_challenges (
                        id TEXT PRIMARY KEY, pubkey TEXT NOT NULL, challenge TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('issued', 'used', 'expired')),
                        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
                    INSERT INTO verification_challenges VALUES
                        ('old', 'key', 'proof', 'issued', 100, 700);
                """)
            connection.execute(f"PRAGMA user_version = {version}")

    def check_sqlite_upgrade(self, version):
        self.create_earlier_sqlite(version)
        database.init_database()
        database.init_database()
        self.assert_legacy_preserved()
        if version == 2:
            with database._connection() as connection:
                self.assertEqual(tuple(connection.execute(
                    "SELECT * FROM verification_challenges").fetchone()),
                    ("old", "key", "proof", "issued", 100, 700))
        self.assertIsNone(database.authenticate_player("aaa", PASSWORD))
        with self.assertRaises(ValueError):
            database.authenticate_player("a" * 64, PASSWORD)
        new_id = database.create_player("aaa", PASSWORD)
        self.assertGreater(new_id, 7)
        self.assertEqual(database.authenticate_player("aaa", PASSWORD), new_id)
        self.assert_legacy_preserved()

    def test_version_one_upgrade_preserves_data_and_prevents_claims(self):
        self.check_sqlite_upgrade(1)

    def test_version_two_upgrade_preserves_data_and_prevents_claims(self):
        self.check_sqlite_upgrade(2)

    def test_sqlite_rebuild_failure_rolls_back_schema_and_data(self):
        self.create_earlier_sqlite(2)
        with sqlite3.connect(self.data_dir / "galaxy_resources.db") as connection:
            connection.execute("INSERT INTO payments VALUES (999, 0, 'orphan', 'date')")
        with self.assertRaises(sqlite3.IntegrityError):
            database.init_database()
        with sqlite3.connect(self.data_dir / "galaxy_resources.db") as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertNotIn("username", [row[1] for row in connection.execute("PRAGMA table_info(players)")])
            self.assertEqual(connection.execute("SELECT id FROM players").fetchall(), [(7,)])
            self.assertEqual(connection.execute("SELECT count(*) FROM payments").fetchone()[0], 2)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'players_v3'").fetchone()[0], 0)
            connection.execute("DELETE FROM payments WHERE player_id = 999")
        database.init_database()
        self.assert_legacy_preserved()

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
        self.assert_legacy_preserved()
        (self.data_dir / "players.json").write_text("invalid JSON")
        database.init_database()
        self.assertGreater(database.create_player("explorer", PASSWORD), 7)
        self.assert_legacy_preserved()

    def test_failed_migration_rolls_back_and_can_be_retried(self):
        self.write_legacy_data()
        payments = self.data_dir / "payments.json"
        original = payments.read_text()
        payments.write_text("invalid JSON")
        with self.assertRaises(json.JSONDecodeError):
            database.init_database()
        payments.write_text(original)
        database.init_database()
        self.assert_legacy_preserved()


if __name__ == "__main__":
    unittest.main()
