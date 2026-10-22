#!/usr/bin/env python3
"""
Galaxy Resource Manager - Main Entry Point

A terminal-based MMORPG with local username/password accounts and free access.
"""

import argparse
import getpass
import logging
import warnings
from game_logic import GameLogic
from database import init_database

def setup_parser():
    """Setup command line argument parser"""
    parser = argparse.ArgumentParser(description='Galaxy Resource Manager - MMORPG')
    parser.add_argument('--init', action='store_true', help='Initialize the database')
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument('--register', action='store_true',
                                help='Create a free account (password prompted securely)')
    authentication.add_argument('--login', action='store_true',
                                help='Log in (password prompted securely)')
    authentication.add_argument('--serve-tcp', action='store_true',
                                help='Host plaintext TCP terminal sessions')
    parser.add_argument('--host', default='0.0.0.0', help='TCP bind address (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=7777, help='TCP port (default: 7777)')
    parser.add_argument('--username', help='Username; prompted if omitted (3-32 letters/digits/_)')
    return parser

def main(argv=None):
    """Main entry point for the game"""
    print("Galaxy Resource Manager")
    print("=" * 40)

    # Parse command line arguments
    parser = setup_parser()
    args = parser.parse_args(argv)
    if args.init and (args.register or args.login or args.serve_tcp or args.username is not None):
        parser.error("--init cannot be combined with account options")
    if args.username is not None and not (args.register or args.login):
        parser.error("--username requires --register or --login")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    if args.serve_tcp:
        from tcp_server import serve_tcp
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        try:
            serve_tcp(args.host, args.port)
        except OSError as error:
            parser.exit(1, f"Unable to start TCP server: {error}\n")
        return 0

    # Check if initialization is requested
    if args.init:
        print("Initializing database...")
        init_database()
        print("Database initialized successfully!")
        return 0

    # Initialize the game logic
    game = GameLogic()
    
    if args.register or args.login:
        try:
            username = args.username if args.username is not None else input("Username: ")
            # Fail closed rather than falling back to an echoed password on non-TTY input.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("Password: ")
                if args.register and password != getpass.getpass("Confirm password: "):
                    print("Passwords do not match.")
                    return 1
        except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
            print("\nSecure password entry unavailable or cancelled; use an interactive terminal.")
            return 1
        success = (game.create_player_account(username, password) if args.register
                   else game.login(username, password))
        if not success:
            return 1
        player = game.get_player_info()
        print(f"Welcome, {player['username']}! Your account is ready to play.")
        print(f"Balance: {player['fiat_balance']} fiat, {player['bitcoin_balance']} bitcoin")
        return 0
    else:
        # Default interactive mode - display welcome and instructions
        print("Welcome to Galaxy Resource Manager!")
        print("This is a terminal-based MMORPG where you manage resources in space.")
        print()
        print("To create an account:")
        print("  python main.py --register --username <username>")
        print("To log in:")
        print("  python main.py --login --username <username>")
        print("Passwords are entered privately; registration asks for confirmation.")
        print("Access is free and immediate. Use --help for all options.")
        return 0

if __name__ == "__main__":
    raise SystemExit(main())