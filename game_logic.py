#!/usr/bin/env python3
"""
Core game logic for Galaxy Resource Manager - handles player registration and payment validation
"""

import os
import sys
from database import get_or_create_player, get_player_info, mark_payment_paid, check_payment_status
from nostr.client import Client
from nostr.event import Event
from nostr.key import PrivateKey
import time

class GameLogic:
    def __init__(self):
        """Initialize game logic with database connection"""
        self.player_id = None
        self.npub = None
        
    def validate_npub(self, npub: str) -> bool:
        """
        Validate that the provided npub is a legitimate Nostr public key
        (64-character hex string)
        """
        if not npub:
            return False
            
        # Nostr public keys are 64-character hex strings
        if len(npub) != 64:
            return False
            
        try:
            int(npub, 16)  # Try to convert to integer - will raise exception if not valid hex
            return True
        except ValueError:
            return False
    
    def create_player_account(self, npub: str) -> bool:
        """
        Create a player account with the given npub
        Returns True if successful, False otherwise
        """
        if not self.validate_npub(npub):
            print("Invalid Nostr public key format!")
            return False
            
        try:
            print(f"Creating account for npub: {npub}")
            self.player_id = get_or_create_player(npub)
            self.npub = npub
            print("Account created successfully!")
            return True
        except Exception as e:
            print(f"Failed to create account: {e}")
            return False
    
    def generate_lightning_invoice(self, amount_sats: int = 5000) -> dict:
        """
        Generate a Lightning Network invoice for the specified amount (in sats)
        This function would interface with your Core Lightning node
        """
        print(f"Generating Lightning invoice for {amount_sats} sats...")
        
        # Since we don't have an actual Core Lightning node connected, 
        # we'll simulate this by returning a fake invoice structure
        invoice = {
            "invoice": "lnbc1234567890abcdefghijklmnopqrstuvwxyz1234567890abcdefghijklmnopqrstuvq",
            "amount": amount_sats,
            "status": "pending"
        }
        
        # In a real setup, you would connect to your Core Lightning node:
        # import lightning
        # ln_client = lightning.LightningClient("unix:/path/to/lightning/socket")
        # response = ln_client.invoice(amount_msat=amount_sats * 1000, description="Galaxy Resource Manager Account Creation")
        # return response
        
        print(f"Lightning invoice generated: {invoice['invoice']}")
        return invoice
    
    def check_payment(self) -> bool:
        """
        Check if the player has made a payment (simulate payment checking)
        In practice, this would be connected to your Lightning node's webhook notifications
        """
        # In a real implementation, you'd:
        # 1. Monitor the invoice hash
        # 2. Check with your Lightning node for payment status
        # 3. Update the database when paid
        
        print("Checking if payment has been received...")
        return check_payment_status(self.player_id)
    
    def complete_registration(self, invoice_hash: str) -> bool:
        """
        Complete the registration process after payment is confirmed
        """
        try:
            mark_payment_paid(self.player_id, invoice_hash)
            print("Registration completed successfully!")
            return True
        except Exception as e:
            print(f"Failed to complete registration: {e}")
            return False
    
    def get_player_info(self):
        """Get complete player information"""
        if self.player_id is None:
            return None
        return get_player_info(self.player_id)

def main():
    """Main entry point for the game logic"""
    game = GameLogic()
    
    print("Galaxy Resource Manager - Player Registration")
    print("=" * 50)
    
    # Get npub from player
    npub = input("Please enter your Nostr public key (npub): ").strip()
    
    # Create account
    if not game.create_player_account(npub):
        print("Account creation failed!")
        return
    
    # Generate invoice for payment
    print("\nGenerating payment invoice...")
    invoice = game.generate_lightning_invoice(5000)
    
    print(f"\nPlease pay {invoice['amount']} sats to the following Lightning invoice:")
    print(invoice['invoice'])
    print("\nAfter payment, please confirm by pressing Enter...")
    input()
    
    # Check if payment was received
    if game.check_payment():
        print("\nPayment confirmed! Account fully activated.")
        
        # Display player information
        player_info = game.get_player_info()
        print("\nPlayer Information:")
        print(f"  Nostr Public Key: {player_info['npub']}")
        print(f"  Account Balance: {player_info['fiat_balance']} fiat")
        print("Registration complete!")
    else:
        print("\nPayment not received. Please pay the invoice to continue.")
        print("You can restart this process after payment.")

if __name__ == "__main__":
    main()