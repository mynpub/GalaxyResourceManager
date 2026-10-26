#!/usr/bin/env python3
"""
SQLite persistence for Galaxy Resource Manager.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from authentication import hash_password, normalize_username, verify_password

DATA_DIR = Path.home() / ".galaxy_resources"


def _initialize(connection):
    if connection.execute("PRAGMA user_version").fetchone()[0] >= 4:
        return

    # Rebuilding players permits NULL legacy keys without changing child records.
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            # Another process may have finished initialization while we waited.
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version >= 4:
                return
            if version == 3:
                import economy
                economy.initialize(connection)
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise sqlite3.IntegrityError("Database contains invalid references")
                connection.execute("PRAGMA user_version = 4")
                return
            if version == 0:
                _import_legacy_data(connection)
            connection.execute("""
                CREATE TABLE players_v3 (
                    id INTEGER PRIMARY KEY,
                    npub TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    fiat_balance REAL NOT NULL DEFAULT 0,
                    bitcoin_balance REAL NOT NULL DEFAULT 0,
                    username TEXT UNIQUE COLLATE NOCASE,
                    password_hash TEXT,
                    CHECK (
                        (npub IS NOT NULL AND username IS NULL AND password_hash IS NULL)
                        OR (npub IS NULL AND username IS NOT NULL AND password_hash IS NOT NULL)
                    ),
                    CHECK (username IS NULL OR
                        (length(username) BETWEEN 3 AND 32
                         AND username NOT GLOB '*[^a-z0-9_]*'))
                )
            """)
            connection.execute("""
                INSERT INTO players_v3 (id, npub, created_at, fiat_balance, bitcoin_balance)
                SELECT id, npub, created_at, fiat_balance, bitcoin_balance FROM players
            """)
            connection.execute("DROP TABLE players")
            connection.execute("ALTER TABLE players_v3 RENAME TO players")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("Legacy data contains invalid references")
            connection.execute("PRAGMA user_version = 3")
            import economy
            economy.initialize(connection)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("Database contains invalid references")
            connection.execute("PRAGMA user_version = 4")
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _import_legacy_data(connection):
    if connection.execute("PRAGMA user_version").fetchone()[0]:
        return
    connection.execute("""
        CREATE TABLE players (
            id INTEGER PRIMARY KEY,
            npub TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            fiat_balance REAL NOT NULL DEFAULT 0,
            bitcoin_balance REAL NOT NULL DEFAULT 0
        )
    """)
    connection.execute("""
        CREATE TABLE payments (
            player_id INTEGER PRIMARY KEY REFERENCES players(id),
            paid INTEGER NOT NULL CHECK (paid IN (0, 1)),
            invoice_hash TEXT,
            timestamp TEXT NOT NULL
        )
    """)

    players_file = DATA_DIR / "players.json"
    if players_file.exists():
        with players_file.open() as f:
            for npub, player in json.load(f).items():
                connection.execute(
                    "INSERT INTO players VALUES (?, ?, ?, ?, ?)",
                    (player['id'], npub, player['created_at'],
                     player['balance']['fiat'], player['balance']['bitcoin'])
                )
    payments_file = DATA_DIR / "payments.json"
    if payments_file.exists():
        with payments_file.open() as f:
            for player_id, payment in json.load(f).items():
                connection.execute(
                    "INSERT INTO payments VALUES (?, ?, ?, ?)",
                    (int(player_id), payment['paid'], payment['invoice_hash'],
                     payment['timestamp'])
                )
    connection.execute("PRAGMA user_version = 1")


@contextmanager
def _connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATA_DIR / "galaxy_resources.db")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            _initialize(connection)
        with connection:
            yield connection
    finally:
        connection.close()


def init_database():
    """Initialize SQLite storage and import legacy JSON data once."""
    with _connection():
        pass


def create_player(username, password):
    """Create a new credentialed player; duplicates never load existing accounts."""
    username = normalize_username(username)
    password_hash = hash_password(password)
    with _connection() as connection:
        cursor = connection.execute(
            """INSERT INTO players (username, password_hash, created_at)
               VALUES (?, ?, ?)""",
            (username, password_hash, datetime.now().isoformat())
        )
        import economy
        economy.ensure_player(connection, cursor.lastrowid)
        return cursor.lastrowid


def authenticate_player(username, password):
    """Return the authenticated ID, or None; legacy accounts have no credentials."""
    username = normalize_username(username)
    with _connection() as connection:
        player = connection.execute(
            "SELECT id, password_hash FROM players WHERE username = ?", (username,)
        ).fetchone()
    valid = verify_password(password, player['password_hash'] if player else None)
    return player['id'] if player is not None and valid else None


def get_player_info(player_id):
    """Get player information using the indexed player ID."""
    with _connection() as connection:
        player = connection.execute(
            """SELECT id, npub, username, fiat_balance, bitcoin_balance
               FROM players WHERE id = ?""", (player_id,)
        ).fetchone()
        return dict(player) if player is not None else None


if __name__ == "__main__":
    init_database()