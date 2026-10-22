#!/usr/bin/env python3
"""
Test script for the Galaxy Resource Manager game flow
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_database, get_player_info, mark_payment_paid, check_payment_status
from game_logic import GameLogic
from nostr.key import PrivateKey

def test_player_creation():
    """Test player creation workflow"""
    print("Testing player creation workflow...")
    
    # Initialize database
    init_database()
    
    # Generate a disposable key and sign the registration challenge.
    key = PrivateKey()
    game = GameLogic()
    
    try:
        challenge = game.begin_registration(key.public_key.bech32())
        signature = key.sign_message_hash(bytes.fromhex(challenge['signing_hash']))
        assert game.create_player_account(
            key.public_key.bech32(), signature, challenge['id'])
        player_id = game.player_id
        print(f"Player created with ID: {player_id}")
        
        player_info = get_player_info(player_id)
        print(f"Player info retrieved: {player_info}")
        
        # Test payment tracking
        mark_payment_paid(player_id, "test_invoice_hash_123")
        paid_status = check_payment_status(player_id)
        print(f"Payment status: {paid_status}")
        
        print("All tests passed!")
        return True
        
    except Exception as e:
        print(f"Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    test_player_creation()