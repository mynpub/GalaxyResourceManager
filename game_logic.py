#!/usr/bin/env python3
"""Local player registration and authentication for Galaxy Resource Manager."""

import sqlite3

from authentication import normalize_username
from database import authenticate_player, create_player, get_player_info


class GameLogic:
    def __init__(self, output=None):
        """Start with no authenticated player."""
        self.player_id = None
        self.username = None
        self.output = output if output is not None else lambda message: print(message)

    def _clear_authentication(self):
        self.player_id = None
        self.username = None

    def create_player_account(self, username: str, password: str) -> bool:
        """Register and authenticate a new player with immediate free access."""
        self._clear_authentication()
        try:
            username = normalize_username(username)
            player_id = create_player(username, password)
            self.player_id = player_id
            self.username = username
            self.output("Account created successfully!")
            return True
        except ValueError as error:
            self.output(str(error))
            return False
        except sqlite3.Error:
            self.output("Unable to create account. Username may be unavailable or storage unavailable.")
            return False

    def login(self, username: str, password: str) -> bool:
        """Authenticate an account; any failure clears the previous player."""
        self._clear_authentication()
        try:
            username = normalize_username(username)
            player_id = authenticate_player(username, password)
            if player_id is not None:
                self.player_id = player_id
                self.username = username
                self.output("Login successful!")
                return True
        except (ValueError, sqlite3.Error):
            pass
        self.output("Unable to log in. Invalid username or password, or storage unavailable.")
        return False

    def get_player_info(self):
        """Get complete player information"""
        if self.player_id is None:
            return None
        return get_player_info(self.player_id)

def main():
    """Offer the same registration/login CLI, prompting for an action by default."""
    import sys
    from main import main as cli_main

    if len(sys.argv) == 1:
        try:
            action = input("Register or login? ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 1
        if action not in ("register", "login"):
            print("Choose register or login.")
            return 1
        return cli_main([f"--{action}"])
    return cli_main()

if __name__ == "__main__":
    raise SystemExit(main())