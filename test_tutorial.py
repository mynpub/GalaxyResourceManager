"""Tutorial progression, private email verification and atomic tradable rewards."""

import hashlib
import os
import smtplib
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import database
import email_verification as verification
import tutorial
from auctions import AuctionHouse
from economy import World, earth_bank_id
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
        sender = patch.object(verification, "send_verification_email")
        self.send = sender.start()
        self.addCleanup(sender.stop)

    def complete_lessons(self):
        for number, commands in enumerate(LESSON_COMMANDS, 1):
            self.assertIn(f"Tutorial {number}/6", self.game.command("tutorial"))
            for command in commands:
                self.game.command(command)
            result = self.game.command("tutorial next")
            self.assertNotIn("Complete these lesson commands", result)
        self.assertIn("All tutorial lessons complete", result)

    def verify_email(self, player=None, address="explorer@example.com", now=1000):
        player = self.player if player is None else player
        verification.request(player, address, clock=lambda: now)
        token = self.send.call_args.args[1]
        verification.verify(player, token, clock=lambda: now)
        return token

    def test_signup_starts_resumable_tutorial_without_grant_or_email_requirement(self):
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
        self.assertEqual(World().snapshot(self.player)["land"], [])
        self.send.assert_not_called()
        self.assertIn("Please signup", GameLogic().command("tutorial claim"))

    def test_commands_in_future_lessons_do_not_skip_progress_and_aliases_count(self):
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
        self.assertIn("Use tutorial", self.game.command("tutorial reset"))

    def test_claim_requires_both_lessons_and_email_then_sale_cannot_reset_eligibility(self):
        self.assertIn("Complete all", self.game.command("tutorial claim"))
        self.complete_lessons()
        self.assertIn("Verify an email", self.game.command("tutorial claim"))
        self.verify_email()
        with database._connection() as c:
            bank = earth_bank_id(c)
            before = c.execute(
                "SELECT sum(acres) FROM land WHERE player_id=?", (bank,)
            ).fetchone()[0]
        self.assertIn("received one acre", self.game.command("tutorial claim"))
        snapshot = World().snapshot(self.player)
        self.assertEqual(sum(row["acres"] for row in snapshot["land"]), 1)
        self.assertEqual(snapshot["workers"], [])
        parcel = snapshot["land"][0]["id"]
        with database._connection() as c:
            self.assertEqual(c.execute(
                "SELECT sum(acres) FROM land WHERE player_id=?", (bank,)
            ).fetchone()[0], before - 1)
            self.assertEqual(c.execute("SELECT sum(acres) FROM land").fetchone()[0],
                             36_800_000_000)
        self.assertIn("Land updated", self.game.command(f"land sell {parcel}"))
        self.assertEqual(World().snapshot(self.player)["balances"]["fiat"], 1_000_000)
        self.assertIn("already been claimed", self.game.command("tutorial claim"))
        self.assertIn("reward was claimed", self.game.command("tutorial"))
        with self.assertRaisesRegex(ValueError, "already has"):
            verification.request(self.player, "different@example.com", clock=lambda: 5000)

    def test_reward_can_be_auctioned_and_transferred_to_another_player(self):
        self.complete_lessons()
        self.verify_email()
        tutorial.claim(self.player)
        parcel = World().snapshot(self.player)["land"][0]["id"]
        buyer = database.create_player("buyer", PASSWORD)
        World().seed(buyer, fiat=10)
        now = int(time.time())
        house = AuctionHouse(clock=lambda: now)
        listing = house.list_asset(self.player, "land", parcel, 1, "fiat", 10, duration=1)
        house.bid(buyer, listing, 10)
        self.assertEqual(World(clock=lambda: now + 1).snapshot(buyer)["land"][0]["id"], parcel)
        with database._connection() as c:
            self.assertEqual(c.execute(
                "SELECT status FROM auctions WHERE id=?", (listing,)
            ).fetchone()[0], "sold")
            self.assertEqual(c.execute(
                "SELECT fiat FROM accounts WHERE player_id=?", (self.player,)
            ).fetchone()[0], 10)

    def test_simultaneous_claims_transfer_exactly_one_acre(self):
        self.complete_lessons()
        self.verify_email()

        def claim(_):
            try:
                return tutorial.claim(self.player)
            except ValueError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(claim, range(4)))
        self.assertEqual(sum("received one acre" in result for result in results), 1)
        self.assertEqual(sum("already been claimed" in result for result in results), 3)
        with database._connection() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tutorial_rewards").fetchone()[0], 1)
            self.assertEqual(c.execute(
                "SELECT sum(acres) FROM land WHERE player_id=?", (self.player,)
            ).fetchone()[0], 1)

    def test_unavailable_bank_land_does_not_consume_claim_or_touch_auction_escrow(self):
        self.complete_lessons()
        self.verify_email()
        with database._connection() as c:
            c.execute("UPDATE land SET locked=1 WHERE player_id=?", (earth_bank_id(c),))
        self.assertIn("Not enough Earth Bank land", self.game.command("tutorial claim"))
        with database._connection() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tutorial_rewards").fetchone()[0], 0)
            self.assertEqual(c.execute(
                "SELECT count(*) FROM land WHERE player_id=?", (self.player,)
            ).fetchone()[0], 0)
            c.execute(
                "UPDATE land SET locked=0 WHERE player_id=? AND id NOT IN "
                "(SELECT CAST(asset AS INTEGER) FROM auctions WHERE kind='land' "
                "AND status IN ('active','pending'))", (earth_bank_id(c),)
            )
        self.assertIn("received one acre", self.game.command("tutorial claim"))

    def test_v6_upgrade_preserves_existing_accounts_without_retroactive_rewards(self):
        with database._connection() as c:
            for table in (
                "tutorial_rewards", "tutorial_verified_emails", "tutorial_email_limits",
                "tutorial_email_requests", "tutorial_progress",
            ):
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
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_tokens_are_private_hashed_account_bound_and_single_use(self):
        output = self.game.command("tutorial email Explorer@Example.com")
        address, token = self.send.call_args.args
        self.assertEqual(address, "explorer@example.com")
        self.assertNotIn(token, output)
        self.assertNotIn(address, self.game.command("status") + self.game.command("tutorial"))
        with database._connection() as c:
            row = c.execute("SELECT * FROM tutorial_email_requests").fetchone()
            self.assertEqual(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
            self.assertNotIn(token, (database.DATA_DIR / "galaxy_resources.db").read_bytes().decode(
                "latin1"
            ))
        other = database.create_player("another", PASSWORD)
        with self.assertRaisesRegex(ValueError, "missing or expired"):
            verification.verify(other, token)
        self.assertIn("Email verified", self.game.command(f"tutorial verify {token}"))
        self.assertIn("already has", self.game.command(f"tutorial verify {token}"))
        with database._connection() as c:
            self.assertIsNone(c.execute(
                "SELECT token_hash FROM tutorial_email_requests WHERE player_id=?", (self.player,)
            ).fetchone()[0])

    def test_expiry_resend_and_persisted_attempt_limit(self):
        verification.request(self.player, "explorer@example.com", clock=lambda: 1000)
        old_token = self.send.call_args.args[1]
        verification.request(self.player, "explorer@example.com", clock=lambda: 1060)
        token = self.send.call_args.args[1]
        with self.assertRaisesRegex(ValueError, "Invalid"):
            verification.verify(self.player, old_token, clock=lambda: 1060)
        for _ in range(verification.MAX_ATTEMPTS - 1):
            with self.assertRaisesRegex(ValueError, "Invalid"):
                verification.verify(self.player, old_token, clock=lambda: 1060)
        with self.assertRaisesRegex(ValueError, "Too many"):
            verification.verify(self.player, token, clock=lambda: 1060)
        verification.request(self.player, "explorer@example.com", clock=lambda: 1120)
        token = self.send.call_args.args[1]
        with self.assertRaisesRegex(ValueError, "expired"):
            verification.verify(self.player, token, clock=lambda: 1120 + verification.TOKEN_LIFETIME)
        with database._connection() as c:
            self.assertEqual(c.execute(
                "SELECT count(*) FROM tutorial_verified_emails"
            ).fetchone()[0], 0)

    def test_common_aliases_cannot_verify_for_multiple_reward_accounts(self):
        self.verify_email(address="First.Last+game@GoogleMail.com")
        other = database.create_player("another", PASSWORD)
        verification.request(other, "firstlast@gmail.com", clock=lambda: 1060)
        token = self.send.call_args.args[1]
        with self.assertRaisesRegex(ValueError, "already linked"):
            verification.verify(other, token, clock=lambda: 1060)
        with database._connection() as c:
            self.assertEqual(c.execute(
                "SELECT count(*) FROM tutorial_verified_emails"
            ).fetchone()[0], 1)
            self.assertIsNone(c.execute(
                "SELECT token_hash FROM tutorial_email_requests WHERE player_id=?", (other,)
            ).fetchone()[0])

    def test_concurrent_verifications_cannot_share_email_identity(self):
        other = database.create_player("another", PASSWORD)
        tokens = []
        for player, now in ((self.player, 1000), (other, 1060)):
            verification.request(player, "same@example.com", clock=lambda: now)
            tokens.append((player, self.send.call_args.args[1]))

        def verify(pair):
            try:
                return verification.verify(*pair, clock=lambda: 1060)
            except ValueError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(verify, tokens))
        self.assertEqual(sum("Email verified" in result for result in results), 1)
        self.assertEqual(sum("already linked" in result for result in results), 1)

    def test_account_send_limits_survive_address_changes_and_reset_after_window(self):
        verification.request(self.player, "first@example.com", clock=lambda: 1000)
        with self.assertRaisesRegex(ValueError, "60 seconds"):
            verification.request(self.player, "other@example.com", clock=lambda: 1059)
        for index in range(1, verification.MAX_SENDS):
            verification.request(
                self.player, f"user{index}@example.com", clock=lambda: 1000 + index * 60
            )
        with self.assertRaisesRegex(ValueError, "daily limit"):
            verification.request(self.player, "new@example.com", clock=lambda: 1300)
        verification.request(self.player, "new@example.com", clock=lambda: 1000 + 86400)
        self.assertEqual(self.send.call_count, verification.MAX_SENDS + 1)

    def test_recipient_send_limits_apply_across_accounts_and_aliases(self):
        for index in range(verification.MAX_SENDS):
            player = self.player if index == 0 else database.create_player(f"user{index}", PASSWORD)
            verification.request(
                player, f"recipient+{index}@example.com", clock=lambda: 1000 + index * 60
            )
        other = database.create_player("another", PASSWORD)
        with self.assertRaisesRegex(ValueError, "daily limit"):
            verification.request(other, "recipient@example.com", clock=lambda: 1300)
        self.assertEqual(self.send.call_count, verification.MAX_SENDS)

    def test_delivery_failure_invalidates_token_keeps_limit_and_hides_smtp_details(self):
        self.send.side_effect = smtplib.SMTPException("private server credentials")
        result = self.game.command("tutorial email explorer@example.com")
        token = self.send.call_args.args[1]
        self.assertIn("could not be sent", result)
        self.assertNotIn("private server credentials", result)
        with self.assertRaisesRegex(ValueError, "missing or expired"):
            verification.verify(self.player, token)
        self.assertIn("60 seconds", self.game.command("tutorial email another@example.com"))
        with database._connection() as c:
            self.assertEqual(c.execute("SELECT sends FROM tutorial_email_limits").fetchone()[0], 1)

    def test_unconfigured_smtp_leaves_completed_tutorial_reward_locked(self):
        self.complete_lessons()
        self.send.side_effect = ValueError("Email delivery is not configured.")
        self.assertIn("check SMTP configuration", self.game.command("tutorial email user@example.com"))
        self.assertIn("Verify an email", self.game.command("tutorial claim"))
        with database._connection() as c:
            self.assertIsNone(c.execute("SELECT token_hash FROM tutorial_email_requests").fetchone()[0])
            self.assertEqual(c.execute("SELECT count(*) FROM tutorial_rewards").fetchone()[0], 0)


class EmailDeliveryTests(unittest.TestCase):
    def test_address_validation_and_alias_canonicalization(self):
        for address, expected in (
            (" First.Last+grm@GoogleMail.com ", "firstlast@gmail.com"),
            ("PERSON+game@EXAMPLE.COM", "person@example.com"),
            ("first.last@example.com", "first.last@example.com"),
        ):
            self.assertEqual(verification.normalize_email(address)[1], expected)
        for address in (
            "not-an-email", "x@example.com\r\nBcc: other@example.com", "@example.com",
            "a..b@example.com", ".abc@example.com", "a@localhost", "a@-example.com",
            "a@exam_ple.com", "+tag@example.com", "x" * 65 + "@example.com", None,
        ):
            with self.subTest(address=address), self.assertRaises(ValueError):
                verification.normalize_email(address)

    def test_smtp_requires_configuration_and_tls(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "not configured"):
            verification.send_verification_email("recipient@example.com", "T" * 32)
        settings = {
            "GRM_SMTP_HOST": "smtp.example.com", "GRM_SMTP_FROM": "game@example.com",
            "GRM_SMTP_USERNAME": "mailer", "GRM_SMTP_PASSWORD": "private smtp password",
        }
        with patch.dict(os.environ, settings, clear=True), patch("email_verification.smtplib.SMTP") as smtp:
            client = smtp.return_value
            client.send_message.return_value = {}
            verification.send_verification_email("recipient@example.com", "T" * 32)
            smtp.assert_called_once_with("smtp.example.com", 587, timeout=10)
            client.starttls.assert_called_once()
            client.login.assert_called_once_with("mailer", "private smtp password")
            message = client.send_message.call_args.args[0]
            self.assertEqual(message["To"], "recipient@example.com")
            self.assertIn("tutorial verify " + "T" * 32, message.get_content())
        for overrides in (
            {"GRM_SMTP_SECURITY": "plaintext"}, {"GRM_SMTP_PORT": "invalid"},
            {"GRM_SMTP_PORT": "0"}, {"GRM_SMTP_PASSWORD": ""},
        ):
            with patch.dict(os.environ, settings | overrides, clear=True), self.assertRaises(ValueError):
                verification.send_verification_email("recipient@example.com", "T" * 32)

    def test_smtp_ssl_and_refused_delivery(self):
        settings = {
            "GRM_SMTP_HOST": "smtp.example.com", "GRM_SMTP_FROM": "game@example.com",
            "GRM_SMTP_SECURITY": "ssl", "GRM_SMTP_PORT": "465",
        }
        with patch.dict(os.environ, settings, clear=True), \
                patch("email_verification.smtplib.SMTP_SSL") as smtp:
            client = smtp.return_value
            client.send_message.return_value = {"recipient@example.com": (550, b"Rejected")}
            with self.assertRaises(smtplib.SMTPException):
                verification.send_verification_email("recipient@example.com", "T" * 32)
            client.starttls.assert_not_called()
            client.login.assert_not_called()
            self.assertEqual(smtp.call_args.args, ("smtp.example.com", 465))


if __name__ == "__main__":
    unittest.main()
