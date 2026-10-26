# Galaxy Resource Manager

A terminal-based MMORPG prototype with SQLite, local username/password accounts,
and free access.

## Project Overview

Galaxy Resource Manager is a terminal-based multiplayer role-playing game prototype
where players manage resources in space. Register a username and password to create
an immediately usable account. No signup payment or activation step is required.
The current implementation is an Earth-only playable vertical slice with
gathering, crafting, power, simulated Bitcoin blocks and escrowed auctions.
It is not a production-balanced game: signup grants nothing, and production
land/workforce acquisition and the bootstrap economy remain unresolved.

## Features

- Terminal-based interface
- Multi-client hosted TCP terminal sessions
- Username/password signup and login
- Salted PBKDF2-HMAC-SHA256 password storage
- Transaction-safe SQLite storage and legacy migration
- Persistent player IDs and in-game fiat/bitcoin balances
- Earth land, all 118 elemental inventory records, workers and Mining/Crafting
- Timed recipes, installed solar/ASIC equipment and exact-satoshi simulated mining
- Earth auctions with asset, bid and seller-fee escrow
- Database-serialized simulation, bounded offline catch-up and reward history

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

## Playing the Earth slice

Run `python main.py --login --username explorer --play` for a local game prompt,
or append repeatable `--command "inventory"` options for one-shot commands.
Hosted sessions use the same commands after login. `help` lists syntax:

```text
world                         land
land buy ACRES                land sell PARCEL_ID
inventory                     resources
workers                       assign WORKER_ID PARCEL_ID|none
skills                        craft solar_panel|asic|droid
jobs                          equipment
equip ITEM_ID install|uninstall|on|off
power                         hashrate
chain                         auctions
auction list resource|item|land SYMBOL_OR_ID QUANTITY fiat|btc RESERVE [FEE_SATS] [SECONDS]
auction bid AUCTION_ID AMOUNT
auction cancel AUCTION_ID
status                        quit
```

Quantities are **integer micrograms** (1 kg = 1,000,000,000 micrograms);
prices/bids are **integer microfiat** (1 fiat = 1,000,000 microfiat) or **satoshis**
(1 BTC = 100,000,000 satoshis). Inventory also displays mg/g/kg. IDs come from
the corresponding list commands. Symbols are case-sensitive (`Fe`, not `fe`).
Commands never accept an acting player ID; authorization comes from the session.

### Explicit test-world walkthrough (not production bootstrap)

Every signup starts at **zero fiat, zero BTC, zero of all 118 elements, no
land, no equipment and no workers**. There is deliberately no hidden welcome
grant or automatic human recruitment. Consequently an unseeded new world has
a bootstrap deadlock. This PR does **not** resolve the production economic
policy by inventing free assets.

For a separate, disposable local world:

```bash
python main.py --data-dir /tmp/grm-demo --register --username explorer
python seed_test_world.py --data-dir /tmp/grm-demo --username explorer \
  --confirm-test-grants --fiat 100000000 --humans 1 --recipe-sets 1
python main.py --data-dir /tmp/grm-demo --login --username explorer --play
```

The seeding tool requires an explicit directory, existing username and
confirmation flag. Each call **adds** the requested test grants; it is not
idempotent. It is local operator tooling, never a TCP command or signup path.
`--btc` optionally adds test satoshis, not chain subsidy. **Never seed production
or treat a seeded world's balances as a finite-supply Bitcoin experiment.**

At the game prompt:

```text
land buy 1
land
workers
assign 1 1
craft solar_panel
craft asic
craft droid
jobs
inventory
```

Use the actual IDs displayed. Wait for the configured recipe durations, then
inspect `equipment`, install the panel and ASIC with `equip ID install`, and
inspect `power` and `chain`. `equip ID off` disables an installed item;
`uninstall` disables and removes it. Assign the completed droid to another
owned acre, or unassign the human first. Gather/craft completion is automatic;
there is no repeatable “collect” reward. A second registered/explicitly seeded
test account can bid on listings, for example:

```text
auction list resource Fe 1000 fiat 1000000 0 60
auction bid 1 1000000
```

Run a disposable TCP world with
`python main.py --data-dir /tmp/grm-demo --serve-tcp --host 127.0.0.1 --port 7777`
and connect using `nc 127.0.0.1 7777`. The plaintext warnings below still apply.
No live deployment, firewall or service changes are made by the implementation.

### Provisional balance, data and open decisions

These are configurable **tuning defaults**, not user-approved balance decisions:

- Only Earth is active: **36,800,000,000 whole land acres**. Sea is disabled.
  Planet-keyed land and auctions leave room for future worlds without activating
  any. A provisional primary-market price is **1 fiat/acre**, also the sell-back
  price; resale removes a parcel and returns its acres to available supply.
  Assigned or auction-locked land cannot be sold. One worker per owned acre.
- Humans and droids each gather **1 mg/second**, with equal efficiency, no
  level-based speed bonus and no upkeep for this slice. Human recruitment/pricing is **unimplemented**;
  only explicit operator test grants currently create humans. Workers must be
  assigned to their owner's unlocked Earth land.
- `catalog.py` contains all 118 elements, but catalog membership is **not**
  natural availability. The crust profile is a normalized, hand-rounded game
  approximation informed by the abundance figure in
  [USGS Fact Sheet 087-02](https://pubs.usgs.gov/fs/2002/fs087-02/), not a
  verbatim scientific dataset, ore grade or recoverability model. Trace values
  are explicitly approximate; unmodeled/absent/synthetic elements have zero
  weight. There is no promise every natural element is available. Whole
  micrograms plus per-element fractional numerator carry conserve mass across
  tick partitioning, including tiny yields. Do not change weights in a running
  world without migrating carry denominators.
- Mining gains **1 XP/worker-second**. The configurable quadratic threshold is
  `ceil(631152000 * (level-1)^2 / 98^2)`; levels range **1–999**. A year is
  **365.25 days** (Julian year), so one constantly active worker at level-1
  rates reaches level 99 in exactly **20 years**. No level multiplier changes
  that rate. Crafting uses the same threshold curve provisionally.
- `recipes.json` is the machine-readable recipe contract: microgram inputs,
  seconds, Crafting XP, outputs and item stats. All three recipes unlock at
  level 1. The agreed input quantities are retained, including simplified
  silicon/glass feedstock. Durations, XP and efficiency are tuning defaults.
  Inputs are consumed atomically at job creation; no cancellation/refund is
  provided. The stored recipe snapshot protects jobs from subsequent edits.
  Provisional duration/XP pairs: solar panel **60 seconds / 60 XP**, ASIC
  **120 seconds / 120 XP**, droid **180 seconds / 180 XP**. Crust-average precious
  metal yields are extremely slow for a lone worker; throughput and production
  balance have not been playtested.
- Solar panels supply **100 W continuously** (no night/weather/storage model).
  A basic ASIC consumes **100 W**, with provisional **1 TH/s**. Oldest enabled,
  installed ASICs receive the owner's power first. No double-use: locked or
  uninstalled equipment supplies neither power nor hash. Droids require land,
  not electrical power, under the current zero-upkeep simplification. Use
  `assign`, not `equip`, to operate droids; selling one transfers its linked
  worker too, and requires it to be unassigned.
- Auction default duration is **1 hour**, configurable per listing from 1 second
  to 7 days; fixed closing time, no anti-sniping extension. Reserve is a minimum
  bid, not a second-price rule. Highest bid wins and pays its full bid.
  Bidders can reuse their own prior escrow when raising. Outbid funds are
  refunded immediately. Cancellation is allowed only while active, even with
  bids; at closing, no-bid auctions expire/refund, fiat sales settle immediately,
  and BTC sales enter irrevocable pending settlement. No cancellation after
  closing. Item/land sales transfer whole records; land must be unassigned and
  equipment uninstalled. Listed resources are removed from available inventory.
  BTC sellers separately reserve their chosen **absolute satoshi fee at listing**.
- Workers/acre, recruitment, initial pricing, long-term balance, richer
  planetary geology, weather, crafting concurrency limits and economic sinks
  remain open production decisions. There is no direct player-to-player transfer,
  real Bitcoin, external payment API or external payout.

### Authoritative time, blocks and offline policy

The first simulation tick establishes this world's persistent epoch. Blocks
occur at `epoch + height * 600` seconds, starting at height 1, **exactly**.
No random block discovery. The hosted server loop polls once per second;
local `--play` runs one scheduler while waiting for input. SQLite
`BEGIN IMMEDIATE` serializes ticks and actions across threads/processes.
Actions first catch up time so players cannot apply new equipment retroactively.
Job, auction and block boundaries are processed chronologically.

**Provisional offline policy:** assigned workers and installed/enabled equipment
operate through all elapsed offline time. Jobs and auctions also complete
offline. Catch-up is resumable in bounded batches of 256 event steps (and
1,500 job completions/auction closures per step). A long backlog temporarily rejects mutations
while committing catch-up progress; retry or leave the server running. Time is
integer seconds; backward clock movement pauses simulation rather than undoing
history. Asset columns are nonnegative signed 64-bit integers; overflow pauses
the affected transaction rather than clipping or losing assets. Very large
worlds need wider-quantity storage and capacity planning. Use a stable system
clock and back up storage. No distributed
high-availability or heavy-load scalability claim is made.

Mining rewards use **time-weighted powered hash-seconds** within each block,
persisted across restart. Equipment finishing at a boundary contributes only
after that boundary. Subsidy is 50 BTC for heights 1–210,000, then integer
satoshi right shifts every 210,000 blocks, ending after 33 eras. Full-schedule
issuance is 20,999,999.9769 BTC, below the 21-million cap. No miners means the
scheduled subsidy is **unissued permanently**, not saved for a later miner.

Pending BTC settlements are included highest **absolute fee** first, then
closing time and listing ID (deterministic FIFO ties), with **at most 1,500
transactions globally per block**, not per planet. Lower-priority escrows stay
pending for future blocks; there is no listing/mempool size cap. Successful
settlement fees redistribute existing BTC. If no miners contributed, fee funds
stay in the chain's fee-carry pool and are paid at the next block with work,
even after subsidies end. Fiat sales consume no block capacity. Proportional
payouts use exact integer division and largest fractional remainders, breaking
ties by player ID; every pool satoshi is accounted for. Blocks, reward ledger,
chain state, settlement state and payouts commit atomically and cannot replay.

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
independent authentication state. After login, `help` also lists gameplay commands.
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
├── economy.py         # Fixed-point assets, Earth, workers, crafting and equipment
├── catalog.py         # 118 elements, approximate crust profile and XP curve
├── recipes.json       # Agreed inputs and provisional recipe tuning
├── auctions.py        # Planet markets, assets/bids/fees escrow and settlement
├── blockchain.py      # Scheduled blocks, time-weighted mining and reward ledger
├── simulation.py      # Serialized, bounded authoritative time advancement
├── commands.py        # Shared authenticated gameplay commands
├── seed_test_world.py # Strictly opt-in local operator test grants
├── requirements.txt   # No third-party dependencies currently required
└── README.md          # This file
```

## Storage and legacy migration

Storage lives at `~/.galaxy_resources/galaxy_resources.db`. Back it up before
upgrading. Schema version 4 upgrades earlier SQLite versions automatically.
The player table is rebuilt transactionally to allow new accounts without a
legacy public key, while copying existing IDs, public keys, creation dates, and
balances unchanged. Payment rows and any existing verification challenges remain
untouched as **inert history**. Foreign-key references are checked before commit,
and enforcement is restored before normal operations. Failed migrations roll
back and can be retried after correcting the underlying data; initialization
is repeatable and serialized across concurrent processes.

The v4 forward migration adds the economy tables without replacing existing
player/payment rows. Legacy REAL balance columns remain unchanged as an inert
archive; new gameplay reads/writes `accounts` integer microfiat/satoshis.
Conversion uses the stored value's decimal string, floors to the fixed-point
unit, and preserves **every sub-unit decimal residual** in explicit
`legacy_fiat_remainder` / `legacy_btc_remainder` text fields. These residuals are
not spendable yet; a future conversion policy is needed, and original binary
REAL precision cannot be recovered. Negative/nonfinite/overflowing legacy
balances abort migration for operator resolution rather than silently losing
value. Every player gets missing elemental records at zero without replacing
existing holdings. Normal signup initializes all records in one transaction.

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