#!/usr/bin/env python3
"""Isolated end-to-end account flow, also runnable directly."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
from game_logic import GameLogic


class GameFlowTests(unittest.TestCase):
    def test_player_creation_and_returning_login_preserve_balances(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(database, "DATA_DIR", Path(temporary)):
            database.init_database()
            game = GameLogic()
            self.assertTrue(game.create_player_account("explorer", "a long test passphrase"))
            player_id = game.player_id
            self.assertIsNotNone(game.get_player_info())
            with database._connection() as connection:
                connection.execute(
                    "UPDATE players SET fiat_balance = ?, bitcoin_balance = ? WHERE id = ?",
                    (12.5, 0.001, player_id))
            returning = GameLogic()
            self.assertTrue(returning.login("EXPLORER", "a long test passphrase"))
            self.assertEqual(returning.player_id, player_id)
            self.assertEqual(returning.get_player_info()['fiat_balance'], 12.5)
            self.assertEqual(returning.get_player_info()['bitcoin_balance'], 0.001)
            self.assertFalse(returning.login("explorer", "incorrect password"))
            self.assertIsNone(returning.get_player_info())


if __name__ == "__main__":
    unittest.main()