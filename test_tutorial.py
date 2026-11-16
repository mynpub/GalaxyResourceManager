"""Resumable tutorial, market-priced payouts and atomic one-time claims."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import database
import tutorial
from auctions import AuctionHouse
from economy import MAX_INTEGER, World, earth_bank_id
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
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 9)
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


if __name__ == "__main__":
    unittest.main()
