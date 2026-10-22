#!/usr/bin/env python3
"""
Galaxy Resource Manager - Main Entry Point

A terminal-based MMORPG using SQLite, Nostr npubs, and Lightning payments
"""

import argparse
import sys
import json
from game_logic import GameLogic
from database import init_database

def setup_parser():
    """Setup command line argument parser"""
    parser = argparse.ArgumentParser(description='Galaxy Resource Manager - MMORPG')
    parser.add_argument('--init', action='store_true', help='Initialize the database')
    registration = parser.add_mutually_exclusive_group()
    registration.add_argument('--register', '--begin-register', dest='begin_register',
                              action='store_true', help='Begin ownership verification')
    registration.add_argument('--complete-register', action='store_true',
                              help='Verify ownership and create an account/invoice')
    parser.add_argument('--npub', help='Nostr public key for registration')
    parser.add_argument('--signature', help='BIP-340 Schnorr signature (128 hex characters)')
    parser.add_argument('--challenge-id', help='Registration challenge ID')
    return parser

def main():
    """Main entry point for the game"""
    print("Galaxy Resource Manager")
    print("=" * 40)

    # Parse command line arguments
    parser = setup_parser()
    args = parser.parse_args()

    # Check if initialization is requested
    if args.init:
        print("Initializing database...")
        init_database()
        print("Database initialized successfully!")
        return

    # Initialize the game logic
    game = GameLogic()
    
    if args.begin_register or args.complete_register:
        # Handle registration with npub argument
        if not args.npub:
            print("Nostr public key required for registration")
            parser.print_help()
            sys.exit(1)
        
        if args.begin_register:
            try:
                challenge = game.begin_registration(args.npub)
            except ValueError as e:
                parser.exit(1, f"{e}\n")
            print("Sign SHA-256 of the exact challenge text using BIP-340 Schnorr.")
            print("Challenge (JSON; decode escapes before signing):")
            print(json.dumps(challenge, indent=2))
            print("Then run --complete-register with --npub, --challenge-id and --signature.")
            return

        if not args.signature or not args.challenge_id:
            parser.error("--complete-register requires --signature and --challenge-id")
        if not game.create_player_account(args.npub, args.signature, args.challenge_id):
            print("Account creation failed!")
            sys.exit(1)
        
        # Generate invoice for payment
        print("\nGenerating payment invoice...")
        invoice = game.generate_lightning_invoice(5000)
        
        print(f"\nPlease pay {invoice['amount']} sats to the following Lightning invoice:")
        print(invoice['invoice'])
        print("\nPayment processing is simulated; no payment confirmation CLI is available.")
        return
        
    else:
        # Default interactive mode - display welcome and instructions
        print("Welcome to Galaxy Resource Manager!")
        print("This is a terminal-based MMORPG where you manage resources in space.")
        print()
        print("To create an account:")
        print("1. Have your Nostr public key (npub) ready")
        print("2. Run: python main.py --begin-register --npub <your_npub>")
        print("3. Externally sign the challenge's SHA-256 digest using BIP-340 Schnorr")
        print("4. Run: python main.py --complete-register --npub <your_npub> "
              "--challenge-id <id> --signature <hex_signature>")
        print("5. Pay 5000 sats to the Lightning invoice generated (simulated)")

if __name__ == "__main__":
    main()