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
        self.assertIn("integer", self.game.command("land buy 1.5"))
        self.assertIn("Unexpected", self.game.command("inventory 999"))
        self.assertIn("No closing quotation", self.game.command('craft "'))
        self.assertIn("Please signup", GameLogic().command("inventory"))
        self.assertEqual(mass(1), "0.001 mg")
        self.assertEqual(mass(1_000_000), "1.0 g")
        self.assertEqual(mass(1_500_000_000), "1.5 kg")

    def test_world_mutators_auction_and_private_inventory(self):
        World().seed(self.game.player_id, fiat=5_000_000, resources={"Fe": 1000})
        self.assertIn("Land updated", self.game.command("land buy 1"))
        result = self.game.command("auction list resource Fe 100 fiat 10")
        self.assertIn("Auction listed:", result)
        self.assertEqual(json.loads(self.game.command("auctions"))[0]["asset"], "Fe")
        other = GameLogic(output=lambda message: None)
        other.create_player_account("another", "test long passphrase")
        self.assertIn("seller", other.command("auction cancel 1"))
        self.assertEqual(sum(r["quantity"] for r in json.loads(other.command("inventory"))), 0)
        self.assertIn("cancelled", self.game.command("auction cancel 1"))
        self.assertIn("Insufficient", other.command("land buy 1"))

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
