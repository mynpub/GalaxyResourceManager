"""Shared authenticated gameplay commands for local and TCP sessions."""

import json
import shlex
import sqlite3

import database
import catalog
import tutorial
from auctions import AuctionHouse
from blockchain import status as chain_status
from economy import World, integer, ensure_player
from simulation import tick

HELP = """world | land | inventory/resources | workers | skills | jobs | equipment | power/hashrate
tutorial [status|next|claim]
work [status|start] (Earth Bank employment; 8-hour shifts)
land buy AUCTION_ID ACRES PRICE_PER_ACRE | land sell PARCEL_ID
assign WORKER_ID PARCEL_ID|none
craft solar_panel|asic|droid
equip ITEM_ID install|uninstall|on|off
chain (block schedule, hashrates, queue and your reward ledger)
auctions (Earth market and your last 100 settlement records)
auction list resource|item|land SYMBOL_OR_ID QUANTITY fiat|btc RESERVE [FEE_SATS] [SECONDS]
auction bid AUCTION_ID AMOUNT [ACRES] | auction cancel AUCTION_ID
bank wealth (Earth Bank fiat, bitcoin and land reserve)
Prices: integer microfiat or satoshis. Resources: integer micrograms.
For Earth Bank land, bid amount is per acre and total escrow is amount times acres.
Listed land/items must be idle/uninstalled. Bitcoin fees are reserved from the seller.
Bank shifts gather resources for Earth Bank and pay fiat at shift completion.
status/profile | help | quit
Finish the tutorial for a one-time fiat reward: current one-acre winning bid plus 10%.
No workers or equipment are granted; work start earns your first wages."""


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
        if command == "tutorial":
            if args in ([], ["status"]):
                return tutorial.show(player)
            if args == ["next"]:
                return tutorial.next_lesson(player)
            if args == ["claim"]:
                return tutorial.claim(player)
            raise ValueError("Use tutorial [status|next|claim]")
        # Reads also resume bounded offline catch-up; mutators enforce complete catch-up.
        tick()
        world = World()
        market = AuctionHouse()
        if command == "work":
            if args == ["start"]:
                shift = world.start_bank_shift(player)
                rate = fixed(
                    shift["wage_numerator"] * 1_000_000 // shift["wage_denominator"],
                    1_000_000
                )
                return (f"Bank shift #{shift['id']} started for 8 hours at "
                        f"{rate} microfiat per work-second.")
            if args in ([], ["status"]):
                shift = world.bank_work_status(player)
                if shift is None:
                    result = "No active Earth Bank work shift."
                else:
                    rate = fixed(
                        shift["wage_numerator"] * 1_000_000 // shift["wage_denominator"],
                        1_000_000
                    )
                    result = (f"Earth Bank shift #{shift['id']} is active until "
                              f"{shift['completes_at']}; locked wage rate: "
                              f"{rate} microfiat per work-second.")
                with database._connection() as c:
                    c.execute("BEGIN IMMEDIATE")
                    tutorial.observe(c, player, "work status")
                return result
            raise ValueError("Use work [status|start]")
        if command == "land" and len(args) == 4 and args[0] == "buy":
            market.bid(player, integer(args[1], "auction ID"),
                       integer(args[3], "price per acre"), integer(args[2], "acres"))
            return "Earth Bank land bid escrow reserved."
        if command == "land" and len(args) == 2:
            if args[0] == "buy":
                raise ValueError("Use land buy AUCTION_ID ACRES PRICE_PER_ACRE")
            elif args[0] == "sell":
                result = world.sell_land(player, integer(args[1], "parcel ID"))
            else:
                raise ValueError(
                    "Use land buy AUCTION_ID ACRES PRICE_PER_ACRE or land sell PARCEL_ID"
                )
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
            if args[0] == "bid" and len(args) in (3, 4):
                acreage = integer(args[3], "acres") if len(args) == 4 else None
                market.bid(player, integer(args[1], "auction ID"),
                           integer(args[2], "bid"), acreage)
                return "Bid escrow reserved."
            if args[0] == "cancel" and len(args) == 2:
                market.cancel(player, integer(args[1], "auction ID"))
                return "Auction cancelled; escrow released."
            raise ValueError("Invalid auction arguments; use help")
        if args and not (command == "bank" and args == ["wealth"]):
            raise ValueError("Unexpected arguments; use help")
        if command == "auctions":
            result = json.dumps(market.listings(player), indent=2)
            with database._connection() as c:
                c.execute("BEGIN IMMEDIATE")
                tutorial.observe(c, player, "auctions")
            return result
        with database._connection() as c:
            ensure_player(c, player)
            if command in ("status", "profile"):
                row = c.execute("SELECT fiat,btc FROM accounts WHERE player_id=?", (player,)).fetchone()
                username = c.execute("SELECT username FROM players WHERE id=?", (player,)).fetchone()[0]
                result = (f"Username: {username}\nBalance: {fixed(row['fiat'], 1_000_000)} fiat, "
                          f"{fixed(row['btc'], 100_000_000)} bitcoin (available; excludes escrow)")
                tutorial.observe(c, player, command)
                return result
            if command == "bank" and args in ([], ["wealth"]):
                bank = c.execute(
                    "SELECT p.id,a.fiat,a.btc FROM system_accounts s "
                    "JOIN players p ON p.id=s.player_id "
                    "JOIN accounts a ON a.player_id=p.id WHERE s.name='Earth Bank'"
                ).fetchone()
                land_acres = c.execute(
                    "SELECT coalesce(sum(acres),0) FROM land WHERE player_id=?",
                    (bank["id"],)
                ).fetchone()[0]
                result = (f"Earth Bank wealth: {fixed(bank['fiat'], 1_000_000)} fiat, "
                          f"{fixed(bank['btc'], 100_000_000)} bitcoin, "
                          f"{land_acres} acres of land.")
                tutorial.observe(c, player, "bank wealth")
                return result
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
            tutorial.observe(c, player, command)
        return json.dumps(result, indent=2)
    except ValueError as error:
        return str(error)
    except (sqlite3.Error, OverflowError):
        return "Storage unavailable or amount outside supported range; try again later."
