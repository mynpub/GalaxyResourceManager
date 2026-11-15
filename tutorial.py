"""Persistent mechanics lessons and one tradable acre per verified email."""

import json
import time

import database
from economy import World, transfer_bank_land

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
        "Resources are stored in micrograms. The tutorial reward is land only, "
        "not a worker; Bank shifts do not fill your personal inventory.",
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
        "for block inclusion. Your reward acre may also be sold or auctioned. "
        "After this lesson, verify an email and claim your acre.",
        ("auctions", "bank wealth"),
    ),
)

WELCOME = (
    "Tutorial started! Use tutorial to see your current lesson, then try its "
    "commands and use tutorial next. Progress survives logout. Finish all "
    "lessons and verify an email to claim one tradable acre, once per email. "
    "Local players can continue with --play."
)


def initialize(c):
    c.execute("""
        CREATE TABLE tutorial_progress (
            player_id INTEGER PRIMARY KEY REFERENCES players(id),
            step INTEGER NOT NULL DEFAULT 0 CHECK(step BETWEEN 0 AND 6),
            seen TEXT NOT NULL DEFAULT '[]'
        )
    """)
    c.execute("""
        CREATE TABLE tutorial_email_requests (
            player_id INTEGER PRIMARY KEY REFERENCES tutorial_progress(player_id),
            address TEXT NOT NULL,
            email_key TEXT NOT NULL,
            token_hash TEXT,
            expires_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
            last_sent_at INTEGER NOT NULL,
            window_started_at INTEGER NOT NULL,
            sends INTEGER NOT NULL CHECK(sends>0)
        )
    """)
    c.execute("""
        CREATE TABLE tutorial_email_limits (
            email_key TEXT PRIMARY KEY,
            window_started_at INTEGER NOT NULL,
            sends INTEGER NOT NULL CHECK(sends>0),
            last_sent_at INTEGER NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE tutorial_verified_emails (
            email_key TEXT PRIMARY KEY,
            player_id INTEGER NOT NULL UNIQUE REFERENCES tutorial_progress(player_id),
            verified_at INTEGER NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE tutorial_rewards (
            player_id INTEGER PRIMARY KEY REFERENCES tutorial_progress(player_id),
            email_key TEXT NOT NULL UNIQUE REFERENCES tutorial_verified_emails(email_key),
            parcel_id INTEGER NOT NULL,
            claimed_at INTEGER NOT NULL
        )
    """)


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
            "All tutorial lessons complete! Use tutorial email ADDRESS to receive "
            "a verification token, tutorial verify TOKEN to verify it, then "
            "tutorial claim for one tradable acre. One reward per account and "
            "verified email in this world."
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
            "SELECT parcel_id FROM tutorial_rewards WHERE player_id=?", (player,)
        ).fetchone()
        if reward:
            return f"Tutorial complete. Your one-acre reward was claimed as parcel #{reward[0]}."
        text = lesson_text(row["step"], json.loads(row["seen"]))
        if c.execute(
            "SELECT 1 FROM tutorial_verified_emails WHERE player_id=?", (player,)
        ).fetchone():
            text += "\nEmail verified. Use tutorial claim after completing the lessons."
        return text


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
            "SELECT 1 FROM tutorial_rewards WHERE player_id=?", (player,)
        ).fetchone():
            raise ValueError("Your tutorial acre has already been claimed.")
        if row["step"] != len(LESSONS):
            raise ValueError("Complete all tutorial lessons before claiming your acre.")
        email = c.execute(
            "SELECT email_key FROM tutorial_verified_emails WHERE player_id=?", (player,)
        ).fetchone()
        if email is None:
            raise ValueError("Verify an email with tutorial email and tutorial verify first.")
        parcel = transfer_bank_land(c, player, 1)
        c.execute(
            "INSERT INTO tutorial_rewards(player_id,email_key,parcel_id,claimed_at) "
            "VALUES (?,?,?,?)", (player, email["email_key"], parcel, int(clock()))
        )
        return (
            f"Tutorial complete! You received one acre: parcel #{parcel}. "
            "Use land to inspect it. It can be sold with land sell PARCEL_ID "
            "or listed with auction list land PARCEL_ID 1 fiat RESERVE."
        )
