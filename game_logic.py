#!/usr/bin/env python3
"""
Core game logic for Galaxy Resource Manager - handles player registration and payment validation
"""

import hashlib
import secrets
import time

from database import (
    get_player_info, mark_payment_paid, check_payment_status,
    create_verification_challenge, get_verification_challenge,
    consume_challenge_and_create_player,
)
from ownership import normalize_pubkey, verify_signature

CHALLENGE_LIFETIME_SECONDS = 600

class GameLogic:
    def __init__(self):
        """Initialize game logic with database connection"""
        self.player_id = None
        self.npub = None
        
    def validate_npub(self, npub: str) -> bool:
        """Validate hex or NIP-19 npub input without asserting ownership."""
        try:
            normalize_pubkey(npub)
            return True
        except ValueError:
            return False

    def begin_registration(self, npub: str) -> dict:
        """Issue a ten-minute, single-use proof of control for this public key."""
        pubkey = normalize_pubkey(npub)
        now = int(time.time())
        challenge_id = secrets.token_hex(32)
        expires_at = now + CHALLENGE_LIFETIME_SECONDS
        text = (
            "GalaxyResourceManager registration ownership proof\n"
            f"Challenge ID: {challenge_id}\n"
            f"Public key: {pubkey}\n"
            f"Created at: {now}\n"
            f"Expires at: {expires_at}"
        )
        challenge = {
            "id": challenge_id, "pubkey": pubkey, "challenge": text,
            "status": "issued", "created_at": now, "expires_at": expires_at,
            "signing_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        create_verification_challenge(challenge)
        return challenge

    def create_player_account(self, npub: str, signature: str = None,
                              challenge_id: str = None) -> bool:
        """
        Verify ownership, then atomically consume the challenge and create an account.
        Returns True if successful, False otherwise
        """
        self.player_id = None
        self.npub = None
        try:
            pubkey = normalize_pubkey(npub)
            if not signature or not challenge_id:
                raise ValueError("Ownership verification requires a challenge ID and signature.")
            challenge = get_verification_challenge(challenge_id, int(time.time()))
            if challenge is None:
                raise ValueError("Registration challenge not found.")
            if challenge['pubkey'] != pubkey:
                raise ValueError("Registration challenge belongs to a different public key.")
            if challenge['status'] != 'issued':
                raise ValueError(f"Registration challenge is {challenge['status']}; begin again.")
            if not verify_signature(pubkey, challenge['challenge'], signature):
                raise ValueError("Invalid ownership signature.")
            player_id = consume_challenge_and_create_player(
                challenge_id, pubkey, int(time.time()))
            if player_id is None:
                raise ValueError("Registration challenge is expired or already used; begin again.")
            self.player_id = player_id
            self.npub = pubkey
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
        if self.player_id is None:
            raise ValueError("Verify ownership before generating a registration invoice.")
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
    
    try:
        challenge = game.begin_registration(npub)
    except ValueError as e:
        print(e)
        return
    print("\nSign SHA-256 of this exact UTF-8 text using BIP-340 Schnorr:")
    print(challenge['challenge'])
    print(f"Signing hash: {challenge['signing_hash']}")
    signature = input("Enter the 128-character hex signature: ").strip()

    # Create account only after ownership verification.
    if not game.create_player_account(npub, signature, challenge['id']):
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