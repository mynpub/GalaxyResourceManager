#!/usr/bin/env python3
"""
SQLite persistence for Galaxy Resource Manager.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DATA_DIR = Path.home() / ".galaxy_resources"


def _initialize(connection):
    if connection.execute("PRAGMA user_version").fetchone()[0] >= 2:
        return

    # Serialize first-time setup so concurrent processes import legacy data once.
    connection.execute("BEGIN IMMEDIATE")
    if not connection.execute("PRAGMA user_version").fetchone()[0]:
        _import_legacy_data(connection)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS verification_challenges (
            id TEXT PRIMARY KEY,
            pubkey TEXT NOT NULL,
            challenge TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('issued', 'used', 'expired')),
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
    """)
    connection.execute("PRAGMA user_version = 2")


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


def get_or_create_player(npub):
    """Get an existing player ID or atomically create a player."""
    with _connection() as connection:
        return _get_or_create_player(connection, npub)


def _get_or_create_player(connection, npub):
    player = connection.execute(
        "SELECT id FROM players WHERE npub = ?", (npub,)
    ).fetchone()
    if player is None:
        # Older registrations accepted uppercase hex without normalization.
        player = connection.execute(
            "SELECT id FROM players WHERE lower(npub) = ? LIMIT 1", (npub,)
        ).fetchone()
    if player is not None:
        connection.execute("UPDATE players SET npub = ? WHERE id = ?",
                           (npub, player['id']))
        return player['id']
    connection.execute(
        """INSERT INTO players (npub, created_at) VALUES (?, ?)
           ON CONFLICT(npub) DO NOTHING""",
        (npub, datetime.now().isoformat())
    )
    return connection.execute(
        "SELECT id FROM players WHERE npub = ?", (npub,)
    ).fetchone()['id']


def create_verification_challenge(challenge):
    """Persist an issued ownership challenge, with UTC Unix timestamps."""
    with _connection() as connection:
        connection.execute(
            """INSERT INTO verification_challenges
               (id, pubkey, challenge, status, created_at, expires_at)
               VALUES (:id, :pubkey, :challenge, 'issued', :created_at, :expires_at)""",
            challenge
        )


def get_verification_challenge(challenge_id, now):
    """Fetch a challenge and persist its expired state when necessary."""
    with _connection() as connection:
        connection.execute(
            """UPDATE verification_challenges SET status = 'expired'
               WHERE id = ? AND status = 'issued' AND expires_at <= ?""",
            (challenge_id, now)
        )
        challenge = connection.execute(
            "SELECT * FROM verification_challenges WHERE id = ?", (challenge_id,)
        ).fetchone()
        return dict(challenge) if challenge is not None else None


def consume_challenge_and_create_player(challenge_id, pubkey, now):
    """Atomically redeem a verified challenge and create/load its player.

    Only the caller verifying the signature should invoke this storage helper.
    Conditional updates prevent concurrent registration attempts from replaying it.
    """
    with _connection() as connection:
        updated = connection.execute(
            """UPDATE verification_challenges SET status = 'used'
               WHERE id = ? AND pubkey = ? AND status = 'issued'
                   AND expires_at > ?""",
            (challenge_id, pubkey, now)
        )
        if updated.rowcount != 1:
            connection.execute(
                """UPDATE verification_challenges SET status = 'expired'
                   WHERE id = ? AND status = 'issued' AND expires_at <= ?""",
                (challenge_id, now)
            )
            return None
        return _get_or_create_player(connection, pubkey)


def get_player_info(player_id):
    """Get player information using the indexed player ID."""
    with _connection() as connection:
        player = connection.execute(
            """SELECT id, npub, fiat_balance, bitcoin_balance
               FROM players WHERE id = ?""", (player_id,)
        ).fetchone()
        return dict(player) if player is not None else None


def mark_payment_paid(player_id, invoice_hash):
    """Atomically record or update a player's simulated payment."""
    if player_id is None:
        raise ValueError("A player ID is required to record payment")
    with _connection() as connection:
        connection.execute(
            """INSERT INTO payments (player_id, paid, invoice_hash, timestamp)
               VALUES (?, 1, ?, ?)
               ON CONFLICT(player_id) DO UPDATE SET
                   paid = excluded.paid,
                   invoice_hash = excluded.invoice_hash,
                   timestamp = excluded.timestamp""",
            (player_id, invoice_hash, datetime.now().isoformat())
        )


def check_payment_status(player_id):
    """Check whether a player's simulated payment has been recorded."""
    with _connection() as connection:
        payment = connection.execute(
            "SELECT paid FROM payments WHERE player_id = ?", (player_id,)
        ).fetchone()
        return bool(payment['paid']) if payment is not None else False


if __name__ == "__main__":
    init_database()