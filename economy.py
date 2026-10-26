"""Integer Earth economy. Call advance only inside a simulation transaction."""

import json
import re
import time
from contextlib import contextmanager
from decimal import Decimal, ROUND_FLOOR, localcontext
from pathlib import Path

import catalog
import database

LAND_SUPPLY = 36800000000
LAND_PRICE = 1000000
GATHER_RATE = 1000  # Provisional 1 mg per second, human and droid alike.
RECIPES = json.loads(Path(__file__).with_name("recipes.json").read_text())["recipes"]
MAX_INTEGER = 2 ** 63 - 1
MAX_SEED_COUNT = 10000
JOB_BATCH = 1500


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
    """Schema v4, inside database's locked, rollback-safe migration."""
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
    if symbol not in catalog.SYMBOLS:
        raise ValueError("Unknown element")
    if isinstance(delta, bool) or not isinstance(delta, int) or abs(delta) > MAX_INTEGER:
        raise ValueError("Resource delta must be a bounded integer")
    if c.execute("""UPDATE inventory SET quantity=quantity+?
                    WHERE player_id=? AND symbol=? AND quantity+? BETWEEN 0 AND ?""",
                 (delta, pid, symbol, delta, MAX_INTEGER)).rowcount != 1:
        raise ValueError("Insufficient resource, overflow, or unknown player")


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
        for symbol, weight in zip(catalog.SYMBOLS, weights):
            carry = c.execute("SELECT carry FROM inventory WHERE player_id=? AND symbol=?",
                              (pid, symbol)).fetchone()[0]
            # Fractional micrograms stay in carry; partitioning ticks loses no mass.
            quantity, remainder = divmod(mass * weight + carry, total)
            change_resource(c, pid, symbol, quantity)
            c.execute("UPDATE inventory SET carry=? WHERE player_id=? AND symbol=?",
                      (remainder, pid, symbol))
        _xp(c, pid, "mining", (end - start) * count)


def advance(c, start, end):
    """Gather and finish up to JOB_BATCH jobs; return False if more are due."""
    start = integer(start, "start", 0)
    end = integer(end, "end", start)
    cursor = start
    jobs = c.execute("SELECT * FROM jobs WHERE status='running' AND completes_at<=? "
                     "ORDER BY completes_at,id LIMIT ?", (end, JOB_BATCH)).fetchall()
    for job in jobs:
        boundary = max(cursor, job["completes_at"])
        _gather(c, cursor, boundary)
        cursor = boundary
        recipe = json.loads(job["recipe_json"])
        if recipe["output"] == "droid":
            _droid(c, job["player_id"])
        else:
            stats = recipe["stats"]
            c.execute("INSERT INTO equipment(player_id,kind,hashrate,power_watts) VALUES (?,?,?,?)",
                      (job["player_id"], recipe["output"], stats["hashrate"], stats["power_watts"]))
        _xp(c, job["player_id"], "crafting", recipe["xp"])
        c.execute("UPDATE jobs SET status='completed' WHERE id=?", (job["id"],))
    _gather(c, cursor, end)
    return c.execute(
        "SELECT 1 FROM jobs WHERE status='running' AND completes_at<=? LIMIT 1",
        (end,)).fetchone() is None


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
            result["available_land"] = c.execute(
                "SELECT total_acres-(SELECT coalesce(sum(acres),0) FROM land WHERE planet='Earth') "
                "FROM planets WHERE name='Earth'").fetchone()[0]
            return result

    def buy_land(self, pid, acres):
        acres = integer(acres, "acres")
        with self._transaction(pid) as c:
            supply = c.execute("SELECT total_acres FROM planets WHERE name='Earth'").fetchone()[0]
            used = c.execute("SELECT coalesce(sum(acres),0) FROM land WHERE planet='Earth'").fetchone()[0]
            if acres > supply - used:
                raise ValueError("Not enough Earth land")
            debit(c, pid, "fiat", acres * LAND_PRICE)
            return c.execute("INSERT INTO land(player_id,acres) VALUES (?,?)", (pid, acres)).lastrowid

    def sell_land(self, pid, land_id):
        land_id = integer(land_id, "land_id")
        with self._transaction(pid) as c:
            row = c.execute("SELECT acres FROM land WHERE id=? AND player_id=? AND locked=0",
                            (land_id, pid)).fetchone()
            if row is None or c.execute("SELECT 1 FROM workers WHERE land_id=?", (land_id,)).fetchone():
                raise ValueError("Land is unavailable or has assigned workers")
            credit(c, pid, "fiat", row["acres"] * LAND_PRICE)
            c.execute("DELETE FROM land WHERE id=?", (land_id,))

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

    def craft(self, pid, recipe_id):
        if not isinstance(recipe_id, str) or recipe_id not in RECIPES:
            raise ValueError("Unknown recipe")
        recipe = RECIPES[recipe_id]
        with self._transaction(pid) as c:
            for symbol, amount in recipe["inputs"].items():
                change_resource(c, pid, symbol, -integer(amount))
            now = c.execute("SELECT last_tick FROM chain WHERE id=1").fetchone()[0]
            return c.execute("""INSERT INTO jobs(player_id,recipe_id,started_at,completes_at,recipe_json)
                                VALUES (?,?,?,?,?)""",
                             (pid, recipe_id, now, now + integer(recipe["duration"]),
                              json.dumps(recipe, sort_keys=True))).lastrowid

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
                supply = c.execute("SELECT total_acres FROM planets WHERE name='Earth'").fetchone()[0]
                used = c.execute("SELECT coalesce(sum(acres),0) FROM land WHERE planet='Earth'").fetchone()[0]
                if acres > supply - used:
                    raise ValueError("Not enough Earth land")
                c.execute("INSERT INTO land(player_id,acres) VALUES (?,?)", (pid, acres))
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
