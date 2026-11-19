"""Configuration for the Zomato Redis/RQ multi-city Streamlit pipeline.

Keep credentials in cookies.txt (not in this file and never commit it).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
COOKIE_FILE = BASE_DIR / "cookies.txt"

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
RESTAURANT_QUEUE = "zomato:restaurant_jobs"
# Kept only so the app can cancel stale jobs created by older versions.
LEGACY_REVIEW_QUEUE = "zomato:review_jobs"

MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/")
MONGO_DB = "zomato_redis"

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.0"))
PAGINATION_DELAY = float(os.getenv("PAGINATION_DELAY", "3.0"))
REVIEW_PAGE_DELAY = float(os.getenv("REVIEW_PAGE_DELAY", "1.0"))
MAX_REVIEW_PAGES = 5  # fetch only review pages 1-5; still record Zomato total count
MAX_REVIEWS_PER_RESTAURANT = None  # MongoDB stores every available review
MAX_CSV_REVIEWS_PER_RESTAURANT = 100  # Download-only limit
WORKER_GROUPS = {"restaurant": 3}

ACTIVE_RUN_KEY = "zomato:streamlit:active_run:v2"
WORKER_PID_KEY = "zomato:streamlit:worker_pids:v2"


def run_key(run_id: str, suffix: str) -> str:
    return f"zomato:run:{run_id}:{suffix}"
