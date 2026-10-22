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
    if connection.execute("PRAGMA user_version").fetchone()[0]:
        return

    # Serialize first-time setup so concurrent processes import legacy data once.
    connection.execute("BEGIN IMMEDIATE")
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
        player = connection.execute(
            "SELECT id FROM players WHERE npub = ?", (npub,)
        ).fetchone()
        if player is not None:
            return player['id']
        connection.execute(
            """INSERT INTO players (npub, created_at) VALUES (?, ?)
               ON CONFLICT(npub) DO NOTHING""",
            (npub, datetime.now().isoformat())
        )
        return connection.execute(
            "SELECT id FROM players WHERE npub = ?", (npub,)
        ).fetchone()['id']


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