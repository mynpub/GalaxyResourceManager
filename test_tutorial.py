"""Resumable tutorial, market-priced payouts and atomic one-time claims."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import database
import tutorial
from auctions import AuctionHouse
from economy import MAX_INTEGER, RECIPES, World, earth_bank_id
from game_logic import GameLogic

PASSWORD = "a long test passphrase"
LESSON_COMMANDS = (
    ("status", "world"),
    ("work status",),
    ("land", "workers", "inventory"),
    ("skills", "jobs", "equipment"),
    ("power", "chain"),
    ("auctions", "bank wealth"),
)


class TutorialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = patch.object(database, "DATA_DIR", Path(temporary.name))
        directory.start()
        self.addCleanup(directory.stop)
        self.output = []
        self.game = GameLogic(output=self.output.append)
        self.assertTrue(self.game.create_player_account("explorer", PASSWORD))
        self.player = self.game.player_id

    def complete_lessons(self):
        for number, commands in enumerate(LESSON_COMMANDS, 1):
            self.assertIn(f"Tutorial {number}/6", self.game.command("tutorial"))
            for command in commands:
                self.game.command(command)
            result = self.game.command("tutorial next")
            self.assertNotIn("Complete these lesson commands", result)
        self.assertIn("All tutorial lessons complete", result)

    def listing(self):
        return next(row for row in AuctionHouse().listings(self.player)
                    if row["seller_name"] == "Earth Bank" and row["status"] == "active"
                    and row["kind"] == "land")

    def reward(self):
        with database._connection() as c:
            row = c.execute(
                "SELECT * FROM tutorial_fiat_rewards WHERE player_id=?", (self.player,)
            ).fetchone()
            return dict(row) if row else None

    def test_signup_starts_resumable_tutorial_without_grant(self):
        self.assertIn("Tutorial started!", "\n".join(self.output))
        self.assertIn("Tutorial 1/6", "\n".join(self.output))
        self.assertIn("Complete these lesson commands", self.game.command("tutorial next"))
        self.game.command("status")
        self.assertIn("Unexpected arguments", self.game.command("world extra"))
        self.assertIn("world", self.game.command("tutorial next"))
        returning = GameLogic(output=lambda message: None)
        self.assertTrue(returning.login("explorer", PASSWORD))
        self.assertIn("Try: world", returning.command("tutorial"))
        self.assertNotIn("Try: status", returning.command("tutorial"))
        snapshot = World().snapshot(self.player)
        self.assertEqual(snapshot["land"], [])
        self.assertEqual(snapshot["balances"]["fiat"], 0)
        self.assertIn("Please signup", GameLogic().command("tutorial claim"))
        self.assertIn("Use tutorial", self.game.command("tutorial email user@example.com"))

    def test_future_lesson_commands_do_not_skip_progress_and_aliases_count(self):
        self.game.command("auctions")
        self.game.command("profile")
        self.game.command("world")
        self.assertIn("Tutorial 2/6", self.game.command("tutorial next"))
        self.game.command("work")
        self.assertIn("Tutorial 3/6", self.game.command("tutorial next"))
        for command in ("land", "workers", "resources"):
            self.game.command(command)
        self.assertIn("Tutorial 4/6", self.game.command("tutorial next"))
        for command in ("skills", "jobs", "equipment"):
            self.game.command(command)
        self.game.command("tutorial next")
        for command in ("hashrate", "chain"):
            self.game.command(command)
        self.game.command("tutorial next")
        self.game.command("bank wealth")
        self.assertIn("auctions", self.game.command("tutorial next"))

    def test_completed_tutorial_grants_fiat_and_can_buy_one_acre_with_extra_left(self):
        self.assertIn("Complete all", self.game.command("tutorial claim"))
        self.assertIsNone(self.reward())
        self.complete_lessons()
        listing = self.listing()
        result = self.game.command("tutorial claim")
        self.assertIn("received 990000 microfiat", result)
        self.assertIn(f"land buy {listing['id']} 1 900000", result)
        reward = self.reward()
        self.assertEqual(reward["auction_id"], listing["id"])
        self.assertEqual(reward["minimum_bid"], 900_000)
        self.assertEqual(reward["amount"], 990_000)
        snapshot = World().snapshot(self.player)
        self.assertEqual(snapshot["balances"]["fiat"], 990_000)
        self.assertEqual(snapshot["land"], [])
        self.assertEqual(snapshot["workers"], [])
        self.assertEqual(snapshot["equipment"], [])
        self.assertIn("escrow", self.game.command(f"land buy {listing['id']} 1 900000"))
        self.assertEqual(World().snapshot(self.player)["balances"]["fiat"], 90_000)
        completed = World(clock=lambda: listing["closes"]).snapshot(self.player)
        self.assertEqual(sum(row["acres"] for row in completed["land"]), 1)
        self.assertEqual(completed["balances"]["fiat"], 90_000)
        with self.assertRaisesRegex(ValueError, "already been claimed"):
            tutorial.claim(self.player, clock=lambda: listing["closes"])
        self.assertIn("reward was claimed", self.game.command("tutorial"))

    def test_claim_tracks_latest_per_acre_bid_not_total_escrow_or_existing_savings(self):
        self.complete_lessons()
        World().seed(self.player, fiat=123)
        other = database.create_player("bidder", PASSWORD)
        World().seed(other, fiat=20_000_000)
        listing = self.listing()
        AuctionHouse().bid(other, listing["id"], 1_050_000, acreage=10)
        tutorial.claim(self.player)
        self.assertEqual(self.reward()["minimum_bid"], 1_050_001)
        self.assertEqual(self.reward()["amount"], 1_155_002)
        self.assertEqual(World().snapshot(self.player)["balances"]["fiat"], 1_155_125)
        self.assertIn("escrow", self.game.command(f"land buy {listing['id']} 1 1050001"))

    def test_reserve_changes_and_small_prices_use_rounded_up_buffer(self):
        self.complete_lessons()
        listing = self.listing()
        with database._connection() as c:
            c.execute("UPDATE auctions SET reserve=11 WHERE id=?", (listing["id"],))
        tutorial.claim(self.player)
        self.assertEqual(self.reward()["minimum_bid"], 11)
        self.assertEqual(self.reward()["amount"], 13)

    def test_claim_catches_up_expired_auction_before_pricing(self):
        self.complete_lessons()
        other = database.create_player("bidder", PASSWORD)
        World().seed(other, fiat=2_000_000)
        old = self.listing()
        AuctionHouse().bid(other, old["id"], 1_099_999, acreage=1)
        tutorial.claim(self.player, clock=lambda: old["closes"])
        with database._connection() as c:
            active = c.execute(
                "SELECT id,reserve FROM auctions WHERE kind='land' AND status='active'"
            ).fetchone()
            reward = self.reward()
            self.assertNotEqual(reward["auction_id"], old["id"])
            self.assertEqual(reward["auction_id"], active["id"])
            self.assertEqual(reward["minimum_bid"], active["reserve"])
            self.assertEqual(reward["claimed_at"], old["closes"])

    def test_concurrent_claims_pay_once_and_spending_does_not_reset_eligibility(self):
        self.complete_lessons()

        def claim(_):
            try:
                return tutorial.claim(self.player)
            except ValueError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(claim, range(4)))
        self.assertEqual(sum("received 990000" in result for result in results), 1)
        self.assertEqual(sum("already been claimed" in result for result in results), 3)
        with database._connection() as c:
            self.assertEqual(c.execute(
                "SELECT count(*) FROM tutorial_fiat_rewards"
            ).fetchone()[0], 1)
            self.assertEqual(c.execute(
                "SELECT fiat FROM accounts WHERE player_id=?", (self.player,)
            ).fetchone()[0], 990_000)
            c.execute("UPDATE accounts SET fiat=0 WHERE player_id=?", (self.player,))
        self.assertIn("already been claimed", self.game.command("tutorial claim"))

    def test_unavailable_land_does_not_pay_or_consume_eligibility(self):
        self.complete_lessons()
        listing = self.listing()
        with database._connection() as c:
            c.execute("UPDATE auctions SET status='cancelled' WHERE id=?", (listing["id"],))
            c.execute("UPDATE land SET player_id=?,locked=0 WHERE player_id=?",
                      (self.player, earth_bank_id(c)))
        self.assertIn("No active Earth Bank land auction", self.game.command("tutorial claim"))
        self.assertIsNone(self.reward())
        with database._connection() as c:
            self.assertEqual(c.execute(
                "SELECT fiat FROM accounts WHERE player_id=?", (self.player,)
            ).fetchone()[0], 0)
            c.execute("UPDATE land SET player_id=? WHERE player_id=?",
                      (earth_bank_id(c), self.player))
        self.assertIn("received", self.game.command("tutorial claim"))

    def test_overflow_rolls_back_payout_and_allows_retry(self):
        self.complete_lessons()
        listing = self.listing()
        with database._connection() as c:
            c.execute("UPDATE auctions SET reserve=?,buy_now=? WHERE id=?",
                      (MAX_INTEGER, MAX_INTEGER, listing["id"]))
        self.assertIn("exceeds SQLite integer range", self.game.command("tutorial claim"))
        self.assertIsNone(self.reward())
        with database._connection() as c:
            self.assertEqual(c.execute(
                "SELECT fiat FROM accounts WHERE player_id=?", (self.player,)
            ).fetchone()[0], 0)
            c.execute("UPDATE auctions SET reserve=900000 WHERE id=?", (listing["id"],))
            c.execute("UPDATE accounts SET fiat=? WHERE player_id=?", (MAX_INTEGER, self.player))
        self.assertIn("balance overflow", self.game.command("tutorial claim"))
        self.assertIsNone(self.reward())
        with database._connection() as c:
            c.execute("UPDATE accounts SET fiat=0 WHERE player_id=?", (self.player,))
        self.assertIn("received", self.game.command("tutorial claim"))

    def test_v6_upgrade_preserves_existing_accounts_without_retroactive_rewards(self):
        with database._connection() as c:
            for table in ("tutorial_fiat_rewards", "tutorial_legacy_claims", "tutorial_progress"):
                c.execute(f"DROP TABLE {table}")
            c.execute("PRAGMA user_version=6")
        database.init_database()
        database.init_database()
        self.assertEqual(database.authenticate_player("explorer", PASSWORD), self.player)
        self.assertIn("accounts created after", self.game.command("tutorial"))
        self.assertIn("accounts created after", self.game.command("tutorial claim"))
        new_player = database.create_player("newplayer", PASSWORD)
        self.assertIn("Tutorial 1/6", tutorial.show(new_player))
        with database._connection() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v7_upgrade_preserves_progress_and_prevents_double_reward(self):
        self.complete_lessons()
        other = database.create_player("another", PASSWORD)
        with database._connection() as c:
            c.execute("DROP TABLE tutorial_fiat_rewards")
            c.execute("DROP TABLE tutorial_legacy_claims")
            c.execute("CREATE TABLE tutorial_rewards (player_id INTEGER PRIMARY KEY)")
            c.execute("INSERT INTO tutorial_rewards VALUES (?)", (self.player,))
            c.execute("PRAGMA user_version=7")
        database.init_database()
        database.init_database()
        self.assertIn("already been claimed", self.game.command("tutorial claim"))
        self.assertIn("already claimed", self.game.command("tutorial"))
        self.assertIn("Tutorial 1/6", tutorial.show(other))
        self.assertIsNone(self.reward())
        with database._connection() as c:
            self.assertEqual(c.execute(
                "SELECT step FROM tutorial_progress WHERE player_id=?", (self.player,)
            ).fetchone()[0], 6)

    def test_droid_tutorial_full_flow_and_nontransferable_materials(self):
        self.assertIn("tutorial droid start", self.game.command("tutorial droid"))
        self.assertIn("Start the droid tutorial", self.game.command('tutorial droid craft "R2"'))
        self.assertIn("Droid recipe", self.game.command("tutorial droid start"))
        self.assertIn("need unlocked land", self.game.command('tutorial droid craft "R2"'))
        before = World().snapshot(self.player)
        self.assertEqual(sum(before["inventory"].values()), 0)
        self.assertNotIn("queued", self.game.command("craft droid"))
        World().seed(self.player, acres=1)
        land = World().snapshot(self.player)["land"][0]["id"]
        self.assertIn("job #", self.game.command('tutorial droid craft "R2"'))
        self.assertIn("already been queued", self.game.command('tutorial droid craft "R2"'))
        self.assertIn("in progress", self.game.command("tutorial droid"))
        snapshot = World().snapshot(self.player)
        self.assertEqual(sum(snapshot["inventory"].values()), 0)
        job = snapshot["jobs"][0]
        self.assertEqual(job["recipe_id"], "droid")
        self.assertEqual(job["completes_at"] - job["started_at"], RECIPES["droid"]["duration"])
        World(clock=lambda: job["completes_at"]).snapshot(self.player)
        with database._connection() as c:
            worker = c.execute(
                "SELECT worker_id FROM droid_tutorial WHERE player_id=?", (self.player,)
            ).fetchone()[0]
        self.assertIn(f"worker is #{worker}", tutorial.droid_status(
            self.player, clock=lambda: job["completes_at"]))
        future = World(clock=lambda: job["completes_at"])
        future.assign(self.player, worker, land)
        self.assertIn("tutorial complete", tutorial.droid_status(
            self.player, clock=lambda: job["completes_at"]))
        snapshot = World(clock=lambda: job["completes_at"] + 10).snapshot(self.player)
        self.assertGreater(sum(snapshot["inventory"].values()), 0)
        self.assertEqual(next(s["xp"] for s in snapshot["skills"]
                              if s["name"] == "crafting"), RECIPES["droid"]["xp"])

    def test_droid_tutorial_one_job_under_concurrency_and_reconnect(self):
        World().seed(self.player, acres=1)
        self.game.command("tutorial droid start")

        def craft(_):
            try:
                return tutorial.craft_droid(self.player, "R2")
            except ValueError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(craft, range(4)))
        self.assertEqual(sum("queued using" in result for result in results), 1)
        self.assertEqual(sum("already been queued" in result for result in results), 3)
        returning = GameLogic(output=lambda message: None)
        self.assertTrue(returning.login("explorer", PASSWORD))
        self.assertIn("in progress", returning.command("tutorial droid"))
        self.assertIn("in progress", returning.command("tutorial droid start"))
        self.assertEqual(len(World().snapshot(self.player)["jobs"]), 1)

    def test_droid_tutorial_after_actual_land_purchase(self):
        World().seed(self.player, fiat=1_100_000)
        listing = self.listing()
        self.assertIn("Buy now complete", self.game.command(f"auction buy {listing['id']} 1"))
        self.assertIn("Droid recipe", self.game.command("tutorial droid start"))
        self.assertIn("queued", self.game.command('tutorial droid craft "R2"'))
        with database._connection() as c:
            c.execute("UPDATE jobs SET completes_at=started_at WHERE player_id=?", (self.player,))
        self.assertIn("Your droid worker is", self.game.command("tutorial droid"))
        snapshot = World().snapshot(self.player)
        land = snapshot["land"][0]["id"]
        worker = snapshot["workers"][0]["id"]
        self.assertIn("Assignment updated", self.game.command(f"assign {worker} {land}"))
        self.assertIn("tutorial complete", self.game.command("tutorial droid"))
        self.game.command(f"assign {worker} none")
        self.assertIn("already been queued", self.game.command('tutorial droid craft "R2"'))

    def test_droid_tutorial_requires_unlocked_land(self):
        World().seed(self.player, acres=1)
        land = World().snapshot(self.player)["land"][0]["id"]
        AuctionHouse().list_asset(self.player, "land", str(land), 1, "fiat", 1)
        self.game.command("tutorial droid start")
        self.assertIn("need unlocked land", self.game.command('tutorial droid craft "R2"'))
        with database._connection() as c:
            self.assertIsNone(c.execute(
                "SELECT job_id FROM droid_tutorial WHERE player_id=?", (self.player,)
            ).fetchone()[0])

    def test_tutorial_droid_name_and_trade_restrictions(self):
        World().seed(self.player, acres=1, droids=1)
        self.game.command("tutorial droid start")
        for invalid in ('tutorial droid craft ""', 'tutorial droid craft "   "',
                        'tutorial droid craft "' + "x" * 33 + '"',
                        'tutorial droid craft "bad\nname"'):
            self.assertIn("1-32 printable", self.game.command(invalid))
        self.assertIn('craft "NAME"', self.game.command("tutorial droid craft"))
        self.assertIn("Rover One", self.game.command('tutorial droid craft "Rover One"'))
        with database._connection() as c:
            c.execute("UPDATE jobs SET completes_at=started_at WHERE player_id=?", (self.player,))
        self.assertIn("named Rover One", self.game.command("tutorial droid"))
        workers = json.loads(self.game.command("workers"))
        starter = next(w for w in workers if w.get("name") == "Rover One")
        self.assertFalse(starter["tradable"])
        equipment = json.loads(self.game.command("equipment"))
        item = next(e for e in equipment if e["id"] == starter["equipment_id"])
        self.assertEqual(item["name"], "Rover One")
        self.assertFalse(item["tradable"])
        for currency in ("fiat", "btc"):
            self.assertIn("cannot be traded", self.game.command(
                f"auction list item {item['id']} 1 {currency} 1"))
        land = World().snapshot(self.player)["land"][0]["id"]
        self.game.command(f"assign {starter['id']} {land}")
        self.game.command(f"assign {starter['id']} none")
        self.assertIn("cannot be traded", self.game.command(
            f"auction list item {item['id']} 1 fiat 1"))
        ordinary = next(w for w in workers if w["id"] != starter["id"])
        self.assertIn("Auction listed", self.game.command(
            f"auction list item {ordinary['equipment_id']} 1 fiat 1"))
        returning = GameLogic(output=lambda message: None)
        self.assertTrue(returning.login("explorer", PASSWORD))
        self.assertIn("Rover One", returning.command("tutorial droid"))
        self.assertIn("Rover One", returning.command("workers"))

    def test_v9_upgrade_preserves_accounts_and_allows_droid_tutorial(self):
        with database._connection() as c:
            c.execute("DROP TABLE droid_tutorial")
            c.execute("PRAGMA user_version=9")
        database.init_database()
        database.init_database()
        self.assertEqual(database.authenticate_player("explorer", PASSWORD), self.player)
        self.assertIn("Droid recipe", self.game.command("tutorial droid start"))
        with database._connection() as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
