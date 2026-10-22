#!/usr/bin/env python3
"""
Galaxy Resource Manager - Main Entry Point

A terminal-based MMORPG using SQLite, Nostr npubs, and Lightning payments
"""

import argparse
import sys
import os
from game_logic import GameLogic
from database import init_database

def setup_parser():
    """Setup command line argument parser"""
    parser = argparse.ArgumentParser(description='Galaxy Resource Manager - MMORPG')
    parser.add_argument('--init', action='store_true', help='Initialize the database')
    parser.add_argument('--register', action='store_true', help='Register new player account')
    parser.add_argument('--npub', help='Nostr public key for registration')
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
    
    if args.register:
        # Handle registration with npub argument
        if not args.npub:
            print("Nostr public key required for registration")
            parser.print_help()
            sys.exit(1)
        
        # Create account
        if not game.create_player_account(args.npub):
            print("Account creation failed!")
            return
        
        # Generate invoice for payment
        print("\nGenerating payment invoice...")
        invoice = game.generate_lightning_invoice(5000)
        
        print(f"\nPlease pay {invoice['amount']} sats to the following Lightning invoice:")
        print(invoice['invoice'])
        print("\nAfter payment is confirmed, please run the command again with '--confirm'")
        return
        
    else:
        # Default interactive mode - display welcome and instructions
        print("Welcome to Galaxy Resource Manager!")
        print("This is a terminal-based MMORPG where you manage resources in space.")
        print()
        print("To create an account:")
        print("1. Have your Nostr public key (npub) ready")
        print("2. Run: python main.py --register --npub <your_npub>")
        print("3. Pay 5000 sats to the Lightning invoice generated")
        print("4. Confirm payment and complete registration")

if __name__ == "__main__":
    main()