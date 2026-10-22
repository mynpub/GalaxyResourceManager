# Galaxy Resource Manager

A terminal-based MMORPG prototype with SQLite, local username/password accounts,
and free access.

## Project Overview

Galaxy Resource Manager is a terminal-based multiplayer role-playing game prototype
where players manage resources in space. Register a username and password to create
an immediately usable account. No signup payment or activation step is required.
The current implementation provides local account registration, authentication,
and balance storage, not a network service or a complete gameplay loop.

## Features

- Terminal-based interface
- Username/password signup and login
- Salted PBKDF2-HMAC-SHA256 password storage
- Transaction-safe SQLite storage and legacy migration
- Persistent player IDs and in-game fiat/bitcoin balances

## Installation

Use Python 3.10 or newer. Runtime and tests use only the standard library;
no database server, signing software, or payment service is needed.

Optionally create a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
```

Initialize storage (also done automatically on first account operation):
```bash
python main.py --init
```

## Usage

Create an account:
```bash
python main.py --register --username explorer
```

Log in to an existing account:
```bash
python main.py --login --username explorer
```

Omit `--username` to enter it at a prompt. Passwords are always entered using
`getpass`, never command-line arguments; signup asks for confirmation. Use an
interactive terminal: if hidden input is unavailable, authentication stops rather
than falling back to echoed input. Do not place passwords in shell commands or
logs. Successful signup/login displays the username and balances, without any
invoice or payment check.

For welcome instructions or help:
```bash
python main.py
python main.py --help
```

`python game_logic.py` prompts for `register` or `login` and uses the same secure
flow. It also accepts the options above.

### Credential rules and authentication state

- Usernames contain **3-32 ASCII letters, digits, or underscores**. They are
  stored lowercase and are case-insensitive (`Explorer` and `explorer` are the
  same username). Whitespace is rejected, not trimmed. Database constraints
  enforce normalized usernames and uniqueness, including concurrent signup.
- Passwords contain **12-128 Unicode characters**. Control and other Unicode
  category-C characters (including surrogates and formatting characters) are
  rejected. Spaces are allowed and significant. Passwords are not trimmed,
  normalized, or silently truncated; case matters.
- Each password uses PBKDF2-HMAC-SHA256 with **600,000 iterations**, a random
  **16-byte per-account salt**, and a **32-byte derived key**. Only the encoded
  scheme, iteration count, salt, and hash are stored; comparisons use
  `hmac.compare_digest`.
- `GameLogic.create_player_account(username, password)` and
  `GameLogic.login(username, password)` return a boolean. Success authenticates
  that player in the current instance; every failed attempt clears prior
  authentication. Duplicate signup never logs in an existing account.
- Authentication is local and lasts for the current process/`GameLogic` instance.
  There are no persistent login tokens, password recovery, or account-claim
  facilities. Protect the database, JSON backups, and OS account from other users.

## Project Structure

```
GalaxyResourceManager/
├── main.py            # Main entry point with command-line argument parsing
├── database.py        # SQLite storage and player data management
├── authentication.py  # Credential validation and password hashing
├── game_logic.py      # Registration and current-player authentication state
├── requirements.txt   # No third-party dependencies currently required
└── README.md          # This file
```

## Storage and legacy migration

Storage lives at `~/.galaxy_resources/galaxy_resources.db`. Back it up before
upgrading. Schema version 3 upgrades both earlier SQLite versions automatically.
The player table is rebuilt transactionally to allow new accounts without a
legacy public key, while copying existing IDs, public keys, creation dates, and
balances unchanged. Payment rows and any existing verification challenges remain
untouched as **inert history**. Foreign-key references are checked before commit,
and enforcement is restored before normal operations. Failed migrations roll
back and can be retried after correcting the underlying data; initialization
is repeatable and serialized across concurrent processes.

**Legacy Nostr accounts have no passwords or usernames.** Their credentials
cannot be inferred. They remain preserved but cannot log in through the new
authentication flow. No automatic linking, unauthenticated password assignment,
or account-claim flow is provided. Registering a username creates a separate
player with a new ID and zero starting balances; it never takes over a legacy
account or transfers its balances.

For a previously uninitialized database, existing `players.json` and
`payments.json` in `~/.galaxy_resources` are imported together in the migration
transaction. IDs, balances, and payment history are preserved, with the same
legacy-account limitations above. The JSON files remain untouched as backups
and are not read again after a successful import. Invalid JSON or broken payment
references abort the migration without a partial import; do not delete legacy
files to work around an import error.

Invoice generation, payment processing, activation gates, Nostr signing, and
their CLI flags are removed. In-game fiat/bitcoin balances are **not** removed.

## Tests

Run the isolated standard-library test suite (uses temporary storage, not your
real account database):
```bash
python -m unittest discover -v
```
Or run focused tests with `python -m unittest test_authentication.py`,
`python -m unittest test_database.py`, or `python test_game_flow.py`.

## License

MIT