"""Planet-local auctions with escrow; Bitcoin settlement uses the global chain."""

import time

import database
from economy import EARTH_BANK_NAME, credit, debit, integer, change_resource

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
            buy_now INTEGER CHECK(buy_now IS NULL OR
                                  (typeof(buy_now)='integer' AND buy_now>=reserve)),
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


def migrate_v9(c):
    """Add an optional fixed price to existing auctions."""
    columns = {row["name"] for row in c.execute("PRAGMA table_info(auctions)")}
    if "buy_now" not in columns:
        c.execute(
            "ALTER TABLE auctions ADD COLUMN buy_now INTEGER "
            "CHECK(buy_now IS NULL OR (typeof(buy_now)='integer' AND buy_now>0))"
        )
    tables = {row["name"] for row in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if {"system_accounts", "land", "chain"} <= tables:
        from economy import EARTH_BANK_NAME, earth_bank_id

        bank = c.execute(
            "SELECT player_id FROM system_accounts WHERE name=?", (EARTH_BANK_NAME,)
        ).fetchone()
        if bank is not None:
            now = c.execute("SELECT last_tick FROM chain WHERE id=1").fetchone()[0]
            if now is not None:
                supply = c.execute(
                    "SELECT coalesce(sum(l.acres),0) FROM auctions a JOIN land l "
                    "ON l.id=CAST(a.asset AS INTEGER) WHERE a.seller=? AND a.kind='land' "
                    "AND a.status IN ('active','pending')",
                    (earth_bank_id(c),)
                ).fetchone()[0]
                if supply:
                    buy_now = integer(
                        (_earth_land_market_price(c, now, supply) * 110 + 99) // 100,
                        "Earth Bank buy now price", 1
                    )
                    c.execute(
                        "UPDATE auctions SET buy_now=? WHERE seller=? AND kind='land' "
                        "AND status='active' AND buy_now IS NULL",
                        (buy_now, bank["player_id"])
                    )


def _earth_land_market_price(c, now, listed_supply):
    from economy import EARTH_BANK_PRICE_WINDOW, LAND_PRICE, integer

    sales = c.execute(
        "SELECT a.bid,l.acres FROM auctions a JOIN land l "
        "ON l.id=CAST(a.asset AS INTEGER) WHERE a.kind='land' AND a.currency='fiat' "
        "AND a.planet='Earth' AND a.status='sold' AND a.closes>=? ORDER BY a.id",
        (now - EARTH_BANK_PRICE_WINDOW,)
    ).fetchall()
    recent_demand = sum(sale["acres"] for sale in sales)
    if not recent_demand:
        return LAND_PRICE
    average_price = sum(sale["bid"] for sale in sales) // recent_demand
    # Strong demand raises the price; weak demand never cuts it below the recent average.
    return integer(average_price * max(recent_demand, listed_supply) // listed_supply,
                   "Earth market price", 1)


def _earth_resource_reserve(c, now, symbol, quantity):
    """Price Bank stock 10% below recent sales, or at labor value without history."""
    import catalog
    from economy import BANK_WORK_WAGE, EARTH_BANK_PRICE_WINDOW, GATHER_RATE, MAX_INTEGER

    sales = c.execute(
        "SELECT bid,quantity FROM auctions WHERE kind='resource' AND asset=? "
        "AND currency='fiat' AND planet='Earth' AND status='sold' AND closes>=?",
        (symbol, now - EARTH_BANK_PRICE_WINDOW)
    ).fetchall()
    # Python integers avoid SQLite sum() overflow on huge resource quantities.
    sold = sum(sale["quantity"] for sale in sales)
    if sold:
        reserve = quantity * sum(sale["bid"] for sale in sales) * 9 // (10 * sold)
    else:
        # Each gatherable element earns an equal share of one work-second's base wage.
        weights = catalog.ABUNDANCE
        total = sum(weights.values())
        elements = sum(1 for weight in weights.values() if weight)
        denominator = elements * GATHER_RATE * weights[symbol]
        reserve = -(-quantity * BANK_WORK_WAGE * total // denominator)
    return min(MAX_INTEGER, max(1, reserve))


def ensure_earth_bank_listing(c, now):
    """Keep Bank land and eligible element stock available on the auction house."""
    from economy import (
        EARTH_BANK_AUCTION_DURATION, EARTH_BANK_LOT_ACRES,
        MAX_INTEGER, earth_bank_id,
    )

    bank_id = earth_bank_id(c)
    active_land = c.execute(
        "SELECT 1 FROM auctions WHERE seller=? AND kind='land' "
        "AND status IN ('active','pending') LIMIT 1", (bank_id,)
    ).fetchone()
    if not active_land:
        parcel = c.execute(
            "SELECT id,acres FROM land WHERE player_id=? AND planet='Earth' AND locked=0 "
            "ORDER BY id LIMIT 1",
            (bank_id,)
        ).fetchone()
        if parcel is not None:
            acres = min(EARTH_BANK_LOT_ACRES, parcel["acres"])
            if acres == parcel["acres"]:
                c.execute("UPDATE land SET locked=1 WHERE id=?", (parcel["id"],))
                listing_parcel = parcel["id"]
            else:
                c.execute("UPDATE land SET acres=acres-? WHERE id=?", (acres, parcel["id"]))
                listing_parcel = c.execute(
                    "INSERT INTO land(player_id,acres,locked) VALUES (?,?,1)",
                    (bank_id, acres)
                ).lastrowid

            listed_supply = c.execute(
                "SELECT coalesce(sum(l.acres),0) FROM auctions a JOIN land l "
                "ON l.id=CAST(a.asset AS INTEGER) WHERE a.seller=? AND a.kind='land' "
                "AND a.planet='Earth' "
                "AND a.status IN ('active','pending')", (bank_id,)
            ).fetchone()[0] + acres
            adjusted_price = _earth_land_market_price(c, now, listed_supply)
            adjusted_price = integer(adjusted_price, "Earth market price", 1)
            reserve = max(1, adjusted_price * 9 // 10)
            buy_now = (adjusted_price * 110 + 99) // 100
            if reserve > MAX_INTEGER or buy_now > MAX_INTEGER:
                raise OverflowError("Earth Bank reserve exceeds supported integer range")
            c.execute(
                "INSERT INTO auctions(seller,kind,asset,quantity,currency,reserve,buy_now,"
                "fee,closes,status) VALUES(?, 'land', ?, 1, 'fiat', ?, ?, 0, ?, 'active')",
                (bank_id, str(listing_parcel), reserve, buy_now,
                 now + EARTH_BANK_AUCTION_DURATION)
            )

    if c.execute(
        "SELECT 1 FROM bank_work_shifts WHERE status='running' LIMIT 1"
    ).fetchone():
        return
    stocks = c.execute(
        "SELECT symbol,quantity FROM inventory WHERE player_id=? AND quantity>0 ORDER BY symbol",
        (bank_id,)
    ).fetchall()
    for stock in stocks:
        if c.execute(
            "SELECT 1 FROM auctions WHERE seller=? AND kind='resource' AND asset=? "
            "AND status IN ('active','pending') LIMIT 1", (bank_id, stock["symbol"])
        ).fetchone():
            continue
        c.execute("UPDATE inventory SET quantity=0 WHERE player_id=? AND symbol=?",
                  (bank_id, stock["symbol"]))
        c.execute(
            "INSERT INTO auctions(seller,kind,asset,quantity,currency,reserve,fee,closes,status) "
            "VALUES(?, 'resource', ?, ?, 'fiat', ?, 0, ?, 'active')",
            (bank_id, stock["symbol"], stock["quantity"],
             _earth_resource_reserve(c, now, stock["symbol"], stock["quantity"]),
             now + EARTH_BANK_AUCTION_DURATION)
        )


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
    bank = c.execute(
        "SELECT 1 FROM system_accounts WHERE name=? AND player_id=?",
        (EARTH_BANK_NAME, listing["seller"])
    ).fetchone()
    if listing["kind"] == "land" and bank:
        if not listing["bid_quantity"]:
            raise ValueError("Earth Bank auction has no selected acreage")
        parcel = c.execute(
            "SELECT acres FROM land WHERE id=? AND player_id=? AND planet='Earth' AND locked=1",
            (int(listing["asset"]), listing["seller"])
        ).fetchone()
        if parcel is None or not 1 <= listing["bid_quantity"] <= min(1000, parcel["acres"]):
            raise ValueError("Earth Bank auction acreage is unavailable")
        bought = listing["bid_quantity"]
        if bought < parcel["acres"]:
            c.execute("UPDATE land SET player_id=?,acres=?,locked=0 WHERE id=?",
                      (listing["bidder"], bought, int(listing["asset"])))
            c.execute("INSERT INTO land(player_id,acres) VALUES (?,?)",
                      (listing["seller"], parcel["acres"] - bought))
        else:
            c.execute("UPDATE land SET player_id=?,locked=0 WHERE id=?",
                      (listing["bidder"], int(listing["asset"])))
    else:
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
                   duration=DEFAULT_DURATION, buy_now=None):
        quantity = integer(quantity)
        reserve = integer(reserve, "reserve")
        buy_now = None if buy_now is None else integer(buy_now, "buy now price")
        if buy_now is not None and buy_now < reserve:
            raise ValueError("Buy now price must be at least the reserve")
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
                if c.execute(
                    "SELECT 1 FROM workers w JOIN droid_tutorial t ON t.worker_id=w.id "
                    "WHERE w.equipment_id=? AND w.player_id=?", (asset, player)
                ).fetchone():
                    raise ValueError("Your tutorial droid cannot be traded")
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
                "INSERT INTO auctions(seller,kind,asset,quantity,currency,reserve,buy_now,"
                "fee,closes,status) VALUES(?,?,?,?,?,?,?,?,?,'active')",
                (player, kind, str(asset), quantity, currency, reserve, buy_now, fee,
                 now + duration)).lastrowid

    def bid(self, player, listing_id, amount, acreage=None):
        listing_id = integer(listing_id, "auction ID")
        amount = integer(amount, "bid")
        with database._connection() as c:
            self._prepare(c)
            row = c.execute("SELECT * FROM auctions WHERE id=?", (listing_id,)).fetchone()
            if row is None or row["status"] != "active":
                raise ValueError("Auction is not active")
            if row["seller"] == player:
                raise ValueError("Cannot bid on your own listing")
            bank = c.execute(
                "SELECT 1 FROM system_accounts WHERE name=? AND player_id=?",
                (EARTH_BANK_NAME, row["seller"])
            ).fetchone()
            if row["kind"] == "land" and bank:
                if acreage is None:
                    raise ValueError("Earth Bank bids require a per-acre price and acreage")
                acreage = integer(acreage, "acres")
                parcel = c.execute(
                    "SELECT acres FROM land WHERE id=? AND player_id=? "
                    "AND planet='Earth' AND locked=1",
                    (int(row["asset"]), row["seller"])
                ).fetchone()
                if parcel is None or acreage > min(1000, parcel["acres"]):
                    raise ValueError("Earth Bank listing allows 1-1000 acres")
                if amount < row["reserve"] or (
                    row["bidder"] is not None and amount <= row["bid"] // row["bid_quantity"]
                ):
                    raise ValueError("Per-acre bid must meet reserve and exceed current bid")
                if row["buy_now"] is not None and amount >= row["buy_now"]:
                    raise ValueError("Bid must be below buy now; use auction buy instead")
                total_bid = integer(amount * acreage, "total bid")
                bid_quantity = acreage
            else:
                if acreage is not None:
                    raise ValueError("Acreage selection is only available on Earth Bank listings")
                if amount < row["reserve"] or amount <= row["bid"]:
                    raise ValueError("Bid must meet reserve and exceed current bid")
                if row["buy_now"] is not None and amount >= row["buy_now"]:
                    raise ValueError("Bid must be below buy now; use auction buy instead")
                total_bid = amount
                bid_quantity = 0

            if row["bidder"] == player:
                difference = total_bid - row["bid"]
                if difference > 0:
                    debit(c, player, row["currency"], difference)
                elif difference < 0:
                    credit(c, player, row["currency"], -difference)
            else:
                debit(c, player, row["currency"], total_bid)
                if row["bidder"] is not None:
                    credit(c, row["bidder"], row["currency"], row["bid"])
            c.execute(
                "UPDATE auctions SET bidder=?,bid=?,bid_quantity=? WHERE id=?",
                (player, total_bid, bid_quantity, listing_id)
            )

    def buy_now(self, player, listing_id, acreage=None):
        """Buy at the optional fixed price, immediately for fiat or in the next BTC block."""
        listing_id = integer(listing_id, "auction ID")
        with database._connection() as c:
            self._prepare(c)
            row = c.execute("SELECT * FROM auctions WHERE id=?", (listing_id,)).fetchone()
            if row is None or row["status"] != "active":
                raise ValueError("Auction is not active")
            if row["seller"] == player:
                raise ValueError("Cannot buy your own listing")
            if row["buy_now"] is None:
                raise ValueError("This auction has no buy now price")
            bank = c.execute(
                "SELECT 1 FROM system_accounts WHERE name=? AND player_id=?",
                (EARTH_BANK_NAME, row["seller"])
            ).fetchone()
            bid_quantity = 0
            if row["kind"] == "land" and bank:
                if acreage is None:
                    raise ValueError("Earth Bank land purchases require an acreage amount")
                acreage = integer(acreage, "acres")
                parcel = c.execute(
                    "SELECT acres FROM land WHERE id=? AND player_id=? "
                    "AND planet='Earth' AND locked=1",
                    (int(row["asset"]), row["seller"])
                ).fetchone()
                if parcel is None or acreage > min(1000, parcel["acres"]):
                    raise ValueError("Earth Bank listing allows 1-1000 acres")
                bid_quantity = acreage
                total = integer(row["buy_now"] * acreage, "buy now total")
            else:
                if acreage is not None:
                    raise ValueError("Acreage selection is only available on Earth Bank listings")
                total = row["buy_now"]

            if row["bidder"] == player:
                difference = total - row["bid"]
                if difference > 0:
                    debit(c, player, row["currency"], difference)
                elif difference < 0:
                    credit(c, player, row["currency"], -difference)
            else:
                debit(c, player, row["currency"], total)
                if row["bidder"] is not None:
                    credit(c, row["bidder"], row["currency"], row["bid"])

            status = "active" if row["currency"] == "fiat" else "pending"
            c.execute(
                "UPDATE auctions SET bidder=?,bid=?,bid_quantity=?,status=? WHERE id=?",
                (player, total, bid_quantity, status, listing_id)
            )
            if row["currency"] == "fiat":
                settle(c, c.execute("SELECT * FROM auctions WHERE id=?", (listing_id,)).fetchone())
                return "sold"
            return "pending"

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
                "SELECT a.id,a.planet,a.kind,a.asset,a.quantity,a.currency,a.reserve,a.buy_now,a.fee,"
                "a.closes,a.status,a.bid,a.bid_quantity,a.settled_height,"
                "l.acres AS land_acres,coalesce(p.username,s.name) AS seller_name FROM auctions a "
                "LEFT JOIN players p ON p.id=a.seller "
                "LEFT JOIN system_accounts s ON s.player_id=a.seller "
                "LEFT JOIN land l ON a.kind='land' AND l.id=CAST(a.asset AS INTEGER) "
                "WHERE a.status IN ('active','pending') OR a.seller=? OR a.bidder=? "
                "ORDER BY a.id DESC LIMIT 100", (player, player)).fetchall()
            result = []
            for row in rows:
                listing = dict(row)
                if (listing["seller_name"] == EARTH_BANK_NAME
                        and listing["kind"] == "land"):
                    listing["reserve_per_acre"] = listing["reserve"]
                    listing["buy_now_per_acre"] = listing["buy_now"]
                    listing["bid_per_acre"] = (
                        listing["bid"] // listing["bid_quantity"]
                        if listing["bid_quantity"] else None
                    )
                result.append(listing)
            return result
