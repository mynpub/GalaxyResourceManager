"""Explicit local test grants. Never invoked by signup, the server, or gameplay."""

import argparse
from pathlib import Path

import database
from authentication import normalize_username
from economy import RECIPES, World


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="Explicit test database directory; never use production data")
    parser.add_argument("--username", required=True, help="An already registered test account")
    parser.add_argument("--confirm-test-grants", action="store_true", required=True)
    parser.add_argument("--fiat", type=int, default=0, help="Microfiat to add")
    parser.add_argument("--btc", type=int, default=0, help="Test satoshis to add (not mining subsidy)")
    parser.add_argument("--humans", type=int, default=0)
    parser.add_argument("--recipe-sets", type=int, default=0,
                        help="Add inputs for this many copies of each recipe")
    args = parser.parse_args(argv)
    if any(value < 0 for value in (args.fiat, args.btc, args.humans, args.recipe_sets)):
        parser.error("Grants must be nonnegative")
    if args.humans > 10000 or args.recipe_sets > 10000:
        parser.error("At most 10000 humans/recipe sets per invocation")
    database.DATA_DIR = args.data_dir.expanduser().resolve()
    try:
        username = normalize_username(args.username)
        with database._connection() as c:
            player = c.execute("SELECT id FROM players WHERE username=?", (username,)).fetchone()
        if player is None:
            parser.error("Register the test account in this data directory first")
        resources = {}
        for recipe in RECIPES.values():
            for symbol, quantity in recipe["inputs"].items():
                resources[symbol] = resources.get(symbol, 0) + quantity * args.recipe_sets
        World().seed(player["id"], fiat=args.fiat, btc=args.btc, humans=args.humans,
                     resources=resources)
    except (ValueError, OverflowError) as error:
        parser.error(str(error))
    print("Explicit TEST grants applied. This is not a production bootstrap policy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
