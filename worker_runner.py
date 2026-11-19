"""RQ runner for the single restaurant queue."""
import argparse
import sys
from pathlib import Path

import redis
from rq import Queue, Worker

from config import REDIS_URL, RESTAURANT_QUEUE

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    # Accept the old explicit restaurant flag for a smooth manual transition.
    parser.add_argument("--type", choices=["restaurant"], default="restaurant")
    args = parser.parse_args()

    connection = redis.from_url(REDIS_URL)
    print(f"Starting {args.name} on {RESTAURANT_QUEUE}", flush=True)
    Worker(
        [Queue(RESTAURANT_QUEUE, connection=connection)],
        connection=connection,
        name=args.name,
    ).work()


if __name__ == "__main__":
    main()
