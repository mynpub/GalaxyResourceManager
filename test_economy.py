"""Focused Earth economy, integer accounting, and v5 migration regression tests."""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import catalog
import database
import economy


class EconomyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        data = patch.object(database, "DATA_DIR", self.directory)
        data.start()
        self.addCleanup(data.stop)
        database.init_database()
        with database._connection() as c:
            self.pid = c.execute(
                "INSERT INTO players(username,password_hash,created_at) VALUES ('tester','unused','date')"
            ).lastrowid
            economy.ensure_player(c, self.pid)
        self.now = 1000
        self.world = economy.World(lambda: self.now)

    def test_new_player_zero_and_complete_catalog(self):
        snapshot = self.world.snapshot(self.pid)
        self.assertEqual(len(catalog.SYMBOLS), 118)
        self.assertEqual(len(set(catalog.SYMBOLS)), 118)
        self.assertEqual(snapshot["balances"], {"fiat": 0, "btc": 0})
        self.assertEqual(snapshot["inventory"], dict.fromkeys(catalog.SYMBOLS, 0))
        self.assertEqual(snapshot["workers"], [])
        self.assertEqual(snapshot["equipment"], [])
        self.assertEqual(snapshot["available_land"], economy.LAND_SUPPLY)
        with database._connection() as c:
            bank_id = economy.earth_bank_id(c)
            self.assertEqual(c.execute(
                "SELECT sum(acres) FROM land WHERE player_id=?", (bank_id,)
            ).fetchone()[0], economy.LAND_SUPPLY)
            self.assertEqual(c.execute(
                "SELECT fiat FROM accounts WHERE player_id=?", (bank_id,)
            ).fetchone()[0], economy.BANK_INITIAL_FIAT)
            self.assertEqual(c.execute(
                "SELECT initial_fiat_supply FROM world_economy WHERE id=1"
            ).fetchone()[0], economy.BANK_INITIAL_FIAT)
            self.assertEqual(snapshot["land"], [])
        with self.assertRaises(ValueError):
            self.world.buy_land(self.pid, 1)
        new_id = database.create_player("newcomer", "a long test password")
        self.assertEqual(self.world.snapshot(new_id)["balances"], {"fiat": 0, "btc": 0})
        self.assertEqual(self.world.snapshot(new_id)["inventory"], dict.fromkeys(catalog.SYMBOLS, 0))

    def test_signup_does_not_start_chain_clock(self):
        with database._connection() as c:
            before = tuple(c.execute("SELECT epoch,last_tick,height FROM chain").fetchone())
        database.create_player("newcomer", "a long test password")
        with database._connection() as c:
            after = tuple(c.execute("SELECT epoch,last_tick,height FROM chain").fetchone())
        self.assertEqual(after, before)
        self.assertEqual(after[2], 0)

    def test_bank_shift_pays_player_and_lists_bank_resources(self):
        shift = self.world.start_bank_shift(self.pid)
        self.assertEqual(shift["wage_numerator"], shift["wage_denominator"])
        with self.assertRaisesRegex(ValueError, "already active"):
            self.world.start_bank_shift(self.pid)
        self.now += economy.BANK_WORK_SHIFT
        snapshot = self.world.snapshot(self.pid)

        with database._connection() as c:
            bank_id = economy.earth_bank_id(c)
            self.assertEqual(snapshot["balances"]["fiat"], economy.BANK_WORK_SHIFT)
            self.assertEqual(sum(snapshot["inventory"].values()), 0)
            self.assertEqual(c.execute(
                "SELECT xp FROM skills WHERE player_id=? AND name='mining'", (self.pid,)
            ).fetchone()[0], economy.BANK_WORK_SHIFT)
            bank = c.execute(
                "SELECT sum(quantity),sum(carry) FROM inventory WHERE player_id=?", (bank_id,)
            ).fetchone()
            listed = c.execute(
                "SELECT sum(quantity) FROM auctions WHERE seller=? AND kind='resource' "
                "AND status='active'", (bank_id,)
            ).fetchone()[0]
            listings = c.execute(
                "SELECT asset,currency,reserve,closes FROM auctions WHERE seller=? "
                "AND kind='resource' AND status='active'", (bank_id,)
            ).fetchall()
            self.assertTrue(listings)
            self.assertEqual(len({row["asset"] for row in listings}), len(listings))
            self.assertTrue(all(
                row["currency"] == "fiat" and row["reserve"] == 1
                and row["closes"] == self.now + economy.EARTH_BANK_AUCTION_DURATION
                for row in listings
            ))
            weight_total = sum(catalog.ABUNDANCE.values())
            self.assertEqual(bank["sum(quantity)"] * weight_total + bank["sum(carry)"]
                             + listed * weight_total,
                             economy.GATHER_RATE * economy.BANK_WORK_SHIFT * weight_total)
            self.assertEqual(
                c.execute("SELECT fiat FROM accounts WHERE player_id=?", (bank_id,)).fetchone()[0],
                economy.BANK_INITIAL_FIAT
            )
        self.assertIsNone(self.world.bank_work_status(self.pid))

    def test_bank_shift_locks_half_inflation_wage_at_start(self):
        with database._connection() as c:
            economy.credit(c, self.pid, "fiat", economy.BANK_INITIAL_FIAT)
        shift = self.world.start_bank_shift(self.pid)
        self.assertEqual(shift["wage_numerator"] * 2, shift["wage_denominator"] * 3)
        with database._connection() as c:
            economy.credit(c, self.pid, "fiat", economy.BANK_INITIAL_FIAT)
        self.now += economy.BANK_WORK_SHIFT
        self.world.snapshot(self.pid)
        with database._connection() as c:
            self.assertEqual(c.execute(
                "SELECT wage_paid FROM bank_work_shifts WHERE id=?", (shift["id"],)
            ).fetchone()[0], economy.BANK_WORK_SHIFT * 3 // 2)

    def test_strict_integer_and_accounting(self):
        for value in (True, False, 1.0, 1.2, "1.0", "1e2", " 1", None, -1, 2 ** 63):
            with self.subTest(value=value), self.assertRaises(ValueError):
                economy.integer(value)
        self.assertEqual(economy.integer(0, minimum=0), 0)
        self.assertEqual(economy.integer("123"), 123)
        with database._connection() as c:
            economy.credit(c, self.pid, "fiat", 10)
            economy.debit(c, self.pid, "fiat", 4)
            for currency, amount in (("fiat", 7), ("fiat", -1), ("bitcoin", 1)):
                with self.assertRaises(ValueError):
                    economy.debit(c, self.pid, currency, amount)
            self.assertEqual(c.execute(
                "SELECT fiat FROM accounts WHERE player_id=?", (self.pid,)
            ).fetchone()[0], 6)
            with self.assertRaises(ValueError):
                economy.credit(c, self.pid, "fiat", economy.MAX_INTEGER)
            for field, value in (("fiat", -1), ("btc", 0.5)):
                with self.assertRaises(sqlite3.IntegrityError):
                    c.execute(f"UPDATE accounts SET {field}=?", (value,))

    def test_land_worker_capacity_ownership_and_escrow(self):
        self.world.seed(self.pid, fiat=3000000, humans=2, acres=1)
        land = self.world.snapshot(self.pid)["land"][0]["id"]
        workers = self.world.snapshot(self.pid)["workers"]
        self.world.assign(self.pid, workers[0]["id"], land)
        self.world.assign(self.pid, workers[0]["id"], land)
        with self.assertRaises(ValueError):
            self.world.assign(self.pid, workers[1]["id"], land)
        with self.assertRaises(ValueError):
            self.world.sell_land(self.pid, land)
        self.world.assign(self.pid, workers[0]["id"], None)
        with database._connection() as c:
            c.execute("UPDATE land SET locked=1 WHERE id=?", (land,))
        with self.assertRaises(ValueError):
            self.world.sell_land(self.pid, land)
        with self.assertRaises(ValueError):
            self.world.assign(self.pid, workers[0]["id"], land)
        with database._connection() as c:
            c.execute("UPDATE land SET locked=0 WHERE id=?", (land,))
            bank_id = economy.earth_bank_id(c)
            economy.credit(c, bank_id, "fiat", economy.LAND_PRICE)
        self.world.sell_land(self.pid, land)
        self.assertEqual(self.world.snapshot(self.pid)["balances"]["fiat"],
                         3000000 + economy.LAND_PRICE)

    def test_seed_transfers_existing_supply_from_earth_bank(self):
        self.world.seed(self.pid, acres=2)
        with database._connection() as c:
            bank_id = economy.earth_bank_id(c)
            bank_acres = c.execute(
                "SELECT coalesce(sum(acres),0) FROM land WHERE player_id=?", (bank_id,)
            ).fetchone()[0]
            world_acres = c.execute(
                "SELECT coalesce(sum(acres),0) FROM land"
            ).fetchone()[0]
        self.assertEqual(bank_acres + 2, economy.LAND_SUPPLY)
        self.assertEqual(world_acres, economy.LAND_SUPPLY)

    def test_gather_partition_invariant_and_exact_fractional_mass(self):
        self.world.seed(self.pid, fiat=1000000, humans=1, acres=1)
        land = self.world.snapshot(self.pid)["land"][0]["id"]
        worker = self.world.snapshot(self.pid)["workers"][0]["id"]
        self.world.assign(self.pid, worker, land)
        with database._connection() as c:
            economy.advance(c, 1000, 1001)
            economy.advance(c, 1001, 1007)
            split = [tuple(r) for r in c.execute(
                "SELECT symbol,quantity,carry FROM inventory ORDER BY symbol")]
            mining_xp = c.execute(
                "SELECT xp FROM skills WHERE player_id=? AND name='mining'",
                (self.pid,)
            ).fetchone()[0]
            self.assertEqual(mining_xp, 7)
            total = sum(catalog.ABUNDANCE.values())
            self.assertEqual(sum(quantity * total + carry for _, quantity, carry in split),
                             7 * economy.GATHER_RATE * total)
            c.execute("UPDATE inventory SET quantity=0,carry=0")
            economy.advance(c, 1000, 1007)
            self.assertEqual(split, [tuple(r) for r in c.execute(
                "SELECT symbol,quantity,carry FROM inventory ORDER BY symbol")])
            for symbol, quantity, carry in split:
                if catalog.ABUNDANCE[symbol] == 0:
                    self.assertEqual((quantity, carry), (0, 0))
            c.execute("UPDATE land SET locked=1")
            economy.advance(c, 1007, 1008)
            self.assertEqual(split, [tuple(r) for r in c.execute(
                "SELECT symbol,quantity,carry FROM inventory ORDER BY symbol")])

    def test_skill_curve_exact_twenty_year_target(self):
        self.assertEqual(catalog.level(0), 1)
        self.assertEqual(catalog.level(catalog.MINING_LEVEL_99_SECONDS - 1), 98)
        self.assertEqual(catalog.level(catalog.MINING_LEVEL_99_SECONDS), 99)
        self.assertEqual(catalog.level(catalog.XP_THRESHOLDS[-1] * 100), 999)
        self.assertEqual(sorted(catalog.XP_THRESHOLDS), list(catalog.XP_THRESHOLDS))

    def test_recipes_atomic_consumption_completion_once_and_droid_worker(self):
        recipe = economy.RECIPES["solar_panel"]
        self.world.seed(self.pid, resources={"Si": recipe["inputs"]["Si"]})
        before = self.world.snapshot(self.pid)["inventory"]
        with self.assertRaises(ValueError):
            self.world.craft(self.pid, "solar_panel")
        self.assertEqual(before, self.world.snapshot(self.pid)["inventory"])
        self.world.seed(self.pid, resources={symbol: mass for symbol, mass in recipe["inputs"].items()
                                           if symbol != "Si"})
        job_id = self.world.craft(self.pid, "solar_panel")
        self.assertEqual(self.world.snapshot(self.pid)["inventory"]["Si"], 0)
        self.now += recipe["duration"] - 1
        self.assertEqual(self.world.snapshot(self.pid)["equipment"], [])
        self.now += 1
        snapshot = self.world.snapshot(self.pid)
        self.assertEqual(len(snapshot["equipment"]), 1)
        self.assertEqual(snapshot["jobs"][0]["id"], job_id)
        self.assertEqual(snapshot["jobs"][0]["status"], "completed")
        self.assertEqual(len(self.world.snapshot(self.pid)["equipment"]), 1)
        self.world.seed(self.pid, resources=economy.RECIPES["droid"]["inputs"])
        self.world.craft(self.pid, "droid")
        self.now += economy.RECIPES["droid"]["duration"]
        snapshot = self.world.snapshot(self.pid)
        self.assertEqual([w["kind"] for w in snapshot["workers"]], ["droid"])
        self.assertEqual(snapshot["workers"][0]["land_id"], None)
        self.assertEqual(len(snapshot["equipment"]), 2)
        droid = next(item for item in snapshot["equipment"] if item["kind"] == "droid")
        self.assertEqual(snapshot["workers"][0]["equipment_id"], droid["id"])

    def test_recipe_is_frozen_when_job_starts(self):
        self.world.seed(self.pid, resources=economy.RECIPES["asic"]["inputs"])
        self.world.craft(self.pid, "asic")
        changed = dict(economy.RECIPES["asic"], output="solar_panel")
        with patch.dict(economy.RECIPES, {"asic": changed}):
            self.now += 120
            self.assertEqual(self.world.snapshot(self.pid)["equipment"][0]["kind"], "asic")

    def test_one_worker_gathers_asic_and_solar_panel_inputs_in_sixty_seconds(self):
        self.world.seed(self.pid, humans=1, acres=1)
        snapshot = self.world.snapshot(self.pid)
        self.world.assign(self.pid, snapshot["workers"][0]["id"], snapshot["land"][0]["id"])
        for recipe_id in ("asic", "solar_panel"):
            with database._connection() as c:
                c.execute("UPDATE inventory SET quantity=0,carry=0 WHERE player_id=?", (self.pid,))
            if recipe_id == "asic":
                # ASIC gold is the bottleneck that sets the gathering rate.
                self.now += 59
                with self.assertRaises(ValueError):
                    self.world.craft(self.pid, recipe_id)
                self.now += 1
            else:
                self.now += 60
            self.world.craft(self.pid, recipe_id)

    def test_resource_credits_stop_at_storage_cap_without_pausing_simulation(self):
        self.world.seed(self.pid, humans=1, acres=1)
        snapshot = self.world.snapshot(self.pid)
        self.world.assign(self.pid, snapshot["workers"][0]["id"], snapshot["land"][0]["id"])
        with database._connection() as c:
            c.execute("UPDATE inventory SET quantity=? WHERE player_id=? AND symbol='O'",
                      (economy.MAX_INTEGER - 1, self.pid))
            economy.change_resource(c, self.pid, "O", economy.MAX_INTEGER)
            self.assertEqual(c.execute(
                "SELECT quantity FROM inventory WHERE player_id=? AND symbol='O'", (self.pid,)
            ).fetchone()[0], economy.MAX_INTEGER)
        # A week of offline gathering would exceed the cap for oxygen.
        self.now += 7 * 86400
        import simulation
        while not simulation.tick(lambda: self.now):
            pass
        inventory = self.world.snapshot(self.pid)["inventory"]
        self.assertEqual(inventory["O"], economy.MAX_INTEGER)
        self.assertGreater(inventory["Au"], 0)

    def test_accepted_recipe_inputs_and_reachable_natural_resources(self):
        expected = {
            "solar_panel": {"Si": 8000000000, "Al": 6000000000, "Cu": 2000000000, "Ag": 20000000},
            "asic": {"Si": 1500000000, "Cu": 3000000000, "Al": 2000000000,
                     "Au": 8000000, "Sn": 150000000, "Ni": 500000000},
            "droid": {"Fe": 18000000000, "Al": 6000000000, "Cu": 4000000000,
                      "Si": 600000000, "Ni": 1200000000, "Li": 300000000, "Co": 250000000},
        }
        for recipe_id, inputs in expected.items():
            self.assertEqual(economy.RECIPES[recipe_id]["inputs"], inputs)
            for symbol in inputs:
                self.assertGreater(catalog.ABUNDANCE[symbol], 0)
        self.assertEqual(catalog.ABUNDANCE["Au"], 4)
        self.assertEqual(catalog.ABUNDANCE["Ag"], 75)

    def test_zero_length_boundary_completes_jobs_once(self):
        self.world.seed(self.pid, resources=economy.RECIPES["asic"]["inputs"])
        self.world.craft(self.pid, "asic")
        boundary = self.now + economy.RECIPES["asic"]["duration"]
        with database._connection() as c:
            economy.advance(c, boundary, boundary)
            economy.advance(c, boundary, boundary)
            self.assertEqual(c.execute("SELECT count(*) FROM equipment").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT status FROM jobs").fetchone()[0], "completed")
            self.assertEqual(c.execute(
                "SELECT xp FROM skills WHERE player_id=? AND name='crafting'",
                (self.pid,)).fetchone()[0],
                economy.RECIPES["asic"]["xp"])

    def test_job_completion_is_bounded_and_resumable(self):
        self.world.seed(self.pid, resources=economy.RECIPES["asic"]["inputs"])
        self.world.craft(self.pid, "asic")
        with database._connection() as c:
            job = c.execute("SELECT * FROM jobs").fetchone()
            c.executemany("""INSERT INTO jobs
                (player_id,recipe_id,started_at,completes_at,recipe_json)
                VALUES (?,?,?,?,?)""",
                ((self.pid, job["recipe_id"], job["started_at"], job["completes_at"],
                  job["recipe_json"]) for _ in range(economy.JOB_BATCH)))
            boundary = job["completes_at"]
            self.assertFalse(economy.advance(c, boundary, boundary))
            self.assertEqual(c.execute("SELECT count(*) FROM equipment").fetchone()[0],
                             economy.JOB_BATCH)
            self.assertTrue(economy.advance(c, boundary, boundary))
            self.assertTrue(economy.advance(c, boundary, boundary))
            self.assertEqual(c.execute("SELECT count(*) FROM equipment").fetchone()[0],
                             economy.JOB_BATCH + 1)
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0], 0)

    def test_effective_hash_power_install_enabled_and_locked(self):
        self.world.seed(self.pid, solar_panels=1, asics=2)
        items = self.world.snapshot(self.pid)["equipment"]
        for item in items:
            self.world.equipment(self.pid, item["id"], True, True)
        self.assertEqual(self.world.snapshot(self.pid)["effective_hash"], 1)
        with database._connection() as c:
            c.execute("UPDATE equipment SET locked=1 WHERE kind='solar_panel'")
        self.assertEqual(self.world.snapshot(self.pid)["effective_hash"], 0)
        with self.assertRaises(ValueError):
            self.world.equipment(self.pid, items[0]["id"], False, False)
        for installed, enabled in ((False, True), (1, True), (True, 1)):
            with self.assertRaises(ValueError):
                self.world.equipment(self.pid, items[-1]["id"], installed, enabled)

    def test_enabled_toggle_never_changes_installation_and_checks_locked_owner(self):
        self.world.seed(self.pid, asics=1, droids=1)
        snapshot = self.world.snapshot(self.pid)
        item_id = next(item["id"] for item in snapshot["equipment"] if item["kind"] == "asic")
        droid_id = next(item["id"] for item in snapshot["equipment"] if item["kind"] == "droid")
        with self.assertRaisesRegex(ValueError, "must be installed"):
            self.world.set_enabled(self.pid, item_id, True)
        self.world.set_enabled(self.pid, item_id, False)
        with database._connection() as c:
            self.assertEqual(tuple(c.execute(
                "SELECT installed,enabled FROM equipment WHERE id=?", (item_id,)).fetchone()), (0, 0))
        self.world.equipment(self.pid, item_id, True, False)
        self.world.set_enabled(self.pid, item_id, True)
        with database._connection() as c:
            self.assertEqual(tuple(c.execute(
                "SELECT installed,enabled FROM equipment WHERE id=?", (item_id,)).fetchone()), (1, 1))
        self.world.equipment(self.pid, item_id, False, False)
        self.world.set_enabled(self.pid, item_id, False)
        with database._connection() as c:
            self.assertEqual(tuple(c.execute(
                "SELECT installed,enabled FROM equipment WHERE id=?", (item_id,)).fetchone()), (0, 0))
        outsider = database.create_player("outsider", "a long test password")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.world.set_enabled(outsider, item_id, False)
        with database._connection() as c:
            c.execute("UPDATE equipment SET locked=1 WHERE id=?", (item_id,))
        for enabled in (True, False):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.world.set_enabled(self.pid, item_id, enabled)
            with self.assertRaisesRegex(ValueError, "worker assignments"):
                self.world.set_enabled(self.pid, droid_id, enabled)
        with self.assertRaisesRegex(ValueError, "boolean"):
            self.world.set_enabled(self.pid, item_id, 1)

    def test_catch_up_backlog_blocks_mutation(self):
        self.world.seed(self.pid, resources=economy.RECIPES["asic"]["inputs"])
        with patch("simulation.catch_up", return_value=False):
            with self.assertRaisesRegex(ValueError, "catching up"):
                self.world.craft(self.pid, "asic")
        self.assertEqual(self.world.snapshot(self.pid)["land"], [])

    def test_other_player_assets_cannot_be_used(self):
        self.world.seed(self.pid, fiat=1000000, humans=1, asics=1, acres=1)
        parcel = self.world.snapshot(self.pid)["land"][0]["id"]
        snap = self.world.snapshot(self.pid)
        other = database.create_player("outsider", "a long test password")
        with self.assertRaises(ValueError):
            self.world.sell_land(other, parcel)
        with self.assertRaises(ValueError):
            self.world.assign(other, snap["workers"][0]["id"], parcel)
        with self.assertRaises(ValueError):
            self.world.equipment(other, snap["equipment"][0]["id"], True, True)

    def test_missing_records_restored_without_grants(self):
        with database._connection() as c:
            c.execute("DELETE FROM inventory WHERE symbol='O'")
            c.execute("DELETE FROM skills")
        snap = self.world.snapshot(self.pid)
        self.assertEqual(snap["inventory"]["O"], 0)
        self.assertEqual(len(snap["skills"]), 2)

    def test_seed_land_exact_and_supply_limited_with_atomic_rollback(self):
        self.world.seed(self.pid, acres=2, humans=1)
        self.assertEqual(self.world.snapshot(self.pid)["land"][0]["acres"], 2)
        self.assertEqual(self.world.snapshot(self.pid)["balances"]["fiat"], 0)
        with self.assertRaises(ValueError):
            self.world.seed(self.pid, fiat=10, acres=economy.LAND_SUPPLY)
        self.assertEqual(self.world.snapshot(self.pid)["balances"]["fiat"], 0)

    def test_droid_inventory_link_and_escrow_prevents_assignment(self):
        self.world.seed(self.pid, acres=1, droids=1)
        snap = self.world.snapshot(self.pid)
        droid = snap["workers"][0]
        self.assertEqual(droid["equipment_id"], snap["equipment"][0]["id"])
        for installed, enabled in ((True, True), (False, False)):
            with self.assertRaisesRegex(ValueError, "Use worker assignments for droids"):
                self.world.equipment(self.pid, droid["equipment_id"], installed, enabled)
        with database._connection() as c:
            c.execute("UPDATE equipment SET locked=1 WHERE id=?", (droid["equipment_id"],))
        with self.assertRaises(ValueError):
            self.world.assign(self.pid, droid["id"], snap["land"][0]["id"])

    def test_seed_worker_and_item_counts_are_bounded_before_any_grant(self):
        for name in ("humans", "droids", "solar_panels", "asics"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "cannot exceed"):
                self.world.seed(self.pid, fiat=10, **{name: economy.MAX_SEED_COUNT + 1})
        snap = self.world.snapshot(self.pid)
        self.assertEqual(snap["balances"]["fiat"], 0)
        self.assertEqual(snap["workers"], [])
        self.assertEqual(snap["equipment"], [])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        data = patch.object(database, "DATA_DIR", self.directory)
        data.start()
        self.addCleanup(data.stop)

    def legacy(self, fiat, btc, version=3):
        with sqlite3.connect(self.directory / "galaxy_resources.db") as c:
            c.execute("""CREATE TABLE players (
                id INTEGER PRIMARY KEY,npub TEXT UNIQUE,created_at TEXT,
                fiat_balance REAL,bitcoin_balance REAL,username TEXT,password_hash TEXT)""")
            c.execute("INSERT INTO players VALUES (7,'legacy','date',?,?,NULL,NULL)", (fiat, btc))
            c.execute(f"PRAGMA user_version={version}")

    def test_exact_subunit_remainders_and_legacy_archive(self):
        self.legacy(12.123456789, 0.000000019)
        database.init_database()
        database.init_database()
        with database._connection() as c:
            a = c.execute("SELECT * FROM accounts WHERE player_id=7").fetchone()
            self.assertEqual(a["fiat"], 12123456)
            self.assertEqual(a["btc"], 1)
            self.assertEqual(Decimal(a["legacy_fiat_remainder"]) + Decimal(a["fiat"]) / 1000000,
                             Decimal("12.123456789"))
            self.assertEqual(Decimal(a["legacy_btc_remainder"]) + Decimal(a["btc"]) / 100000000,
                             Decimal("0.000000019"))
            self.assertEqual(c.execute(
                "SELECT fiat_balance FROM players WHERE id=7").fetchone()[0], 12.123456789)
            self.assertEqual(c.execute(
                "SELECT count(*) FROM inventory WHERE player_id=7"
            ).fetchone()[0], 118)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_invalid_migration_rolls_back_schema_version_and_data(self):
        self.legacy(-0.1, 0)
        with self.assertRaisesRegex(ValueError, "player 7 fiat"):
            database.init_database()
        with sqlite3.connect(self.directory / "galaxy_resources.db") as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT fiat_balance FROM players").fetchone()[0], -0.1)
            self.assertIsNone(c.execute(
                "SELECT name FROM sqlite_master WHERE name='accounts'").fetchone())
            c.execute("UPDATE players SET fiat_balance=0")
        database.init_database()

    def test_nonfinite_and_out_of_range_rejected_atomically(self):
        self.legacy(float("inf"), 0)
        for value in (float("inf"), 1e30):
            with sqlite3.connect(self.directory / "galaxy_resources.db") as c:
                c.execute("UPDATE players SET fiat_balance=?", (value,))
            with self.assertRaises(ValueError):
                database.init_database()
            with sqlite3.connect(self.directory / "galaxy_resources.db") as c:
                self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_concurrent_v3_upgrade(self):
        self.legacy(3.25, 0.5)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: database.init_database(), range(8)))
        with database._connection() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertEqual(c.execute("SELECT fiat,btc FROM accounts WHERE player_id=7").fetchone()[:],
                             (3250000, 50000000))
            self.assertEqual(c.execute(
                "SELECT count(*) FROM inventory WHERE player_id=7"
            ).fetchone()[0], 118)

    def test_v4_upgrade_assigns_unowned_land_to_earth_bank(self):
        with sqlite3.connect(self.directory / "galaxy_resources.db") as c:
            c.executescript("""
                CREATE TABLE players (
                    id INTEGER PRIMARY KEY, npub TEXT UNIQUE, created_at TEXT NOT NULL);
                CREATE TABLE accounts (
                    player_id INTEGER PRIMARY KEY REFERENCES players(id),
                    fiat INTEGER NOT NULL DEFAULT 0, btc INTEGER NOT NULL DEFAULT 0,
                    legacy_fiat_remainder TEXT NOT NULL DEFAULT '0',
                    legacy_btc_remainder TEXT NOT NULL DEFAULT '0');
                CREATE TABLE inventory (
                    player_id INTEGER NOT NULL REFERENCES players(id), symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 0, carry INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(player_id,symbol));
                CREATE TABLE skills (
                    player_id INTEGER NOT NULL REFERENCES players(id), name TEXT NOT NULL,
                    xp INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(player_id,name));
                CREATE TABLE planets(name TEXT PRIMARY KEY,total_acres INTEGER NOT NULL);
                CREATE TABLE land (
                    id INTEGER PRIMARY KEY,player_id INTEGER NOT NULL REFERENCES players(id),
                    planet TEXT NOT NULL,acres INTEGER NOT NULL,locked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE auctions(
                    id INTEGER PRIMARY KEY, bid INTEGER NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'fiat',
                    status TEXT NOT NULL DEFAULT 'active');
                INSERT INTO players VALUES (7,'legacy','date');
                INSERT INTO accounts(player_id,fiat,btc) VALUES (7,123,456);
                INSERT INTO planets VALUES ('Earth',36800000000);
                INSERT INTO land(id,player_id,planet,acres) VALUES (1,7,'Earth',100);
                PRAGMA user_version=4;
            """)
        database.init_database()
        with database._connection() as c:
            bank_id = economy.earth_bank_id(c)
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertEqual(c.execute(
                "SELECT player_id,acres FROM land WHERE id=1"
            ).fetchone()[:], (7, 100))
            self.assertEqual(c.execute(
                "SELECT sum(acres) FROM land WHERE player_id=?", (bank_id,)
            ).fetchone()[0], economy.LAND_SUPPLY - 100)
            self.assertEqual(c.execute(
                "SELECT fiat,btc FROM accounts WHERE player_id=7"
            ).fetchone()[:], (123, 456))
            self.assertEqual(c.execute(
                "SELECT initial_fiat_supply FROM world_economy WHERE id=1"
            ).fetchone()[0], 123)

    def test_v3_orphan_payment_aborts_and_rolls_back_entire_upgrade(self):
        self.legacy(1, 0)
        with sqlite3.connect(self.directory / "galaxy_resources.db") as c:
            c.execute("CREATE TABLE payments(player_id INTEGER REFERENCES players(id))")
            c.execute("INSERT INTO payments VALUES (999)")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid references"):
            database.init_database()
        with sqlite3.connect(self.directory / "galaxy_resources.db") as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertIsNone(c.execute(
                "SELECT name FROM sqlite_master WHERE name='accounts'").fetchone())
            self.assertEqual(c.execute("SELECT player_id FROM payments").fetchone()[0], 999)
            self.assertEqual(c.execute("SELECT fiat_balance FROM players").fetchone()[0], 1)
            c.execute("DELETE FROM payments")
        database.init_database()
        with database._connection() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v1_rebuild_and_v4_failure_are_one_transaction(self):
        with sqlite3.connect(self.directory / "galaxy_resources.db") as c:
            c.execute("""CREATE TABLE players (
                id INTEGER PRIMARY KEY,npub TEXT NOT NULL UNIQUE,created_at TEXT,
                fiat_balance REAL,bitcoin_balance REAL)""")
            c.execute("INSERT INTO players VALUES (7,'legacy','date',-1,0)")
            c.execute("PRAGMA user_version=1")
        with self.assertRaises(ValueError):
            database.init_database()
        with sqlite3.connect(self.directory / "galaxy_resources.db") as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn("username", [row[1] for row in c.execute("PRAGMA table_info(players)")])
            self.assertEqual(c.execute("SELECT fiat_balance FROM players").fetchone()[0], -1)
            self.assertIsNone(c.execute(
                "SELECT name FROM sqlite_master WHERE name='accounts'").fetchone())


if __name__ == "__main__":
    unittest.main()
