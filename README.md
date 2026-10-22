# Galaxy Resource Manager

A terminal-based MMORPG using SQLite, Nostr npubs, and Lightning payments.

## Project Overview

Galaxy Resource Manager is a terminal-based multiplayer online role-playing game where players manage resources in space. Accounts are created using Nostr public keys (npub) and require a 5000 satoshi payment to activate.

## Features

- Terminal-based interface
- Nostr public-key ownership verification using single-use signed challenges
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

1. Begin registration using a NIP-19 `npub1...` or 64-character hex public key:
```bash
python main.py --begin-register --npub <your_npub>
```

The response contains a random challenge `id`, the exact `challenge` text, its
`signing_hash`, and UTC Unix `created_at`/`expires_at` timestamps. Challenges expire
after **10 minutes**. `--register` remains an alias for `--begin-register`; it no
longer creates an account without proof of ownership.

2. Sign externally with the corresponding Nostr private key. Use **BIP-340
Schnorr over the 32-byte SHA-256 digest of the exact UTF-8 challenge text**.
The `signing_hash` field is that digest in hex: decode it to bytes before signing.
Decode JSON escapes in `challenge`; do not sign the JSON quotation marks, literal
`\n` characters, or add a trailing newline. The resulting signature is 64 bytes,
submitted as 128 hex characters. This is a raw challenge signature, **not** a
serialized signed Nostr event or a NIP-04/NIP-44 encrypted message. An event-only
browser signer such as a basic NIP-07 extension cannot sign this raw digest.
Never enter your private key into the registration CLI or send it to the game.

For example, on your own trusted signing machine with `nostr` installed, sign
the displayed hash without putting the private key in shell history:
```python
from getpass import getpass
from nostr.key import PrivateKey

key = PrivateKey.from_nsec(getpass("Your nsec (local signing only): "))
digest = bytes.fromhex(input("Registration signing_hash: ").strip())
print(key.sign_message_hash(digest))
```

3. Complete ownership verification and generate the simulated payment invoice:
```bash
python main.py --complete-register --npub <your_npub> \
  --challenge-id <id> --signature <128_character_hex_signature>
```

Only successful signature verification creates/loads an account and generates an
invoice. Each challenge can succeed once, including across separate CLI runs or
concurrent requests. Invalid signatures do not consume it; expired or used
challenges require starting again. Keys are stored as lowercase hex, so hex,
uppercase hex, and equivalent npub inputs refer to the same account.

4. For the welcome/instructions mode (default):
```bash
python main.py
```

`python game_logic.py` also provides a prompted registration flow, including the
external signing step. `python main.py --help` lists all options.

## Project Structure

```
GalaxyResourceManager/
├── main.py            # Main entry point with command-line argument parsing
├── database.py        # SQLite storage and player data management
├── game_logic.py      # Core game logic for account creation and payment flow
├── ownership.py       # Nostr key normalization and Schnorr verification
├── requirements.txt   # Python dependencies
└── README.md          # This file
```

## Payment Flow

1. Player requests a registration challenge for their Nostr public key
2. Player signs externally; the system verifies ownership and consumes the challenge
3. System creates/loads the account and generates a Lightning invoice for 5000 sats
4. Player pays the invoice; payment validation and activation remain simulated

## Development Notes

This is a barebones implementation that demonstrates:
- Nostr public key normalization and cryptographic ownership verification
- Account creation flow
- Payment validation (simulated)
- Transaction-safe SQLite persistence at `~/.galaxy_resources/galaxy_resources.db`

Storage initializes automatically when first used, or explicitly with `python main.py --init`.
Existing `players.json` and `payments.json` files in `~/.galaxy_resources` are
imported together in a single transaction on first use. IDs, balances, and payment
records are preserved. The JSON files remain untouched as backups and are not
read again after a successful import. SQLite uses Python's standard library;
no database server is required.

This checkout already uses SQLite rather than JSON for live storage, so
verification challenges are stored in the same local database. Existing SQLite
databases are upgraded automatically without discarding players or payments.
Challenge records retain `issued`, `used`, or `expired` status, their claimed
public key, challenge text, and timestamps. Expiration is persisted when a
challenge is accessed; redemption checks the deadline again in the same
transaction that marks it used and creates/loads the account. Protect the local
database from untrusted modification, just as you would the legacy JSON files.
This registration proof is not ongoing session authentication for gameplay.
The unavailable `python-nostr==1.4.0` dependency pin has been corrected to
`nostr==0.0.2`, the published package for the original Python Nostr library.

Run isolated persistence and registration tests with:
```bash
python -m unittest test_database.py test_ownership.py
```
The existing demonstration script also remains available as `python test_game_flow.py`.

To process real payments, connect to your Core Lightning node; invoice generation
and payment confirmation are still simulated.

## License

MIT