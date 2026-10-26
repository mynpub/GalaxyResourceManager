"""Planet-local auctions with escrow; Bitcoin settlement uses the global chain."""

import time

import database
from economy import credit, debit, integer, change_resource

DEFAULT_DURATION = 3600
MAX_DURATION = 7 * 86400
CLOSE_BATCH = 1500


def initialize(c):
    c.execute("""
        CREATE TABLE auctions (
            id INTEGER PRIMARY KEY, planet TEXT NOT NULL DEFAULT 'Earth' REFERENCES planets(name),
            seller INTEGER NOT NULL REFERENCES players(id),
            kind TEXT NOT NULL CHECK(kind IN ('resource','item','land')),
            asset TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0),
            currency TEXT NOT NULL CHECK(currency IN ('fiat','btc')),
            reserve INTEGER NOT NULL CHECK(reserve > 0),
            fee INTEGER NOT NULL CHECK(fee >= 0),
            closes INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','pending','sold','cancelled','expired')),
            bidder INTEGER REFERENCES players(id),
            bid INTEGER NOT NULL DEFAULT 0 CHECK(bid >= 0),
            settled_height INTEGER,
            CHECK((bidder IS NULL AND bid = 0) OR (bidder IS NOT NULL AND bid >= reserve)),
            CHECK(currency = 'btc' OR fee = 0),
            CHECK(kind = 'resource' OR quantity = 1)
        )
    """)
    c.execute("CREATE INDEX auction_closing ON auctions(status, closes, id)")
    c.execute("CREATE INDEX auction_queue ON auctions(status, fee DESC, closes, id)")


def _release_asset(c, listing, recipient):
    if listing["kind"] == "resource":
        change_resource(c, recipient, listing["asset"], listing["quantity"])
    elif listing["kind"] == "item":
        c.execute("UPDATE equipment SET player_id=?,locked=0 WHERE id=?",
                  (recipient, int(listing["asset"])))
        c.execute("UPDATE workers SET player_id=?,locked=0 WHERE equipment_id=?",
                  (recipient, int(listing["asset"])))
    else:
        c.execute("UPDATE land SET player_id=?,locked=0 WHERE id=?",
                  (recipient, int(listing["asset"])))


def _refund(c, listing):
    _release_asset(c, listing, listing["seller"])
    if listing["bidder"] is not None:
        credit(c, listing["bidder"], listing["currency"], listing["bid"])
    if listing["fee"]:
        credit(c, listing["seller"], "btc", listing["fee"])


def settle(c, listing, height=None):
    """Consume already-reserved funds/assets, once, under the simulation write lock."""
    if listing["status"] not in ("active", "pending") or listing["bidder"] is None:
        raise ValueError("Auction is not ready for settlement")
    _release_asset(c, listing, listing["bidder"])
    credit(c, listing["seller"], listing["currency"], listing["bid"])
    c.execute("UPDATE auctions SET status='sold',settled_height=? WHERE id=?",
              (height, listing["id"]))
    return listing["fee"]


def close_due(c, now):
    rows = c.execute(
        "SELECT * FROM auctions WHERE status='active' AND closes<=? "
        "ORDER BY closes,id LIMIT ?", (now, CLOSE_BATCH)).fetchall()
    for row in rows:
        if row["bidder"] is None:
            _refund(c, row)
            c.execute("UPDATE auctions SET status='expired' WHERE id=?", (row["id"],))
        elif row["currency"] == "fiat":
            settle(c, row)
        else:
            c.execute("UPDATE auctions SET status='pending' WHERE id=?", (row["id"],))
    return c.execute(
        "SELECT 1 FROM auctions WHERE status='active' AND closes<=? LIMIT 1",
        (now,)).fetchone() is None


class AuctionHouse:
    def __init__(self, clock=time.time):
        self.clock = clock

    def _prepare(self, c):
        from simulation import prepare
        c.execute("BEGIN IMMEDIATE")
        now = int(self.clock())
        prepare(c, now)
        return now

    def list_asset(self, player, kind, asset, quantity, currency, reserve, fee=0,
                   duration=DEFAULT_DURATION):
        quantity = integer(quantity)
        reserve = integer(reserve, "reserve")
        fee = integer(fee, "fee", minimum=0)
        duration = integer(duration, "duration")
        if currency not in ("fiat", "btc") or (currency == "fiat" and fee):
            raise ValueError("Choose fiat or btc; fiat listings have no Bitcoin fee")
        if duration > MAX_DURATION:
            raise ValueError("Auction duration exceeds seven days")
        if kind not in ("resource", "item", "land"):
            raise ValueError("Choose resource, item or land")
        if kind != "resource" and quantity != 1:
            raise ValueError("List one item or land parcel at a time")
        with database._connection() as c:
            now = self._prepare(c)
            if kind == "resource":
                change_resource(c, player, asset, -quantity)
            elif kind == "item":
                asset = integer(asset, "item ID")
                changed = c.execute(
                    "UPDATE equipment SET locked=1 WHERE id=? AND player_id=? "
                    "AND installed=0 AND locked=0 AND NOT EXISTS "
                    "(SELECT 1 FROM workers WHERE equipment_id=equipment.id AND land_id IS NOT NULL)",
                    (asset, player)).rowcount
                if not changed:
                    raise ValueError("Item unavailable; uninstall/unassign it before listing")
                c.execute("UPDATE workers SET locked=1 WHERE equipment_id=?", (asset,))
            else:
                asset = integer(asset, "land ID")
                changed = c.execute(
                    "UPDATE land SET locked=1 WHERE id=? AND player_id=? AND locked=0 "
                    "AND NOT EXISTS(SELECT 1 FROM workers WHERE land_id=land.id)",
                    (asset, player)).rowcount
                if not changed:
                    raise ValueError("Land unavailable; unassign workers before listing")
            if fee:
                debit(c, player, "btc", fee)
            return c.execute(
                "INSERT INTO auctions(seller,kind,asset,quantity,currency,reserve,fee,closes,status) "
                "VALUES(?,?,?,?,?,?,?,?,'active')",
                (player, kind, str(asset), quantity, currency, reserve, fee,
                 now + duration)).lastrowid

    def bid(self, player, listing_id, amount):
        listing_id = integer(listing_id, "auction ID")
        amount = integer(amount, "bid")
        with database._connection() as c:
            self._prepare(c)
            row = c.execute("SELECT * FROM auctions WHERE id=?", (listing_id,)).fetchone()
            if row is None or row["status"] != "active":
                raise ValueError("Auction is not active")
            if row["seller"] == player:
                raise ValueError("Cannot bid on your own listing")
            if amount < row["reserve"] or amount <= row["bid"]:
                raise ValueError("Bid must meet reserve and exceed current bid")
            # A bidder raising their own bid can reuse their existing escrow.
            if row["bidder"] == player:
                debit(c, player, row["currency"], amount - row["bid"])
            else:
                debit(c, player, row["currency"], amount)
                if row["bidder"] is not None:
                    credit(c, row["bidder"], row["currency"], row["bid"])
            c.execute("UPDATE auctions SET bidder=?,bid=? WHERE id=?",
                      (player, amount, listing_id))

    def cancel(self, player, listing_id):
        listing_id = integer(listing_id, "auction ID")
        with database._connection() as c:
            self._prepare(c)
            row = c.execute("SELECT * FROM auctions WHERE id=?", (listing_id,)).fetchone()
            if row is None or row["seller"] != player or row["status"] != "active":
                raise ValueError("Only the seller can cancel an active auction")
            _refund(c, row)
            c.execute("UPDATE auctions SET status='cancelled' WHERE id=?", (listing_id,))

    def listings(self, player):
        with database._connection() as c:
            # Public market, plus only this player's private completed history.
            rows = c.execute(
                "SELECT id,planet,kind,asset,quantity,currency,reserve,fee,closes,status,bid,"
                "settled_height FROM auctions WHERE status IN ('active','pending') "
                "OR seller=? OR bidder=? ORDER BY id DESC LIMIT 100", (player, player))
            return [dict(row) for row in rows]
