"""Integer Earth economy. Call advance only inside a simulation transaction."""

import json
import re
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR, localcontext
from pathlib import Path

import catalog
import database

LAND_SUPPLY = 36800000000
LAND_PRICE = 1000000
# Micrograms per second (~33.4 t/s), human and droid alike: one worker gathers every
# basic ASIC and solar panel input in 60 seconds (ASIC gold is the bottleneck).
GATHER_RATE = 33_437_202_633_334
RECIPES = json.loads(Path(__file__).with_name("recipes.json").read_text())["recipes"]
MAX_INTEGER = 2 ** 63 - 1
MAX_SEED_COUNT = 10000
JOB_BATCH = 1500
EARTH_BANK_NAME = "Earth Bank"
EARTH_BANK_NPUB = "system:earth_bank"
EARTH_BANK_LOT_ACRES = 1000
EARTH_BANK_AUCTION_DURATION = 3600
EARTH_BANK_PRICE_WINDOW = 30 * 86400
BANK_INITIAL_FIAT = 1000 * 1_000_000
BANK_WORK_WAGE = 1
BANK_WORK_SHIFT = 8 * 3600


def integer(value, name="quantity", minimum=1):
    if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value):
        try:
            value = int(value)
        except ValueError:
            raise ValueError(f"{name} exceeds supported integer range") from None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    if value > MAX_INTEGER:
        raise ValueError(f"{name} exceeds SQLite integer range")
    return value


def _legacy(value, scale, name):
    with localcontext() as context:
        context.prec = 400
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError(f"Cannot migrate {name}: negative or nonfinite legacy balance")
        units = amount * scale
        whole = int(units.to_integral_value(rounding=ROUND_FLOOR))
        integer(whole, name, 0)
        return whole, str(amount - Decimal(whole) / scale)


def initialize(c):
    """Create the v4 economy schema inside the locked, rollback-safe migration."""
    statements = [
        """CREATE TABLE accounts (
            player_id INTEGER PRIMARY KEY REFERENCES players(id),
            fiat INTEGER NOT NULL DEFAULT 0 CHECK(typeof(fiat)='integer' AND fiat>=0),
            btc INTEGER NOT NULL DEFAULT 0 CHECK(typeof(btc)='integer' AND btc>=0),
            legacy_fiat_remainder TEXT NOT NULL DEFAULT '0',
            legacy_btc_remainder TEXT NOT NULL DEFAULT '0')""",
        """CREATE TABLE inventory (
            player_id INTEGER NOT NULL REFERENCES players(id), symbol TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0 CHECK(typeof(quantity)='integer' AND quantity>=0),
            carry INTEGER NOT NULL DEFAULT 0 CHECK(typeof(carry)='integer' AND carry>=0),
            PRIMARY KEY(player_id,symbol))""",
        """CREATE TABLE planets (
            name TEXT PRIMARY KEY, total_acres INTEGER NOT NULL CHECK(total_acres>0))""",
        """CREATE TABLE land (
            id INTEGER PRIMARY KEY, player_id INTEGER NOT NULL REFERENCES players(id),
            planet TEXT NOT NULL DEFAULT 'Earth' REFERENCES planets(name),
            acres INTEGER NOT NULL CHECK(typeof(acres)='integer' AND acres>0),
            locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)))""",
        """CREATE TABLE workers (
            id INTEGER PRIMARY KEY, player_id INTEGER NOT NULL REFERENCES players(id),
            kind TEXT NOT NULL CHECK(kind IN ('human','droid')),
            land_id INTEGER REFERENCES land(id),
            equipment_id INTEGER UNIQUE REFERENCES equipment(id),
            locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)))""",
        """CREATE TABLE equipment (
            id INTEGER PRIMARY KEY, player_id INTEGER NOT NULL REFERENCES players(id),
            kind TEXT NOT NULL CHECK(kind IN ('solar_panel','asic','droid')),
            installed INTEGER NOT NULL DEFAULT 0 CHECK(installed IN (0,1)),
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
            locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)),
            hashrate INTEGER NOT NULL DEFAULT 1 CHECK(hashrate>=0),
            power_watts INTEGER NOT NULL DEFAULT 100 CHECK(power_watts>0))""",
        """CREATE TABLE skills (
            player_id INTEGER NOT NULL REFERENCES players(id), name TEXT NOT NULL,
            xp INTEGER NOT NULL DEFAULT 0 CHECK(typeof(xp)='integer' AND xp>=0),
            PRIMARY KEY(player_id,name))""",
        """CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, player_id INTEGER NOT NULL REFERENCES players(id),
            recipe_id TEXT NOT NULL, started_at INTEGER NOT NULL,
            completes_at INTEGER NOT NULL CHECK(completes_at>=started_at),
            status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed')),
            recipe_json TEXT NOT NULL)""",
        "CREATE INDEX workers_land ON workers(land_id)",
        "CREATE INDEX jobs_due ON jobs(status,completes_at)",
    ]
    for statement in statements:
        c.execute(statement)
    c.execute("INSERT INTO planets VALUES ('Earth',?)", (LAND_SUPPLY,))
    for p in c.execute("SELECT id,fiat_balance,bitcoin_balance FROM players").fetchall():
        fiat, fr = _legacy(p["fiat_balance"], 1000000, f"player {p['id']} fiat")
        btc, br = _legacy(p["bitcoin_balance"], 100000000, f"player {p['id']} bitcoin")
        c.execute("INSERT INTO accounts VALUES (?,?,?,?,?)", (p["id"], fiat, btc, fr, br))
        ensure_player(c, p["id"])
    import blockchain
    import auctions
    blockchain.initialize(c)
    auctions.initialize(c)


def migrate_v5(c):
    """Create the Earth Bank and move unowned Earth acreage into its reserve."""
    c.execute(
        "ALTER TABLE auctions ADD COLUMN bid_quantity INTEGER NOT NULL DEFAULT 0 "
        "CHECK(bid_quantity >= 0)"
    )
    c.execute("""CREATE TABLE system_accounts (
        name TEXT PRIMARY KEY, player_id INTEGER NOT NULL UNIQUE REFERENCES players(id))""")

    npub = EARTH_BANK_NPUB
    suffix = 0
    while c.execute("SELECT 1 FROM players WHERE npub=?", (npub,)).fetchone():
        suffix += 1
        npub = f"{EARTH_BANK_NPUB}:{suffix}"
    if not c.execute("SELECT 1 FROM players WHERE id=0").fetchone():
        c.execute("INSERT INTO players(id,npub,created_at) VALUES (0,?,?)",
                  (npub, datetime.now().isoformat()))
        player_id = 0
    else:
        player_id = c.execute(
            "INSERT INTO players(npub,created_at) VALUES (?,?)",
            (npub, datetime.now().isoformat())
        ).lastrowid
    c.execute("INSERT INTO system_accounts(name,player_id) VALUES (?,?)",
              (EARTH_BANK_NAME, player_id))
    ensure_player(c, player_id)

    supply = c.execute("SELECT total_acres FROM planets WHERE name='Earth'").fetchone()[0]
    allocated = c.execute(
        "SELECT coalesce(sum(acres),0) FROM land WHERE planet='Earth'"
    ).fetchone()[0]
    if allocated > supply:
        raise sqlite3.IntegrityError("Earth land allocation exceeds total supply")
    if supply > allocated:
        c.execute("INSERT INTO land(player_id,planet,acres) VALUES (?,'Earth',?)",
                  (player_id, supply - allocated))


def fiat_supply(c):
    """Total spendable plus escrowed fiat in the world."""
    balances = [row[0] for row in c.execute("SELECT fiat FROM accounts")]
    escrow = [row[0] for row in c.execute(
        "SELECT bid FROM auctions WHERE currency='fiat' AND status IN ('active','pending')"
    )]
    return integer(sum(balances) + sum(escrow), "fiat supply", 0)


def migrate_v6(c):
    """Add Bank employment and anchor wages to this world's fiat supply."""
    bank_id = earth_bank_id(c)
    if fiat_supply(c) == 0:
        credit(c, bank_id, "fiat", BANK_INITIAL_FIAT)
    initial_supply = fiat_supply(c)
    if initial_supply <= 0:
        raise ValueError("World fiat supply must be positive")
    c.execute("""
        CREATE TABLE world_economy (
            id INTEGER PRIMARY KEY CHECK(id=1),
            initial_fiat_supply INTEGER NOT NULL CHECK(initial_fiat_supply>0)
        )
    """)
    c.execute("INSERT INTO world_economy VALUES (1,?)", (initial_supply,))
    c.execute("""
        CREATE TABLE bank_employment (
            player_id INTEGER PRIMARY KEY REFERENCES players(id),
            wage_remainder INTEGER NOT NULL DEFAULT 0 CHECK(wage_remainder>=0)
        )
    """)
    c.execute("""
        CREATE TABLE bank_work_shifts (
            id INTEGER PRIMARY KEY,
            player_id INTEGER NOT NULL REFERENCES players(id),
            started_at INTEGER NOT NULL,
            completes_at INTEGER NOT NULL CHECK(completes_at>started_at),
            wage_numerator INTEGER NOT NULL CHECK(wage_numerator>0),
            wage_denominator INTEGER NOT NULL CHECK(wage_denominator>0),
            status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed')),
            wage_paid INTEGER NOT NULL DEFAULT 0 CHECK(wage_paid>=0)
        )
    """)
    c.execute("""
        CREATE UNIQUE INDEX bank_one_active_shift
        ON bank_work_shifts(player_id) WHERE status='running'
    """)
    c.execute("CREATE INDEX bank_work_due ON bank_work_shifts(status,completes_at)")


def earth_bank_id(c):
    row = c.execute("SELECT player_id FROM system_accounts WHERE name=?",
                    (EARTH_BANK_NAME,)).fetchone()
    if row is None:
        raise ValueError("Earth Bank is not initialized")
    return row["player_id"]


def _seed_land(c, pid, acres):
    bank_id = earth_bank_id(c)
    remaining = acres
    parcels = c.execute(
        "SELECT id,acres FROM land WHERE player_id=? AND planet='Earth' AND locked=0 "
        "ORDER BY id",
        (bank_id,)
    ).fetchall()
    if sum(parcel["acres"] for parcel in parcels) < acres:
        raise ValueError("Not enough Earth Bank land")
    for parcel in parcels:
        take = min(remaining, parcel["acres"])
        if take == parcel["acres"]:
            c.execute("DELETE FROM land WHERE id=?", (parcel["id"],))
        else:
            c.execute("UPDATE land SET acres=acres-? WHERE id=?",
                      (take, parcel["id"]))
        remaining -= take
        if not remaining:
            break
    return c.execute("INSERT INTO land(player_id,acres) VALUES (?,?)",
                     (pid, acres)).lastrowid


def ensure_player(c, pid):
    c.execute("INSERT OR IGNORE INTO accounts(player_id) VALUES (?)", (pid,))
    c.executemany("INSERT OR IGNORE INTO inventory(player_id,symbol) VALUES (?,?)",
                  ((pid, symbol) for symbol in catalog.SYMBOLS))
    c.executemany("INSERT OR IGNORE INTO skills(player_id,name) VALUES (?,?)",
                  ((pid, name) for name in ("mining", "crafting")))


def _currency(currency):
    if currency not in ("fiat", "btc"):
        raise ValueError("currency must be fiat or btc")
    return currency


def debit(c, pid, currency, amount):
    currency = _currency(currency)
    amount = integer(amount, "amount", 0)
    if c.execute(f"UPDATE accounts SET {currency}={currency}-? "
                 f"WHERE player_id=? AND {currency}>=?", (amount, pid, amount)).rowcount != 1:
        raise ValueError("Insufficient funds or unknown player")


def credit(c, pid, currency, amount):
    currency = _currency(currency)
    amount = integer(amount, "amount", 0)
    if c.execute(f"UPDATE accounts SET {currency}={currency}+? "
                 f"WHERE player_id=? AND {currency}<=?",
                 (amount, pid, MAX_INTEGER - amount)).rowcount != 1:
        raise ValueError("Unknown player or balance overflow")


def change_resource(c, pid, symbol, delta):
    """Debit exactly; credits stop at the storage cap and discard any excess."""
    if symbol not in catalog.SYMBOLS:
        raise ValueError("Unknown element")
    if isinstance(delta, bool) or not isinstance(delta, int) or abs(delta) > MAX_INTEGER:
        raise ValueError("Resource delta must be a bounded integer")
    if delta >= 0:
        changed = c.execute(
            "UPDATE inventory SET quantity=CASE WHEN quantity>? THEN ? ELSE quantity+? END "
            "WHERE player_id=? AND symbol=?",
            (MAX_INTEGER - delta, MAX_INTEGER, delta, pid, symbol)
        ).rowcount
    else:
        changed = c.execute(
            "UPDATE inventory SET quantity=quantity+? "
            "WHERE player_id=? AND symbol=? AND quantity>=?",
            (delta, pid, symbol, -delta)
        ).rowcount
    if changed != 1:
        raise ValueError("Insufficient resource or unknown player")


def effective_hash(c):
    """TH/s; oldest enabled ASICs get power first. Sunlight always on for now."""
    result = {}
    power = {}
    for row in c.execute("SELECT * FROM equipment WHERE installed=1 AND enabled=1 AND locked=0 ORDER BY id"):
        pid = row["player_id"]
        if row["kind"] == "solar_panel":
            power[pid] = power.get(pid, 0) + row["power_watts"]
    for row in c.execute("SELECT * FROM equipment WHERE kind='asic' AND installed=1 AND enabled=1 AND locked=0 ORDER BY id"):
        pid = row["player_id"]
        if power.get(pid, 0) >= row["power_watts"]:
            power[pid] -= row["power_watts"]
            result[pid] = result.get(pid, 0) + row["hashrate"]
    return result


def _xp(c, pid, name, amount):
    amount = integer(amount, "xp", 0)
    row = c.execute("SELECT xp FROM skills WHERE player_id=? AND name=?", (pid, name)).fetchone()
    integer(row[0] + amount, "xp", 0)
    c.execute("UPDATE skills SET xp=xp+? WHERE player_id=? AND name=?", (amount, pid, name))


def _droid(c, pid):
    item_id = c.execute(
        "INSERT INTO equipment(player_id,kind,hashrate) VALUES (?,'droid',0)", (pid,)
    ).lastrowid
    return c.execute("INSERT INTO workers(player_id,kind,equipment_id) VALUES (?,'droid',?)",
                     (pid, item_id)).lastrowid


def _queue_craft(c, pid, recipe_id):
    recipe = RECIPES[recipe_id]
    now = c.execute("SELECT last_tick FROM chain WHERE id=1").fetchone()[0]
    return c.execute("""INSERT INTO jobs(player_id,recipe_id,started_at,completes_at,recipe_json)
                        VALUES (?,?,?,?,?)""",
                     (pid, recipe_id, now, integer(now + integer(recipe["duration"]), "job end"),
                      json.dumps(recipe, sort_keys=True))).lastrowid


def _deposit_gather(c, pid, mass, weights, total):
    for symbol, weight in zip(catalog.SYMBOLS, weights):
        carry = c.execute("SELECT carry FROM inventory WHERE player_id=? AND symbol=?",
                          (pid, symbol)).fetchone()[0]
        # Fractional micrograms stay in carry; partitioning ticks loses no mass.
        quantity, remainder = divmod(mass * weight + carry, total)
        change_resource(c, pid, symbol, min(quantity, MAX_INTEGER))
        c.execute("UPDATE inventory SET carry=? WHERE player_id=? AND symbol=?",
                  (remainder, pid, symbol))


def _gather(c, start, end):
    if end == start:
        return
    weights = [integer(catalog.ABUNDANCE[s], "abundance", 0) for s in catalog.SYMBOLS]
    total = sum(weights)
    if not total:
        raise ValueError("Crust abundance must have positive total weight")
    for row in c.execute("""SELECT w.player_id,count(*) AS count FROM workers w
                           JOIN land l ON w.land_id=l.id AND w.player_id=l.player_id
                           WHERE w.locked=0 AND l.locked=0 AND l.planet='Earth'
                           AND (w.equipment_id IS NULL OR EXISTS (
                               SELECT 1 FROM equipment e WHERE e.id=w.equipment_id
                               AND e.player_id=w.player_id AND e.locked=0))
                           GROUP BY w.player_id""").fetchall():
        pid, count = row["player_id"], row["count"]
        mass = GATHER_RATE * (end - start) * count
        _deposit_gather(c, pid, mass, weights, total)
        _xp(c, pid, "mining", (end - start) * count)


def _gather_bank_workers(c, start, end):
    shifts = c.execute(
        "SELECT player_id,started_at,completes_at FROM bank_work_shifts "
        "WHERE status='running' AND started_at<? AND completes_at>?",
        (end, start)
    ).fetchall()
    seconds = 0
    for shift in shifts:
        worked = max(0, min(end, shift["completes_at"]) - max(start, shift["started_at"]))
        if worked:
            seconds += worked
            _xp(c, shift["player_id"], "mining", worked)
    if not seconds:
        return
    bank_id = earth_bank_id(c)
    mass = GATHER_RATE * seconds
    weights = [integer(catalog.ABUNDANCE[s], "abundance", 0) for s in catalog.SYMBOLS]
    total = sum(weights)
    _deposit_gather(c, bank_id, mass, weights, total)


def _complete_bank_shift(c, shift):
    worked = shift["completes_at"] - shift["started_at"]
    employment = c.execute(
        "SELECT wage_remainder FROM bank_employment WHERE player_id=?",
        (shift["player_id"],)
    ).fetchone()
    amount, remainder = divmod(
        worked * shift["wage_numerator"] + employment["wage_remainder"],
        shift["wage_denominator"]
    )
    credit(c, shift["player_id"], "fiat", amount)
    c.execute("UPDATE bank_employment SET wage_remainder=? WHERE player_id=?",
              (remainder, shift["player_id"]))
    c.execute("UPDATE bank_work_shifts SET status='completed',wage_paid=? WHERE id=?",
              (amount, shift["id"]))


def advance(c, start, end):
    """Gather and finish up to JOB_BATCH simulation events; return False if more are due."""
    start = integer(start, "start", 0)
    end = integer(end, "end", start)
    cursor = start
    jobs = c.execute("SELECT * FROM jobs WHERE status='running' AND completes_at<=? "
                     "ORDER BY completes_at,id LIMIT ?", (end, JOB_BATCH)).fetchall()
    shifts = c.execute(
        "SELECT * FROM bank_work_shifts WHERE status='running' AND completes_at<=? "
        "ORDER BY completes_at,id LIMIT ?", (end, JOB_BATCH)
    ).fetchall()
    events = sorted(
        [(row["completes_at"], "job", row) for row in jobs]
        + [(row["completes_at"], "shift", row) for row in shifts],
        key=lambda event: (event[0], event[1], event[2]["id"])
    )[:JOB_BATCH]
    for completes_at, event_type, event in events:
        boundary = max(cursor, completes_at)
        _gather(c, cursor, boundary)
        _gather_bank_workers(c, cursor, boundary)
        cursor = boundary
        if event_type == "shift":
            _complete_bank_shift(c, event)
        else:
            recipe = json.loads(event["recipe_json"])
            if recipe["output"] == "droid":
                worker_id = _droid(c, event["player_id"])
                c.execute("UPDATE droid_tutorial SET worker_id=? WHERE job_id=?",
                          (worker_id, event["id"]))
            else:
                stats = recipe["stats"]
                c.execute("INSERT INTO equipment(player_id,kind,hashrate,power_watts) VALUES (?,?,?,?)",
                          (event["player_id"], recipe["output"], stats["hashrate"], stats["power_watts"]))
            _xp(c, event["player_id"], "crafting", recipe["xp"])
            c.execute("UPDATE jobs SET status='completed' WHERE id=?", (event["id"],))
    _gather(c, cursor, end)
    _gather_bank_workers(c, cursor, end)
    pending = c.execute(
        "SELECT 1 FROM jobs WHERE status='running' AND completes_at<=? "
        "UNION ALL SELECT 1 FROM bank_work_shifts WHERE status='running' AND completes_at<=? "
        "LIMIT 1", (end, end)
    ).fetchone()
    return pending is None


class World:
    def __init__(self, clock=time.time):
        self.clock = clock

    @contextmanager
    def _transaction(self, pid):
        with database._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            import simulation
            simulation.prepare(c, int(self.clock()))
            if not c.execute("SELECT 1 FROM players WHERE id=?", (pid,)).fetchone():
                raise ValueError("Unknown player")
            ensure_player(c, pid)
            yield c

    def snapshot(self, pid):
        with self._transaction(pid) as c:
            account = dict(c.execute("SELECT * FROM accounts WHERE player_id=?", (pid,)).fetchone())
            result = {"accounts": account, "balances": {"fiat": account["fiat"], "btc": account["btc"]}}
            result["inventory"] = dict(c.execute("SELECT symbol,quantity FROM inventory WHERE player_id=?", (pid,)))
            result["inventory_carry"] = dict(c.execute("SELECT symbol,carry FROM inventory WHERE player_id=?", (pid,)))
            for table in ("land", "workers", "equipment", "skills", "jobs"):
                result[table] = [dict(row) for row in c.execute(f"SELECT * FROM {table} WHERE player_id=?", (pid,))]
            for skill in result["skills"]:
                skill["level"] = catalog.level(skill["xp"])
            result["effective_hash"] = effective_hash(c).get(pid, 0)
            bank_id = earth_bank_id(c)
            result["available_land"] = c.execute(
                "SELECT coalesce(sum(acres),0) FROM land WHERE player_id=?",
                (bank_id,)
            ).fetchone()[0]
            return result

    def start_bank_shift(self, pid):
        with self._transaction(pid) as c:
            if c.execute(
                "SELECT 1 FROM bank_work_shifts WHERE player_id=? AND status='running'",
                (pid,)
            ).fetchone():
                raise ValueError("A Bank work shift is already active")
            initial = c.execute(
                "SELECT initial_fiat_supply FROM world_economy WHERE id=1"
            ).fetchone()[0]
            supply = fiat_supply(c)
            numerator = integer(BANK_WORK_WAGE * (initial + supply), "wage numerator")
            denominator = integer(2 * initial, "wage denominator")
            now = c.execute("SELECT last_tick FROM chain WHERE id=1").fetchone()[0]
            completes_at = integer(now + BANK_WORK_SHIFT, "shift end")
            c.execute("INSERT OR IGNORE INTO bank_employment(player_id) VALUES (?)", (pid,))
            shift_id = c.execute(
                "INSERT INTO bank_work_shifts(player_id,started_at,completes_at,"
                "wage_numerator,wage_denominator) VALUES (?,?,?,?,?)",
                (pid, now, completes_at, numerator, denominator)
            ).lastrowid
            return {
                "id": shift_id,
                "started_at": now,
                "completes_at": completes_at,
                "wage_numerator": numerator,
                "wage_denominator": denominator,
            }

    def bank_work_status(self, pid):
        with self._transaction(pid) as c:
            shift = c.execute(
                "SELECT id,started_at,completes_at,wage_numerator,wage_denominator "
                "FROM bank_work_shifts WHERE player_id=? AND status='running'",
                (pid,)
            ).fetchone()
            return dict(shift) if shift is not None else None

    def buy_land(self, pid, acres):
        raise ValueError("Earth land is sold through Earth Bank auctions; use auction bid")

    def sell_land(self, pid, land_id):
        land_id = integer(land_id, "land_id")
        with self._transaction(pid) as c:
            row = c.execute("SELECT acres FROM land WHERE id=? AND player_id=? AND locked=0",
                            (land_id, pid)).fetchone()
            if row is None or c.execute("SELECT 1 FROM workers WHERE land_id=?", (land_id,)).fetchone():
                raise ValueError("Land is unavailable or has assigned workers")
            bank_id = earth_bank_id(c)
            debit(c, bank_id, "fiat", row["acres"] * LAND_PRICE)
            credit(c, pid, "fiat", row["acres"] * LAND_PRICE)
            c.execute("UPDATE land SET player_id=? WHERE id=?", (bank_id, land_id))

    def assign(self, pid, worker_id, land_id):
        worker_id = integer(worker_id, "worker_id")
        if land_id is not None:
            land_id = integer(land_id, "land_id")
        with self._transaction(pid) as c:
            worker = c.execute("SELECT * FROM workers WHERE id=? AND player_id=? AND locked=0",
                               (worker_id, pid)).fetchone()
            if worker is None:
                raise ValueError("Worker is unavailable")
            if worker["equipment_id"] is not None and not c.execute(
                    "SELECT 1 FROM equipment WHERE id=? AND player_id=? AND locked=0",
                    (worker["equipment_id"], pid)).fetchone():
                raise ValueError("Droid is unavailable")
            if land_id is not None:
                land = c.execute("SELECT acres FROM land WHERE id=? AND player_id=? AND locked=0",
                                 (land_id, pid)).fetchone()
                occupied = c.execute("SELECT count(*) FROM workers WHERE land_id=? AND id!=?",
                                     (land_id, worker_id)).fetchone()[0]
                if land is None or occupied >= land["acres"]:
                    raise ValueError("Land is unavailable or full")
            c.execute("UPDATE workers SET land_id=? WHERE id=?", (land_id, worker_id))
            if land_id is not None:
                c.execute(
                    "UPDATE droid_tutorial SET completed=1 WHERE player_id=? AND worker_id=?",
                    (pid, worker_id)
                )

    def craft(self, pid, recipe_id):
        if not isinstance(recipe_id, str) or recipe_id not in RECIPES:
            raise ValueError("Unknown recipe")
        recipe = RECIPES[recipe_id]
        with self._transaction(pid) as c:
            for symbol, amount in recipe["inputs"].items():
                change_resource(c, pid, symbol, -integer(amount))
            return _queue_craft(c, pid, recipe_id)

    def equipment(self, pid, item_id, installed, enabled):
        item_id = integer(item_id, "item_id")
        if type(installed) is not bool or type(enabled) is not bool:
            raise ValueError("installed and enabled must be booleans")
        if enabled and not installed:
            raise ValueError("Enabled equipment must be installed")
        with self._transaction(pid) as c:
            item = c.execute("SELECT kind FROM equipment WHERE id=? AND player_id=? AND locked=0",
                             (item_id, pid)).fetchone()
            if item is None:
                raise ValueError("Equipment is unavailable")
            if item["kind"] == "droid":
                raise ValueError("Use worker assignments for droids")
            if c.execute("""UPDATE equipment SET installed=?,enabled=?
                            WHERE id=? AND player_id=? AND locked=0""",
                         (installed, enabled, item_id, pid)).rowcount != 1:
                raise ValueError("Equipment is unavailable")

    def set_enabled(self, pid, item_id, enabled):
        """Toggle only enabled, reading installation state under the write lock."""
        item_id = integer(item_id, "item_id")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._transaction(pid) as c:
            item = c.execute(
                "SELECT kind,installed FROM equipment WHERE id=? AND player_id=? AND locked=0",
                (item_id, pid)).fetchone()
            if item is None:
                raise ValueError("Equipment is unavailable")
            if item["kind"] == "droid":
                raise ValueError("Use worker assignments for droids")
            if enabled and not item["installed"]:
                raise ValueError("Enabled equipment must be installed")
            c.execute("UPDATE equipment SET enabled=? WHERE id=?", (enabled, item_id))

    def seed(self, pid, *, fiat=0, btc=0, resources=None, acres=0, humans=0, droids=0,
             solar_panels=0, asics=0):
        """Explicit development-only grant; normal signup never calls this."""
        counts = {}
        for name, value in (("human", humans), ("droid", droids),
                            ("solar_panel", solar_panels), ("asic", asics)):
            count = integer(value, name, 0)
            if count > MAX_SEED_COUNT:
                raise ValueError(f"Seed {name} count cannot exceed {MAX_SEED_COUNT}")
            counts[name] = count
        with self._transaction(pid) as c:
            credit(c, pid, "fiat", fiat)
            credit(c, pid, "btc", btc)
            acres = integer(acres, "acres", 0)
            if acres:
                _seed_land(c, pid, acres)
            for symbol, quantity in (resources or {}).items():
                change_resource(c, pid, symbol, integer(quantity, minimum=0))
            for kind in ("human", "droid"):
                for _ in range(counts[kind]):
                    if kind == "droid":
                        _droid(c, pid)
                    else:
                        c.execute("INSERT INTO workers(player_id,kind) VALUES (?,?)", (pid, kind))
            for kind in ("solar_panel", "asic"):
                for _ in range(counts[kind]):
                    c.execute("INSERT INTO equipment(player_id,kind) VALUES (?,?)", (pid, kind))
