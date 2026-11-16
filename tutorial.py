"""Persistent mechanics lessons and a market-priced, one-time fiat reward."""

import json
import time

import database
from economy import RECIPES, World, _queue_craft, credit, earth_bank_id, integer

LESSONS = (
    (
        "Your account and Earth",
        "Earth has a fixed land supply. You start without assets; balances use "
        "microfiat (1 fiat = 1,000,000) and satoshis (1 BTC = 100,000,000). "
        "Inspect your balances with status and the planet with world.",
        ("status", "world"),
    ),
    (
        "Earning your first wages",
        "work start begins an 8-hour Earth Bank shift without land or workers. "
        "The Bank keeps the gathered elements; your fiat wages arrive only at "
        "shift completion. Offline time counts, but you must start each new shift. "
        "Inspect work status now; starting a shift is optional for this lesson.",
        ("work status",),
    ),
    (
        "Land, workers and gathering",
        "Inspect land, workers and inventory. Owned workers gather only when "
        "assigned to your land: assign WORKER_ID PARCEL_ID. Each acre holds one "
        "worker; humans and droids gather 1 mg/second of crust-weighted elements. "
        "Resources are stored in micrograms. The tutorial rewards fiat to bid on "
        "land, not a worker; Bank shifts do not fill your personal inventory.",
        ("land", "workers", "inventory"),
    ),
    (
        "Crafting and skills",
        "Inspect skills, jobs and equipment. Once you have the required elements, "
        "craft solar_panel, craft asic or craft droid queues a timed job and "
        "consumes its inputs. Completed jobs award equipment and crafting XP; "
        "gathering and Bank work award mining XP. Empty lists are normal now.",
        ("skills", "jobs", "equipment"),
    ),
    (
        "Power and simulated Bitcoin",
        "Inspect power and chain. Use equip ITEM_ID install and equip ITEM_ID on "
        "to run solar panels and ASICs. An ASIC needs enough solar power; only "
        "powered hashrate earns simulated block rewards. Blocks arrive every "
        "10 real minutes and offline time counts. These balances are in-game, "
        "not a real Bitcoin wallet.",
        ("power", "chain"),
    ),
    (
        "Trading and your first acre",
        "Inspect auctions and bank wealth. Earth Bank land auctions close after "
        "one hour: land buy AUCTION_ID ACRES PRICE_PER_ACRE reserves the total "
        "bid immediately. The highest per-acre bid wins. Player auctions trade "
        "idle land, elements and uninstalled equipment; Bitcoin settlement waits "
        "for block inclusion. Optional buy-now prices let you purchase early with "
        "`auction buy`; the Bank's fixed land price is 10% above market. After "
        "this lesson, use tutorial claim for enough "
        "fiat to cover the current minimum winning bid for one acre plus 10%. "
        "Then place a land buy bid; land is not granted automatically. "
        "Later competing bids can still outbid you.",
        ("auctions", "bank wealth"),
    ),
)

WELCOME = (
    "Tutorial started! Use tutorial to see your current lesson, then try its "
    "commands and use tutorial next. Progress survives logout. Finish all "
    "lessons to claim a one-time fiat reward priced for one acre plus 10%. "
    "Local players can continue with --play."
)


def initialize(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS tutorial_progress (
            player_id INTEGER PRIMARY KEY REFERENCES players(id),
            step INTEGER NOT NULL DEFAULT 0 CHECK(step BETWEEN 0 AND 6),
            seen TEXT NOT NULL DEFAULT '[]'
        )
    """)
    c.execute("""
        CREATE TABLE tutorial_fiat_rewards (
            player_id INTEGER PRIMARY KEY REFERENCES tutorial_progress(player_id),
            auction_id INTEGER NOT NULL REFERENCES auctions(id),
            minimum_bid INTEGER NOT NULL CHECK(minimum_bid>0),
            amount INTEGER NOT NULL CHECK(amount>minimum_bid),
            claimed_at INTEGER NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE tutorial_legacy_claims (
            player_id INTEGER PRIMARY KEY REFERENCES tutorial_progress(player_id)
        )
    """)
    if c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tutorial_rewards'"
    ).fetchone():
        c.execute("INSERT INTO tutorial_legacy_claims SELECT player_id FROM tutorial_rewards")


def initialize_droid(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS droid_tutorial (
            player_id INTEGER PRIMARY KEY REFERENCES players(id),
            job_id INTEGER UNIQUE REFERENCES jobs(id),
            worker_id INTEGER UNIQUE REFERENCES workers(id),
            name TEXT CHECK(name IS NULL OR length(name) BETWEEN 1 AND 32),
            completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1)),
            CHECK(job_id IS NULL OR name IS NOT NULL)
        )
    """)


def droid_status(player, clock=time.time):
    with World(clock)._transaction(player) as c:
        row = c.execute("SELECT * FROM droid_tutorial WHERE player_id=?", (player,)).fetchone()
        if row is None:
            return (
                "Droid tutorial: learn to craft and assign a worker to your land. "
                "Use tutorial droid start to begin. Starter materials are only usable "
                "for this one droid and cannot be traded. The named droid is also untradeable."
            )
        if row["completed"]:
            return f"Droid tutorial complete! {row['name']} has been assigned to your land."
        if row["job_id"] is None:
            recipe = RECIPES["droid"]
            inputs = ", ".join(f"{symbol}: {amount}" for symbol, amount in recipe["inputs"].items())
            return (
                f"Droid recipe (micrograms): {inputs}. Normal crafting consumes these "
                f"resources and takes {recipe['duration']} seconds. Use land to find an "
                'unlocked parcel you own, then tutorial droid craft "NAME" to name '
                "and craft your droid using your one-time tutorial materials. "
                "The materials and the finished droid cannot be traded."
            )
        if row["worker_id"] is None:
            job = c.execute("SELECT completes_at FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            return (
                f"Droid craft job #{row['job_id']} for {row['name']} is in progress "
                f"until {job['completes_at']}. "
                "Use jobs to check it; offline time counts. Then use tutorial droid."
            )
        worker = c.execute("SELECT player_id FROM workers WHERE id=?", (row["worker_id"],)).fetchone()
        if worker is None or worker["player_id"] != player:
            return (
                "Your tutorial droid is no longer yours, so it cannot be assigned for "
                "this lesson. Tutorial materials cannot be claimed again."
            )
        return (
            f"Your droid worker is #{row['worker_id']}, named {row['name']}. "
            "This droid is untradeable. Use land to find an unlocked "
            f"parcel with room, then assign {row['worker_id']} PARCEL_ID. Each acre "
            "holds one worker. Check workers and inventory; assigned droids gather "
            "automatically, including offline."
        )


def start_droid(player, clock=time.time):
    with World(clock)._transaction(player) as c:
        c.execute("INSERT OR IGNORE INTO droid_tutorial(player_id) VALUES (?)", (player,))
    return droid_status(player, clock)


def craft_droid(player, name, clock=time.time):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 32 \
            or not name.isprintable():
        raise ValueError("Droid name must be 1-32 printable characters.")
    name = name.strip()
    with World(clock)._transaction(player) as c:
        row = c.execute("SELECT job_id FROM droid_tutorial WHERE player_id=?", (player,)).fetchone()
        if row is None:
            raise ValueError("Start the droid tutorial first: tutorial droid start")
        if row["job_id"] is not None:
            raise ValueError("Your one-time tutorial droid has already been queued.")
        if not c.execute(
            "SELECT 1 FROM land WHERE player_id=? AND locked=0 AND acres>0", (player,)
        ).fetchone():
            raise ValueError("You need unlocked land before using the tutorial materials.")
        job_id = _queue_craft(c, player, "droid")
        c.execute("UPDATE droid_tutorial SET job_id=?,name=? WHERE player_id=?",
                  (job_id, name, player))
        return (
            f"Tutorial droid craft job #{job_id} for {name} queued using your one-time materials. "
            "Use jobs to check progress, then tutorial droid for the next step."
        )


def progress(c, player):
    row = c.execute(
        "SELECT * FROM tutorial_progress WHERE player_id=?", (player,)
    ).fetchone()
    if row is None:
        raise ValueError("Tutorial rewards are available to accounts created after this update.")
    return row


def lesson_text(step, seen=()):
    if step == len(LESSONS):
        return (
            "All tutorial lessons complete! Use tutorial claim for a one-time "
            "fiat reward covering the current minimum winning bid for one acre "
            "plus 10%. No email is required. You must still bid and win an auction."
        )
    title, explanation, required = LESSONS[step]
    remaining = [command for command in required if command not in seen]
    instruction = (
        "Try: " + " | ".join(remaining) + ", then tutorial next." if remaining
        else "Commands completed. Use tutorial next."
    )
    return f"Tutorial {step + 1}/{len(LESSONS)}: {title}\n{explanation}\n{instruction}"


def show(player):
    with database._connection() as c:
        row = progress(c, player)
        reward = c.execute(
            "SELECT amount FROM tutorial_fiat_rewards WHERE player_id=?", (player,)
        ).fetchone()
        if reward:
            return f"Tutorial complete. Your fiat reward was claimed: {reward[0]} microfiat."
        if c.execute(
            "SELECT 1 FROM tutorial_legacy_claims WHERE player_id=?", (player,)
        ).fetchone():
            return "Tutorial complete. Your reward was already claimed on the email tutorial branch."
        return lesson_text(row["step"], json.loads(row["seen"]))


def observe(c, player, action):
    """Only successful authenticated command handlers can mark lesson actions."""
    row = c.execute(
        "SELECT * FROM tutorial_progress WHERE player_id=?", (player,)
    ).fetchone()
    if row is None or row["step"] == len(LESSONS):
        return
    action = {"profile": "status", "resources": "inventory", "hashrate": "power"}.get(
        action, action
    )
    required = LESSONS[row["step"]][2]
    seen = set(json.loads(row["seen"]))
    if action not in required or action in seen:
        return
    seen.add(action)
    c.execute("UPDATE tutorial_progress SET seen=? WHERE player_id=?",
              (json.dumps(sorted(seen)), player))


def next_lesson(player):
    with database._connection() as c:
        c.execute("BEGIN IMMEDIATE")
        row = progress(c, player)
        step = row["step"]
        if step == len(LESSONS):
            return lesson_text(step)
        missing = set(LESSONS[step][2]) - set(json.loads(row["seen"]))
        if missing:
            raise ValueError("Complete these lesson commands first: " + " | ".join(sorted(missing)))
        c.execute("UPDATE tutorial_progress SET step=step+1,seen='[]' WHERE player_id=?",
                  (player,))
        return lesson_text(step + 1)


def claim(player, clock=time.time):
    with World(clock)._transaction(player) as c:
        row = progress(c, player)
        if c.execute(
            "SELECT 1 FROM tutorial_fiat_rewards WHERE player_id=? "
            "UNION ALL SELECT 1 FROM tutorial_legacy_claims WHERE player_id=?", (player, player)
        ).fetchone():
            raise ValueError("Your tutorial reward has already been claimed.")
        if row["step"] != len(LESSONS):
            raise ValueError("Complete all tutorial lessons before claiming your fiat reward.")
        listing = c.execute(
            "SELECT a.id,CASE WHEN a.bid_quantity>0 "
            "THEN max(a.reserve,a.bid/a.bid_quantity+1) ELSE a.reserve END AS minimum_bid "
            "FROM auctions a "
            "JOIN land l ON l.id=CAST(a.asset AS INTEGER) "
            "WHERE a.seller=? AND a.kind='land' AND a.currency='fiat' "
            "AND a.planet='Earth' AND a.status='active' "
            "AND l.player_id=a.seller AND l.planet='Earth' AND l.locked=1 AND l.acres>=1 "
            "ORDER BY minimum_bid,a.id LIMIT 1", (earth_bank_id(c),)
        ).fetchone()
        if listing is None:
            raise ValueError("No active Earth Bank land auction; retry when land is listed.")
        minimum = integer(listing["minimum_bid"], "minimum land bid")
        amount = integer(minimum + (minimum + 9) // 10, "tutorial reward")
        credit(c, player, "fiat", amount)
        now = c.execute("SELECT last_tick FROM chain WHERE id=1").fetchone()[0]
        c.execute(
            "INSERT INTO tutorial_fiat_rewards VALUES (?,?,?,?,?)",
            (player, listing["id"], minimum, amount, now)
        )
        return (
            f"Tutorial complete! You received {amount} microfiat: the current "
            f"one-acre minimum winning bid of {minimum} plus 10% (rounded up). "
            f"To bid for one acre now: land buy {listing['id']} 1 {minimum}\n"
            "This reward is paid once per account. Land is not granted automatically; "
            "later bids may outbid you. Use auctions to check the latest price. "
            "Once you own land, use tutorial droid to learn to craft a worker."
        )
