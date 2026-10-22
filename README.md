# Galaxy Resource Manager

A terminal-based MMORPG using PostgreSQL, Nostr npubs, and Lightning payments.

## Project Overview

Galaxy Resource Manager is a terminal-based multiplayer online role-playing game where players manage resources in space. Accounts are created using Nostr public keys (npub) and require a 5000 satoshi payment to activate.

## Features

- Terminal-based interface
- Nostr npub authentication
- Lightning Network payments (5000 sats for account creation)
- PostgreSQL database storage (simulated in this version)
- Player registration workflow

## Installation

1. Create a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
```

2. Install dependencies:
```bash
pip install --break-system-packages -r requirements.txt
```

3. Initialize the database:
```bash
python main.py --init
```

## Usage

1. Register a new player account:
```bash
python main.py --register --npub <your_npub>
```

2. For interactive mode (default):
```bash
python main.py
```

## Project Structure

```
GalaxyResourceManager/
├── main.py            # Main entry point with command-line argument parsing
├── database.py        # Database handling and player data management (simulated)
├── game_logic.py      # Core game logic for account creation and payment flow
├── requirements.txt   # Python dependencies
└── README.md          # This file
```

## Payment Flow

1. Player registers with Nostr npub
2. System generates a Lightning invoice for 5000 sats
3. Player pays the invoice
4. System validates payment and activates account

## Development Notes

This is a barebones implementation that demonstrates:
- Nostr public key validation
- Account creation flow
- Payment validation (simulated)
- Database interaction (using file-based storage for demonstration)

To fully integrate with PostgreSQL and Lightning:
1. Install PostgreSQL database
2. Configure environment variables for database connection
3. Connect to your Core Lightning node for payment processing

## License

MIT