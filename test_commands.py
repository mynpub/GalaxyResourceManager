"""Shared local/session commands and strictly opt-in test grants."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import main
import seed_test_world
from commands import mass
from economy import World
from game_logic import GameLogic


class GameplayCommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        data_patch = patch.object(database, "DATA_DIR", self.directory)
        data_patch.start()
        self.addCleanup(data_patch.stop)
        self.game = GameLogic(output=lambda message: None)
        self.assertTrue(self.game.create_player_account("explorer", "test long passphrase"))

    def test_zero_inventory_help_skills_and_input_validation(self):
        inventory = json.loads(self.game.command("inventory"))
        self.assertEqual(len(inventory), 118)
        self.assertTrue(all(row["quantity"] == 0 for row in inventory))
        self.assertIn("0.0 fiat, 0.0 bitcoin", self.game.command("status"))
        self.assertEqual({s["level"] for s in json.loads(self.game.command("skills"))}, {1})
        self.assertIn("auction list", self.game.command("help"))
        self.assertIn("work [status|start]", self.game.command("help"))
        self.assertIn("integer", self.game.command("land buy invalid 1 900000"))
        self.assertIn("Unexpected", self.game.command("inventory 999"))
        self.assertIn("No closing quotation", self.game.command('craft "'))
        self.assertIn("Please signup", GameLogic().command("inventory"))
        self.assertEqual(mass(1), "0.001 mg")
        self.assertEqual(mass(1_000_000), "1.0 g")
        self.assertEqual(mass(1_500_000_000), "1.5 kg")

    def test_bank_work_commands_start_and_report_shift(self):
        started = self.game.command("work start")
        self.assertIn("started for 8 hours", started)
        status = self.game.command("work status")
        self.assertIn("Earth Bank shift #", status)
        self.assertIn("1.0 microfiat per work-second", status)
        self.assertIn("already active", self.game.command("work start"))
        self.assertIn("Use work", self.game.command("work unknown"))

    def test_world_mutators_auction_and_private_inventory(self):
        World().seed(self.game.player_id, fiat=5_000_000, resources={"Fe": 1000})
        bank_listing = next(
            row for row in json.loads(self.game.command("auctions"))
            if row["seller_name"] == "Earth Bank"
        )
        self.assertIn(
            "escrow", self.game.command(
                f"land buy {bank_listing['id']} 1 {bank_listing['reserve']}"
            )
        )
        self.assertIn("Earth Bank wealth:", self.game.command("bank wealth"))
        result = self.game.command("auction list resource Fe 100 fiat 10")
        self.assertIn("Auction listed:", result)
        resource_listing = next(
            row for row in json.loads(self.game.command("auctions"))
            if row["asset"] == "Fe"
        )
        other = GameLogic(output=lambda message: None)
        other.create_player_account("another", "test long passphrase")
        self.assertIn("seller", other.command(f"auction cancel {resource_listing['id']}"))
        self.assertEqual(sum(r["quantity"] for r in json.loads(other.command("inventory"))), 0)
        self.assertIn(
            "cancelled",
            self.game.command(f"auction cancel {resource_listing['id']}")
        )
        self.assertIn(
            "Insufficient",
            other.command(
                f"land buy {bank_listing['id']} 1 {bank_listing['reserve'] + 1}"
            )
        )

    def test_auction_buy_now_command_and_optional_listing_price(self):
        World().seed(self.game.player_id, fiat=1_000_000, resources={"Fe": 1000})
        listing_id = self.game.command(
            "auction list resource Fe 100 fiat 10 --buy-now 25"
        ).split(": ")[-1]
        self.assertIn('"buy_now": 25', self.game.command("auctions"))
        other = GameLogic(output=lambda message: None)
        other.create_player_account("another", "test long passphrase")
        World().seed(other.player_id, fiat=100)
        self.assertIn("Buy now complete", other.command(f"auction buy {listing_id}"))
        self.assertEqual(World().snapshot(other.player_id)["balances"]["fiat"], 75)
        self.assertEqual(
            sum(World().snapshot(other.player_id)["inventory"].values()),
            100,
        )
        no_bin = self.game.command("auction list resource Fe 10 fiat 10").split(": ")[-1]
        with database._connection() as c:
            c.execute("UPDATE accounts SET fiat=100 WHERE player_id=?", (other.player_id,))
        self.assertIn("no buy now price", other.command(f"auction buy {no_bin}"))
        self.assertIn("--buy-now PRICE", self.game.command("help"))

    def test_bank_buy_now_command_requires_selected_acreage(self):
        World().seed(self.game.player_id, fiat=2_000_000)
        listing = next(
            row for row in json.loads(self.game.command("auctions"))
            if row["seller_name"] == "Earth Bank" and row["kind"] == "land"
        )
        self.assertEqual(listing["buy_now_per_acre"], 1_100_000)
        self.assertIn("require an acreage",
                      self.game.command(f"auction buy {listing['id']}"))
        self.assertIn("Buy now complete",
                      self.game.command(f"auction buy {listing['id']} 1"))
        snapshot = World().snapshot(self.game.player_id)
        self.assertEqual(sum(row["acres"] for row in snapshot["land"]), 1)

    def test_local_one_shot_and_play_paths_use_authenticated_commands(self):
        with patch("main.getpass.getpass", return_value="test long passphrase"), \
                patch("builtins.input", side_effect=["inventory", "quit"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = main.main(["--login", "--username", "explorer", "--command", "world",
                                "--command", "skills", "--play"])
        self.assertEqual(result, 0)
        self.assertIn("Earth", output.getvalue())

    def test_seed_requires_explicit_confirmation_and_only_requested_grants(self):
        args = ["--data-dir", str(self.directory), "--username", "explorer"]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            seed_test_world.main(args)
        self.assertEqual(World().snapshot(self.game.player_id)["balances"]["fiat"], 0)
        with contextlib.redirect_stdout(io.StringIO()):
            seed_test_world.main(args + ["--confirm-test-grants", "--fiat", "1000000",
                                        "--humans", "1"])
        snapshot = World().snapshot(self.game.player_id)
        self.assertEqual(snapshot["balances"], {"fiat": 1_000_000, "btc": 0})
        self.assertEqual(len(snapshot["workers"]), 1)
        self.assertTrue(all(value == 0 for value in snapshot["inventory"].values()))
        self.assertEqual(snapshot["land"], [])

    def test_equipment_power_toggles_cannot_install_an_uninstalled_item(self):
        World().seed(self.game.player_id, solar_panels=1)
        item = json.loads(self.game.command("equipment"))[0]["id"]
        self.assertIn("Equipment updated", self.game.command(f"equip {item} install"))
        self.assertIn("Equipment updated", self.game.command(f"equip {item} off"))
        self.assertEqual(json.loads(self.game.command("equipment"))[0]["installed"], 1)
        self.assertIn("Equipment updated", self.game.command(f"equip {item} uninstall"))
        self.assertNotIn("Equipment updated", self.game.command(f"equip {item} on"))
        self.assertEqual(json.loads(self.game.command("equipment"))[0]["installed"], 0)


if __name__ == "__main__":
    unittest.main()
