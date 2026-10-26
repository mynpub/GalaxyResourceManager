"""Shared authenticated gameplay commands for local and TCP sessions."""

import json
import shlex
import sqlite3

import database
import catalog
from auctions import AuctionHouse
from blockchain import status as chain_status
from economy import World, integer, ensure_player
from simulation import tick

HELP = """world | land | inventory/resources | workers | skills | jobs | equipment | power/hashrate
land buy ACRES | land sell PARCEL_ID
assign WORKER_ID PARCEL_ID|none
craft solar_panel|asic|droid
equip ITEM_ID install|uninstall|on|off
chain (block schedule, hashrates, queue and your reward ledger)
auctions (Earth market and your last 100 settlement records)
auction list resource|item|land SYMBOL_OR_ID QUANTITY fiat|btc RESERVE [FEE_SATS] [SECONDS]
auction bid AUCTION_ID AMOUNT | auction cancel AUCTION_ID
Prices: integer microfiat or satoshis. Resources: integer micrograms.
Listed land/items must be idle/uninstalled. Bitcoin fees are reserved from the seller.
status/profile | help | quit
New accounts receive nothing. Production bootstrap is unresolved; see README for opt-in local seeding."""


def fixed(value, scale):
    whole, fraction = divmod(value, scale)
    if not fraction:
        return f"{whole}.0"
    return f"{whole}.{fraction:0{len(str(scale))-1}d}".rstrip("0")


def mass(quantity):
    if quantity >= 1_000_000_000:
        return fixed(quantity, 1_000_000_000) + " kg"
    if quantity >= 1_000_000:
        return fixed(quantity, 1_000_000) + " g"
    return fixed(quantity, 1000) + " mg"


def _rows(c, table, player):
    return [dict(row) for row in c.execute(
        f"SELECT * FROM {table} WHERE player_id=? ORDER BY 1", (player,))]


def execute(player, line):
    """Only GameLogic supplies player, from its authenticated session state."""
    if player is None:
        return "Please signup or login first."
    try:
        words = shlex.split(line)
        if not words:
            return "Use help for gameplay commands."
        command, args = words[0].lower(), words[1:]
        if command == "help":
            return HELP
        # Reads also resume bounded offline catch-up; mutators enforce complete catch-up.
        tick()
        world = World()
        market = AuctionHouse()
        if command == "land" and len(args) == 2:
            if args[0] == "buy":
                result = world.buy_land(player, integer(args[1], "acres"))
            elif args[0] == "sell":
                result = world.sell_land(player, integer(args[1], "parcel ID"))
            else:
                raise ValueError("Use land buy ACRES or land sell PARCEL_ID")
            return f"Land updated: {result}"
        if command == "assign" and len(args) == 2:
            world.assign(player, integer(args[0], "worker ID"),
                         None if args[1] == "none" else integer(args[1], "parcel ID"))
            return "Assignment updated."
        if command == "craft" and len(args) == 1:
            return f"Craft job queued: {world.craft(player, args[0])}"
        if command == "equip" and len(args) == 2:
            item = integer(args[0], "item ID")
            if args[1] in ("on", "off"):
                world.set_enabled(player, item, args[1] == "on")
            elif args[1] in ("install", "uninstall"):
                installed = args[1] == "install"
                world.equipment(player, item, installed, installed)
            else:
                raise ValueError("Choose install, uninstall, on or off")
            return "Equipment updated."
        if command == "auction" and args:
            if args[0] == "list" and 6 <= len(args) <= 8:
                fee = integer(args[6], "fee", minimum=0) if len(args) > 6 else 0
                kwargs = {"duration": integer(args[7], "duration")} if len(args) > 7 else {}
                result = market.list_asset(
                    player, args[1], args[2], integer(args[3]), args[4],
                    integer(args[5], "reserve"), fee, **kwargs)
                return f"Auction listed: {result}"
            if args[0] == "bid" and len(args) == 3:
                market.bid(player, integer(args[1], "auction ID"), integer(args[2], "bid"))
                return "Bid escrow reserved."
            if args[0] == "cancel" and len(args) == 2:
                market.cancel(player, integer(args[1], "auction ID"))
                return "Auction cancelled; escrow released."
            raise ValueError("Invalid auction arguments; use help")
        if args:
            raise ValueError("Unexpected arguments; use help")
        if command == "auctions":
            return json.dumps(market.listings(player), indent=2)
        with database._connection() as c:
            ensure_player(c, player)
            if command in ("status", "profile"):
                row = c.execute("SELECT fiat,btc FROM accounts WHERE player_id=?", (player,)).fetchone()
                username = c.execute("SELECT username FROM players WHERE id=?", (player,)).fetchone()[0]
                return (f"Username: {username}\nBalance: {fixed(row['fiat'], 1_000_000)} fiat, "
                        f"{fixed(row['btc'], 100_000_000)} bitcoin (available; excludes escrow)")
            if command == "chain":
                result = chain_status(c, player)
            elif command in ("inventory", "resources"):
                result = _rows(c, "inventory", player)
                for row in result:
                    row["display"] = mass(row["quantity"])
            elif command in ("land", "workers", "skills", "jobs", "equipment"):
                result = _rows(c, command, player)
                if command == "skills":
                    for skill in result:
                        skill["level"] = catalog.level(skill["xp"])
            elif command == "world":
                result = {"active_location": "Earth", "land_acres": 36_800_000_000,
                          "sea_enabled": False,
                          "allocated_acres": c.execute(
                              "SELECT coalesce(sum(acres),0) FROM land WHERE planet='Earth'"
                          ).fetchone()[0]}
            elif command in ("power", "hashrate"):
                result = chain_status(c, player)
                eligible = c.execute(
                    "SELECT * FROM equipment WHERE player_id=? AND installed=1 "
                    "AND enabled=1 AND locked=0 ORDER BY id", (player,)).fetchall()
                watts = sum(row["power_watts"] for row in eligible if row["kind"] == "solar_panel")
                result["solar_watts"] = watts
                powered = []
                for row in eligible:
                    if row["kind"] == "asic" and watts >= row["power_watts"]:
                        watts -= row["power_watts"]
                        powered.append(row["id"])
                result["powered_asic_ids"] = powered
                result["unused_watts"] = watts
            else:
                return "Unknown command. Use help."
        return json.dumps(result, indent=2)
    except ValueError as error:
        return str(error)
    except (sqlite3.Error, OverflowError):
        return "Storage unavailable or amount outside supported range; try again later."
