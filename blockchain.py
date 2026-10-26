"""Deterministic, in-game-only Bitcoin-like chain; no network/payment integration."""

INTERVAL = 600
HALVING = 210_000
SATOSHIS = 100_000_000
MAX_SUPPLY = 21_000_000 * SATOSHIS
MAX_TRANSACTIONS = 1500


def initialize(c):
    c.execute("""
        CREATE TABLE chain (
            id INTEGER PRIMARY KEY CHECK(id=1),
            epoch INTEGER, last_tick INTEGER, height INTEGER NOT NULL DEFAULT 0,
            minted INTEGER NOT NULL DEFAULT 0 CHECK(minted BETWEEN 0 AND 2100000000000000),
            fee_carry INTEGER NOT NULL DEFAULT 0 CHECK(fee_carry >= 0)
        )
    """)
    c.execute("INSERT INTO chain(id) VALUES(1)")
    c.execute("""
        CREATE TABLE hash_work (
            player_id INTEGER PRIMARY KEY REFERENCES players(id),
            work TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE blocks (
            height INTEGER PRIMARY KEY, scheduled_at INTEGER NOT NULL UNIQUE,
            subsidy INTEGER NOT NULL, unissued INTEGER NOT NULL,
            fees INTEGER NOT NULL, paid INTEGER NOT NULL, transactions INTEGER NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE rewards (
            height INTEGER NOT NULL REFERENCES blocks(height),
            player_id INTEGER NOT NULL REFERENCES players(id),
            amount INTEGER NOT NULL CHECK(amount >= 0),
            PRIMARY KEY(height,player_id)
        )
    """)


def subsidy(height):
    """First block is height 1; blocks 1..210000 pay the initial subsidy."""
    if type(height) is not int or height < 1:
        raise ValueError("Height must be a positive integer")
    era = (height - 1) // HALVING
    return 0 if era >= 33 else (50 * SATOSHIS) >> era


def shares(amount, weights):
    """Largest-remainder allocation, with stable player-ID ties; no lost satoshis."""
    weights = {p: w for p, w in weights.items() if w > 0}
    total = sum(weights.values())
    if not total:
        return {}
    payouts = {p: amount * w // total for p, w in weights.items()}
    remainder_order = sorted(weights, key=lambda p: (-(amount * weights[p] % total), p))
    for player in remainder_order[:amount - sum(payouts.values())]:
        payouts[player] += 1
    return payouts


def accumulate(c, seconds):
    from economy import effective_hash
    for player, rate in effective_hash(c).items():
        previous = c.execute("SELECT work FROM hash_work WHERE player_id=?", (player,)).fetchone()
        weight = (int(previous[0]) if previous else 0) + rate * seconds
        c.execute("INSERT INTO hash_work VALUES(?,?) ON CONFLICT(player_id) "
                  "DO UPDATE SET work=excluded.work", (player, str(weight)))


def process_block(c):
    from auctions import settle
    from economy import credit
    state = c.execute("SELECT * FROM chain WHERE id=1").fetchone()
    height = state["height"] + 1
    scheduled = state["epoch"] + height * INTERVAL
    if state["last_tick"] != scheduled:
        raise ValueError("Block processing must occur at its scheduled boundary")
    listings = c.execute(
        "SELECT * FROM auctions WHERE status='pending' "
        "ORDER BY fee DESC,closes,id LIMIT ?", (MAX_TRANSACTIONS,)).fetchall()
    fees = sum(settle(c, row, height) for row in listings)
    weights = {row["player_id"]: int(row["work"]) for row in c.execute("SELECT * FROM hash_work")}
    available_subsidy = min(subsidy(height), MAX_SUPPLY - state["minted"])
    issued = available_subsidy if any(w > 0 for w in weights.values()) else 0
    payouts = shares(issued + fees + state["fee_carry"], weights)
    paid = sum(payouts.values())
    carry = state["fee_carry"] + fees if not payouts else 0
    c.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?)",
              (height, scheduled, issued, available_subsidy - issued, fees, paid, len(listings)))
    for player, amount in payouts.items():
        credit(c, player, "btc", amount)
        c.execute("INSERT INTO rewards VALUES(?,?,?)", (height, player, amount))
    c.execute("UPDATE chain SET height=?,minted=minted+?,fee_carry=? WHERE id=1",
              (height, issued, carry))
    c.execute("DELETE FROM hash_work")


def status(c, player):
    result = dict(c.execute("SELECT * FROM chain WHERE id=1").fetchone())
    result["next_block"] = (result["epoch"] + (result["height"] + 1) * INTERVAL
                            if result["epoch"] is not None else None)
    from economy import effective_hash
    rates = effective_hash(c)
    result["your_hashrate"] = rates.get(player, 0)
    result["total_hashrate"] = sum(rates.values())
    result["pending_transactions"] = c.execute(
        "SELECT count(*) FROM auctions WHERE status='pending'").fetchone()[0]
    result["your_rewards"] = [dict(row) for row in c.execute(
        "SELECT height,amount FROM rewards WHERE player_id=? ORDER BY height DESC LIMIT 20",
        (player,))]
    return result
