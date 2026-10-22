# Galaxy Resource Manager

A terminal-based MMORPG using SQLite, Nostr npubs, and Lightning payments.

## Project Overview

Galaxy Resource Manager is a terminal-based multiplayer online role-playing game where players manage resources in space. Accounts are created using Nostr public keys (npub) and require a 5000 satoshi payment to activate.

## Features

- Terminal-based interface
- Nostr npub authentication
- Lightning Network payments (5000 sats for account creation)
- SQLite database storage with indexed player and payment lookups
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
├── database.py        # SQLite storage and player data management
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
- Transaction-safe SQLite persistence at `~/.galaxy_resources/galaxy_resources.db`

Storage initializes automatically when first used, or explicitly with `python main.py --init`.
Existing `players.json` and `payments.json` files in `~/.galaxy_resources` are
imported together in a single transaction on first use. IDs, balances, and payment
records are preserved. The JSON files remain untouched as backups and are not
read again after a successful import. SQLite uses Python's standard library;
no database server is required.

Run isolated persistence and registration tests with:
```bash
python -m unittest test_database.py
```
The existing demonstration script also remains available as `python test_game_flow.py`.

To process real payments, connect to your Core Lightning node; invoice generation
and payment confirmation are still simulated.

## License

MIT