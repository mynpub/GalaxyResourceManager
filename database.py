#!/usr/bin/env python3
"""
Database module for Galaxy Resource Manager - handles player data simulation
"""

import os
import json
import hashlib
from datetime import datetime
from pathlib import Path

# For testing purposes, we'll simulate database storage in local files
DATA_DIR = Path.home() / ".galaxy_resources"
DATA_DIR.mkdir(exist_ok=True)

def init_database():
    """Initialize the data storage for simulation"""
    print("Database initialized successfully!")
    print("Storage location:", DATA_DIR)
    
def get_or_create_player(npub):
    """Get existing player or create a new one (simulated)"""
    # Create a simple file-based storage system
    players_file = DATA_DIR / "players.json"
    
    if players_file.exists():
        with open(players_file, 'r') as f:
            players_data = json.load(f)
    else:
        players_data = {}
    
    if npub in players_data:
        print(f"Player already exists with npub: {npub}")
        return players_data[npub]['id']
    else:
        # Create new player
        new_id = len(players_data) + 1
        players_data[npub] = {
            'id': new_id,
            'npub': npub,
            'created_at': datetime.now().isoformat(),
            'balance': {'fiat': 0.00, 'bitcoin': 0.00000000}
        }
        
        with open(players_file, 'w') as f:
            json.dump(players_data, f)
            
        print(f"New player created with npub: {npub}")
        return new_id

def get_player_info(player_id):
    """Get complete player information (simulated)"""
    players_file = DATA_DIR / "players.json"
    
    if players_file.exists():
        with open(players_file, 'r') as f:
            players_data = json.load(f)
            
        # Find player by ID
        for npub, player_info in players_data.items():
            if player_info['id'] == player_id:
                return {
                    'id': player_info['id'],
                    'npub': npub,
                    'fiat_balance': player_info['balance']['fiat'], 
                    'bitcoin_balance': player_info['balance']['bitcoin']
                }
    
    return None

def mark_payment_paid(player_id, invoice_hash):
    """Mark a player's payment as paid (simulated)"""
    # In a real implementation, this would update the database
    payments_file = DATA_DIR / "payments.json"
    
    if payments_file.exists():
        with open(payments_file, 'r') as f:
            payments_data = json.load(f)
    else:
        payments_data = {}
    
    # Mark payment as paid for player_id
    payments_data[player_id] = {
        'paid': True,
        'invoice_hash': invoice_hash,
        'timestamp': datetime.now().isoformat()
    }
    
    with open(payments_file, 'w') as f:
        json.dump(payments_data, f)
        
    print(f"Payment marked as paid for player {player_id}")

def check_payment_status(player_id):
    """Check if a player has made the required payment (simulated)"""
    payments_file = DATA_DIR / "payments.json"
    
    if payments_file.exists():
        with open(payments_file, 'r') as f:
            payments_data = json.load(f)
            
        if player_id in payments_data:
            return payments_data[player_id]['paid']
    
    return False

if __name__ == "__main__":
    # Initialize the database when run directly
    init_database()