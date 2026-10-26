"""Transactional auction, chain and simulation integration in temporary worlds."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import auctions
import blockchain
import database
import economy
import simulation


class MarketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_patch = patch.object(database, "DATA_DIR", Path(temporary.name))
        data_patch.start()
        self.addCleanup(data_patch.stop)
        database.init_database()
        self.now = 1000
        self.clock = lambda: self.now
        self.market = auctions.AuctionHouse(self.clock)
        self.world = economy.World(self.clock)
        with database._connection() as c:
            for pid in (1, 2, 3):
                c.execute("INSERT INTO players(id,username,password_hash,created_at) VALUES(?,?,?,?)",
                          (pid, f"player{pid}", "unused-test-hash", "test"))
                economy.ensure_player(c, pid)
                economy.credit(c, pid, "fiat", 100_000_000)
                economy.credit(c, pid, "btc", 100_000)
                economy.change_resource(c, pid, "Fe", 100_000)
        simulation.tick(self.clock)

    def query(self, sql, args=()):
        with database._connection() as c:
            return [dict(row) for row in c.execute(sql, args)]

    def balance(self, player, currency="btc"):
        return self.query(f"SELECT {currency} AS value FROM accounts WHERE player_id=?", (player,))[0]["value"]

    def resource(self, player):
        return self.query("SELECT quantity FROM inventory WHERE player_id=? AND symbol='Fe'", (player,))[0]["quantity"]

    def miner(self, player):
        with database._connection() as c:
            for kind in ("solar_panel", "asic"):
                c.execute("INSERT INTO equipment(player_id,kind,installed,enabled) VALUES(?,?,1,1)",
                          (player, kind))

    def test_resource_bid_fee_escrow_outbid_cancel_and_replay(self):
        listing = self.market.list_asset(1, "resource", "Fe", 50, "btc", 100, 7)
        self.assertEqual(self.resource(1), 99_950)
        self.assertEqual(self.balance(1), 99_993)
        with self.assertRaises(ValueError):
            self.market.bid(2, listing, 99)
        self.market.bid(2, listing, 100)
        self.market.bid(2, listing, 120)
        self.assertEqual(self.balance(2), 99_880)
        self.market.bid(3, listing, 130)
        self.assertEqual(self.balance(2), 100_000)
        self.assertEqual(self.balance(3), 99_870)
        with self.assertRaises(ValueError):
            self.market.cancel(2, listing)
        self.market.cancel(1, listing)
        self.assertEqual(self.resource(1), 100_000)
        self.assertEqual(self.balance(1), 100_000)
        self.assertEqual(self.balance(3), 100_000)
        with self.assertRaises(ValueError):
            self.market.cancel(1, listing)
        with self.assertRaises(ValueError):
            self.market.bid(2, listing, 140)

    def test_fee_insolvency_and_insufficient_bid_roll_back_entire_operation(self):
        with self.assertRaises(ValueError):
            self.market.list_asset(1, "resource", "Fe", 50, "btc", 100, 100_001)
        self.assertEqual(self.resource(1), 100_000)
        self.assertEqual(self.query("SELECT * FROM auctions"), [])
        listing = self.market.list_asset(1, "resource", "Fe", 50, "btc", 100)
        self.market.bid(2, listing, 100)
        with self.assertRaises(ValueError):
            self.market.bid(3, listing, 100_001)
        self.assertEqual(self.balance(2), 99_900)
        self.assertEqual(self.balance(3), 100_000)
        self.assertEqual(self.query("SELECT bidder,bid FROM auctions")[0], {"bidder": 2, "bid": 100})

    def test_pending_cancellation_cutoff_restart_settlement_and_no_double_credit(self):
        listing = self.market.list_asset(1, "resource", "Fe", 50, "btc", 100, 7, duration=10)
        self.market.bid(2, listing, 100)
        self.now += 10
        simulation.tick(self.clock)
        self.assertEqual(self.query("SELECT status FROM auctions")[0]["status"], "pending")
        with self.assertRaises(ValueError):
            self.market.cancel(1, listing)
        self.assertEqual(self.resource(2), 100_000)
        database.init_database()
        self.now = 1600
        simulation.tick(self.clock)
        self.assertEqual(self.resource(2), 100_050)
        self.assertEqual(self.balance(1), 100_093)
        self.assertEqual(self.query("SELECT fee_carry,minted FROM chain")[0],
                         {"fee_carry": 7, "minted": 0})
        simulation.tick(self.clock)
        self.assertEqual(len(self.query("SELECT * FROM blocks")), 1)
        self.assertEqual(self.balance(1), 100_093)

    def test_fiat_close_and_no_bid_expiry_do_not_use_block_capacity(self):
        first = self.market.list_asset(1, "resource", "Fe", 50, "fiat", 100, duration=10)
        self.market.bid(2, first, 110)
        self.market.list_asset(1, "resource", "Fe", 20, "btc", 50, 9, duration=10)
        self.now += 10
        simulation.tick(self.clock)
        self.assertEqual(self.balance(1, "fiat"), 100_000_110)
        self.assertEqual(self.resource(1), 99_950)
        self.assertEqual(self.resource(2), 100_050)
        self.assertEqual(self.balance(1), 100_000)
        self.assertEqual([r["status"] for r in self.query("SELECT status FROM auctions ORDER BY id")],
                         ["sold", "expired"])
        self.assertFalse(self.query("SELECT * FROM blocks"))

    def test_item_and_land_locks_authorization_and_atomic_transfer(self):
        parcel = self.world.buy_land(1, 2)
        self.world.seed(1, humans=1, solar_panels=1)
        worker = self.query("SELECT id FROM workers WHERE player_id=1")[0]["id"]
        self.world.assign(1, worker, parcel)
        with self.assertRaises(ValueError):
            self.market.list_asset(1, "land", parcel, 1, "fiat", 1)
        self.world.assign(1, worker, None)
        land_sale = self.market.list_asset(1, "land", parcel, 1, "fiat", 1, duration=10)
        with self.assertRaises(ValueError):
            self.world.assign(1, worker, parcel)
        with self.assertRaises(ValueError):
            self.world.sell_land(1, parcel)
        item = self.query("SELECT id FROM equipment WHERE player_id=1")[0]["id"]
        self.world.equipment(1, item, True, True)
        with self.assertRaises(ValueError):
            self.market.list_asset(1, "item", item, 1, "fiat", 1)
        self.world.equipment(1, item, False, False)
        item_sale = self.market.list_asset(1, "item", item, 1, "fiat", 1, duration=10)
        with self.assertRaises(ValueError):
            self.world.equipment(1, item, True, True)
        with self.assertRaises(ValueError):
            self.market.list_asset(1, "item", item, 1, "fiat", 1)
        for sale in (land_sale, item_sale):
            self.market.bid(2, sale, 1)
        self.now += 10
        simulation.tick(self.clock)
        self.assertEqual(self.query("SELECT player_id,locked FROM land WHERE id=?", (parcel,))[0],
                         {"player_id": 2, "locked": 0})
        self.assertEqual(self.query("SELECT player_id,locked FROM equipment WHERE id=?", (item,))[0],
                         {"player_id": 2, "locked": 0})
        with self.assertRaises(ValueError):
            self.world.equipment(1, item, True, True)
        with database._connection() as c:
            self.assertFalse(c.execute("PRAGMA foreign_key_check").fetchall())

    def test_global_1500_fee_priority_ties_rollover_and_exact_pool(self):
        self.miner(3)
        # Construct a valid large pending queue in one fixture transaction, with
        # the same resource, bid and seller-fee reserves as the public API.
        with database._connection() as c:
            economy.change_resource(c, 1, "Fe", -1501)
            economy.debit(c, 1, "btc", 1509)
            economy.debit(c, 2, "btc", 1501)
            for index in range(1501):
                c.execute(
                    "INSERT INTO auctions(seller,kind,asset,quantity,currency,reserve,fee,"
                    "closes,status,bidder,bid) VALUES(1,'resource','Fe',1,'btc',1,?,1001,'pending',2,1)",
                    (9 if index == 1500 else 1,))
        self.now = 1600
        simulation.tick(self.clock)
        first = self.query("SELECT * FROM blocks")[0]
        self.assertEqual(first["transactions"], 1500)
        self.assertEqual(first["fees"], 1508)
        self.assertEqual(self.query("SELECT id FROM auctions WHERE status='pending'"), [{"id": 1500}])
        self.assertEqual(self.resource(2), 101_500)
        self.assertEqual(self.balance(3), 100_000 + blockchain.subsidy(1) + 1508)
        self.now = 2200
        simulation.tick(self.clock)
        self.assertEqual(self.query("SELECT transactions FROM blocks ORDER BY height"),
                         [{"transactions": 1500}, {"transactions": 1}])
        self.assertEqual(self.resource(2), 101_501)
        self.assertFalse(self.query("SELECT id FROM auctions WHERE status='pending'"))

    def test_droid_escrow_locks_work_and_transfers_linked_worker(self):
        self.world.seed(1, droids=1)
        parcel = self.world.buy_land(1, 1)
        worker = self.query("SELECT * FROM workers WHERE player_id=1")[0]
        item = worker["equipment_id"]
        self.world.assign(1, worker["id"], parcel)
        with self.assertRaises(ValueError):
            self.market.list_asset(1, "item", item, 1, "fiat", 1)
        self.world.assign(1, worker["id"], None)
        listing = self.market.list_asset(1, "item", item, 1, "fiat", 1, duration=10)
        with self.assertRaises(ValueError):
            self.world.assign(1, worker["id"], parcel)
        self.market.bid(2, listing, 1)
        self.now += 10
        simulation.tick(self.clock)
        self.assertEqual(self.query("SELECT player_id,locked,land_id FROM workers WHERE id=?",
                                    (worker["id"],))[0],
                         {"player_id": 2, "locked": 0, "land_id": None})
        with self.assertRaises(ValueError):
            self.world.assign(1, worker["id"], parcel)
        buyer_parcel = self.world.buy_land(2, 1)
        self.world.assign(2, worker["id"], buyer_parcel)

    def test_time_weighting_prevents_last_second_snapshot_and_survives_restart(self):
        self.miner(1)
        self.now = 1599
        simulation.tick(self.clock)
        self.miner(2)
        database.init_database()
        self.now = 1600
        simulation.tick(self.clock)
        payout = blockchain.shares(blockchain.subsidy(1), {1: 600, 2: 1})
        self.assertEqual(self.balance(1), 100_000 + payout[1])
        self.assertEqual(self.balance(2), 100_000 + payout[2])
        self.assertEqual(sum(r["amount"] for r in self.query("SELECT amount FROM rewards")),
                         blockchain.subsidy(1))

    def test_failed_block_rolls_back_transfer_fee_hash_and_schedule(self):
        self.miner(3)
        sale = self.market.list_asset(1, "resource", "Fe", 50, "btc", 100, 7, duration=1)
        self.market.bid(2, sale, 100)
        self.now = 1001
        simulation.tick(self.clock)
        self.now = 1600
        original = auctions.settle

        def failing_settlement(c, row, height=None):
            original(c, row, height)
            raise RuntimeError("simulated interruption after asset transfer")

        with patch.object(auctions, "settle", side_effect=failing_settlement), \
                self.assertRaises(RuntimeError):
            simulation.tick(self.clock)
        self.assertEqual(self.resource(2), 100_000)
        self.assertEqual(self.balance(1), 99_993)
        self.assertEqual(self.query("SELECT status FROM auctions")[0]["status"], "pending")
        self.assertFalse(self.query("SELECT * FROM blocks"))
        self.assertFalse(self.query("SELECT * FROM rewards"))
        self.assertEqual(self.query("SELECT last_tick,height FROM chain")[0],
                         {"last_tick": 1001, "height": 0})
        simulation.tick(self.clock)
        self.assertEqual(self.resource(2), 100_050)
        self.assertEqual(self.balance(1), 100_093)
        self.assertEqual(len(self.query("SELECT * FROM blocks")), 1)

    def test_no_hash_unissued_subsidy_and_fees_pay_after_subsidy_ends(self):
        listing = self.market.list_asset(1, "resource", "Fe", 50, "btc", 1, 9, duration=1)
        self.market.bid(2, listing, 1)
        self.now = 1600
        simulation.tick(self.clock)
        self.assertEqual(self.query("SELECT subsidy,unissued,fees,paid FROM blocks")[0],
                         {"subsidy": 0, "unissued": blockchain.subsidy(1), "fees": 9, "paid": 0})
        self.miner(3)
        with database._connection() as c:
            # Jump the test chain to the finite-subsidy boundary, not twenty-year sleeps.
            height = 33 * blockchain.HALVING
            current = 1000 + height * 600
            c.execute("UPDATE chain SET height=?,last_tick=? WHERE id=1", (height, current))
        self.now = current + 600
        simulation.tick(self.clock)
        self.assertEqual(self.balance(3), 100_009)
        last = self.query("SELECT * FROM blocks ORDER BY height DESC LIMIT 1")[0]
        self.assertEqual(last["subsidy"], 0)
        self.assertEqual(last["paid"], 9)

    def test_bounded_catchup_commit_progress_and_concurrent_idempotency(self):
        self.now += 600 * 300
        with database._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            self.assertFalse(simulation.catch_up(c, self.now, max_events=2))
        partial = self.query("SELECT last_tick FROM chain")[0]["last_tick"]
        self.assertLess(partial, self.now)
        with self.assertRaises(ValueError):
            self.market.list_asset(1, "resource", "Fe", 1, "fiat", 1)
        self.assertGreater(self.query("SELECT last_tick FROM chain")[0]["last_tick"], partial)
        self.assertFalse(self.query("SELECT * FROM auctions"))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: simulation.tick(self.clock), range(8)))
        self.assertEqual(len(self.query("SELECT * FROM blocks")), 300)
        self.assertEqual(self.query("SELECT height,last_tick FROM chain")[0],
                         {"height": 300, "last_tick": self.now})

    def test_competing_bids_are_serialized_without_lost_escrow(self):
        sale = self.market.list_asset(1, "resource", "Fe", 1, "btc", 100)

        def bid(player, amount):
            try:
                self.market.bid(player, sale, amount)
            except ValueError:
                pass

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(bid, 2, 100)
            b = pool.submit(bid, 3, 110)
            a.result()
            b.result()
        self.assertEqual(self.query("SELECT bidder,bid FROM auctions")[0], {"bidder": 3, "bid": 110})
        self.assertEqual(self.balance(2), 100_000)
        self.assertEqual(self.balance(3), 99_890)


if __name__ == "__main__":
    unittest.main()
