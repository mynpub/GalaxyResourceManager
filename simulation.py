"""Database-serialized authoritative simulation with bounded, resumable catch-up."""

import logging
import sqlite3
import time
from threading import Event, Thread

import database
import blockchain
import auctions
import economy

MAX_EVENTS = 256
LOGGER = logging.getLogger(__name__)


def catch_up(c, now, max_events=MAX_EVENTS):
    """Run under BEGIN IMMEDIATE. Offline time counts; process every boundary in order."""
    if type(now) is not int or now < 0:
        raise ValueError("Clock must provide a nonnegative integer timestamp")
    state = c.execute("SELECT * FROM chain WHERE id=1").fetchone()
    if state["epoch"] is None:
        c.execute("UPDATE chain SET epoch=?,last_tick=? WHERE id=1", (now, now))
        return True
    if now < state["last_tick"]:
        raise ValueError("Clock moved backwards; simulation paused")
    for _ in range(max_events):
        state = c.execute("SELECT * FROM chain WHERE id=1").fetchone()
        current = state["last_tick"]
        # Finish all due closes before including any transaction in this boundary's block.
        if economy.advance(c, current, current) is False:
            continue
        if not auctions.close_due(c, current):
            continue
        next_block = state["epoch"] + (state["height"] + 1) * blockchain.INTERVAL
        if current == next_block:
            blockchain.process_block(c)
            continue
        if current == now:
            return True
        candidates = [now, next_block]
        job = c.execute("SELECT min(completes_at) FROM jobs WHERE status='running'").fetchone()[0]
        closing = c.execute("SELECT min(closes) FROM auctions WHERE status='active'").fetchone()[0]
        for boundary in (job, closing):
            if boundary is not None and boundary > current:
                candidates.append(boundary)
        end = min(candidates)
        blockchain.accumulate(c, end - current)
        economy.advance(c, current, end)
        c.execute("UPDATE chain SET last_tick=? WHERE id=1", (end,))
    return False


def prepare(c, now):
    if not catch_up(c, now):
        # Retain bounded progress without performing the requested player action.
        c.commit()
        raise ValueError("Simulation catching up; retry shortly")


def tick(clock=time.time):
    with database._connection() as c:
        c.execute("BEGIN IMMEDIATE")
        return catch_up(c, int(clock()))


class ServerSimulation:
    """Used by the server loop, never one scheduler per connected client."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.last_attempt = None

    def poll(self):
        now = int(self.clock())
        if self.last_attempt == now:
            return
        self.last_attempt = now
        try:
            tick(self.clock)
        except (ValueError, sqlite3.Error):
            # Deliberately exclude exception details and private game data.
            LOGGER.error("Simulation paused; check database integrity and system clock")


class LocalSimulation:
    """One local scheduler while interactive input is waiting."""

    def __enter__(self):
        self.stop = Event()
        self.scheduler = ServerSimulation()
        self.thread = Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def run(self):
        while not self.stop.is_set():
            self.scheduler.poll()
            self.stop.wait(1)

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
