# Galaxy Resource Manager

A terminal-based MMORPG prototype with SQLite, local username/password accounts,
and free access.

## Project Overview

Galaxy Resource Manager is a terminal-based multiplayer role-playing game prototype
where players manage resources in space. Register a username and password to create
an immediately usable account. No signup payment or activation step is required.
The current implementation provides account registration, authentication,
balance storage, and hosted TCP terminal sessions, not a complete gameplay loop.

## Features

- Terminal-based interface
- Multi-client hosted TCP terminal sessions
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

Omit `--username` to enter it at a prompt. In local mode, passwords are entered using
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

## Hosted TCP Server Mode

Start the server on the host where players' accounts should be stored:
```bash
python main.py --serve-tcp --host 0.0.0.0 --port 7777
```

The defaults are `0.0.0.0:7777`. No external services or additional dependencies
are required. SQLite initializes automatically, using the server OS user's
`~/.galaxy_resources/galaxy_resources.db`. Local and hosted modes use the same
account APIs and storage. Back up this directory and restrict its permissions.

After deploying to `129.121.136.245`, players connect with:
```bash
nc 129.121.136.245 7777
```

Choose `signup` (alias `register`) or `login`, then enter your username and
password at the prompts. Signup asks for password confirmation. Use `help`,
`status` or `profile` (username and balances), and `quit`. Each connection has
independent authentication state. There are no resource-management commands yet.
Input is newline-delimited UTF-8; LF and CRLF are supported. Basic telnet clients
can work in line mode, but Telnet option negotiation and terminal escape/control
sequences are not supported; netcat is recommended.

For an Ubuntu host using UFW, an example public firewall rule is:
```bash
sudo ufw allow 7777/tcp
```
Also check any provider firewall/security group. Prefer restricting source IPs or
using a tunnel rather than exposing credentials publicly. Opening this port does
not alter the website's HTTP/HTTPS service.

### Security and limitations

**Plain TCP has no encryption or server authentication: credentials and game
traffic travel in cleartext.** Netcat does not provide password masking; your
terminal may visibly echo passwords as you type. The server never echoes typed
passwords in responses or logs. Do not reuse passwords from other services.

For production, use an SSH tunnel, stunnel, or a TLS-capable TCP reverse proxy
(an ordinary HTTP proxy is not sufficient). For example, bind the game server to
`127.0.0.1` and forward it from your client:
```bash
ssh -N -L 7777:127.0.0.1:7777 serveruser@129.121.136.245
# In a second client terminal:
nc 127.0.0.1 7777
```
The SSH account is an OS account, not a game account. With a tunnel-only setup,
do not open public TCP 7777. TLS and SSH hosting are not implemented by the game.

The lightweight safeguards are a 1,024-byte limit per line (including newline),
120-second socket inactivity timeout, at most 64 simultaneous clients, and
disconnect after three failed authentication/signup attempts per connection.
Invalid UTF-8, control characters, and oversized lines disconnect that client.
These are not per-IP/global abuse protection: reconnecting resets attempt limits.
Use firewall restrictions and external rate limiting for public deployments.
Threads and SQLite suit light use, not large-scale multiplayer hosting.

### Running continuously

For a simple foreground deployment:
```bash
tmux new -s galaxy
python main.py --serve-tcp --host 0.0.0.0 --port 7777
```
Detach with Ctrl+B, D; return with `tmux attach -t galaxy`. Ctrl+C stops the
listener, disconnects active clients, and joins session threads. An in-flight
database/hash operation finishes before its thread exits.

For systemd, install the following as `/etc/systemd/system/galaxy-tcp.service`,
adjusting the account and checkout path. Run as a dedicated unprivileged user:
```ini
[Unit]
Description=Galaxy Resource Manager TCP server
After=network.target

[Service]
Type=simple
User=galaxy
WorkingDirectory=/opt/GalaxyResourceManager
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /opt/GalaxyResourceManager/main.py --serve-tcp --host 0.0.0.0 --port 7777
KillSignal=SIGINT
TimeoutStopSec=30
Restart=on-failure
UMask=0077

[Install]
WantedBy=multi-user.target
```
Ensure the service user has a writable home for SQLite storage. `KillSignal=SIGINT`
uses the same graceful shutdown as Ctrl+C. Enable with
`sudo systemctl daemon-reload && sudo systemctl enable --now galaxy-tcp`.
View startup and client connect/disconnect logs with
`journalctl -u galaxy-tcp -f`. These instructions deploy the feature; they do not
automatically install it on the public server.

## Project Structure

```
GalaxyResourceManager/
├── main.py            # Main entry point with command-line argument parsing
├── database.py        # SQLite storage and player data management
├── authentication.py  # Credential validation and password hashing
├── game_logic.py      # Registration and current-player authentication state
├── tcp_server.py      # Threaded, bounded TCP terminal sessions
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
`python -m unittest test_database.py`, `python test_game_flow.py`, or
`python -m unittest test_tcp_server -v` for loopback TCP and server CLI tests.

## License

MIT