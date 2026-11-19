"""Restaurant workers: extract details and every available review in one job."""
import json
import os
import time

import redis
import zomato_cities_original as original
from config import (
    REDIS_URL, ACTIVE_RUN_KEY, REQUEST_TIMEOUT, REQUEST_DELAY,
    REVIEW_PAGE_DELAY, MAX_REVIEW_PAGES, run_key,
)
from database import RestaurantDatabase

original.REQUEST_TIMEOUT = REQUEST_TIMEOUT
original.REQUEST_DELAY = REQUEST_DELAY
original.REVIEW_PAGE_DELAY = REVIEW_PAGE_DELAY
original.MAX_REVIEW_PAGES = MAX_REVIEW_PAGES


def _set_worker_state(connection, progress_key, restaurant_name, stage, page=None):
    """Publish this worker's live activity for the Streamlit dashboard."""
    state = {
        "pid": os.getpid(),
        "restaurant_name": restaurant_name,
        "stage": stage,
    }
    if page is not None:
        state["page"] = int(page)
    connection.hset(progress_key, f"worker:{os.getpid()}", json.dumps(state))


def _cancelled(connection, run_id):
    if connection.get(run_key(run_id, "cancel")) == "1":
        return True
    active_run = connection.get(ACTIVE_RUN_KEY)
    return bool(active_run and active_run != run_id)


def process_restaurant_job(restaurant, city_name, run_id):
    """One RQ job owns a restaurant from details through review pagination.

    Restaurant pass/fail counts describe detail extraction only. Review outcomes
    are tracked separately and never turn a successfully saved restaurant into
    a failed restaurant.
    """
    name = restaurant.get("restaurant_name") or restaurant.get("restaurant_id") or "Unknown restaurant"
    url = restaurant.get("restaurant_url", "")
    session = database = None
    details_saved = False
    connection = redis.from_url(REDIS_URL, decode_responses=True)
    progress_key = run_key(run_id, "progress")

    try:
        if _cancelled(connection, run_id):
            print(f"[RESTAURANT CANCELLED BEFORE START] {name} | {city_name}", flush=True)
            return {"status": "cancelled", "restaurant_name": name}

        _set_worker_state(connection, progress_key, name, "Starting restaurant")
        print(f"\n[WORKER {os.getpid()}] Starting: {name} | {city_name}", flush=True)
        if not url:
            raise ValueError(f"Missing restaurant URL for {name}")

        _set_worker_state(connection, progress_key, name, "Extracting restaurant details")
        session = original.create_session()
        details = original.restaurant_details(session, restaurant)

        if _cancelled(connection, run_id):
            print(f"[RESTAURANT CANCELLED BEFORE SAVE] {name}", flush=True)
            return {"status": "cancelled", "restaurant_name": name}

        document = {
            "restaurant_name": restaurant.get("restaurant_name", ""),
            "restaurant_url": url,
            "address": "",
            "postal_code": "",
            "city": city_name,
            "mobile_number": "",
            "opening_hours": "",
            "cuisine": restaurant.get("cuisine", ""),
            "rating": restaurant.get("rating", ""),
            "reviews_count": restaurant.get("reviews_count", ""),
            "reviews": "",
        }
        document.update(details or {})
        document["city"] = city_name
        document["restaurant_url"] = url

        database = RestaurantDatabase()
        database.save_restaurant(city_name, document)
        details_saved = True
        connection.hincrby(progress_key, "details_passed", 1)
        print(
            f"[DETAILS SAVED] {name} | address={'yes' if document.get('address') else 'no'}"
            f" | phone={'yes' if document.get('mobile_number') else 'no'}"
            f" | hours={'yes' if document.get('opening_hours') else 'no'}"
            f" | cuisine={'yes' if document.get('cuisine') else 'no'}"
            f" | rating={'yes' if document.get('rating') else 'no'}",
            flush=True,
        )

        # Review extraction is part of this same restaurant job. Existing page
        # checkpoints make retries safe and preserve all previously saved reviews.
        if _cancelled(connection, run_id):
            database.set_review_status(city_name, url, "cancelled")
            connection.hincrby(progress_key, "reviews_cancelled", 1)
            connection.hincrby(progress_key, "restaurant_jobs_done", 1)
            print(f"[REVIEWS CANCELLED] {name}", flush=True)
            return {"status": "details_passed", "review_status": "cancelled",
                    "restaurant_name": name}

        _set_worker_state(connection, progress_key, name, "Checking review checkpoint")
        checkpoint = database.get_review_checkpoint(city_name, url)
        if checkpoint["status"] == "complete":
            saved = database.count_saved_reviews(city_name, url)
            if saved:
                connection.hincrby(progress_key, "reviews_saved_total", saved)
                connection.hincrby(progress_key, "reviews_completed", 1)
                print(f"[REVIEWS ALREADY COMPLETE] {name} | saved={saved}", flush=True)
            elif checkpoint["total_reviews"] > 0:
                connection.hincrby(progress_key, "reviews_partial", 1)
                print(
                    f"[REVIEWS CHECKPOINT INCONSISTENT] {name} | "
                    f"reported_total={checkpoint['total_reviews']} but saved=0",
                    flush=True,
                )
            else:
                connection.hincrby(progress_key, "reviews_no_reviews", 1)
                print(f"[REVIEWS NO REVIEWS] {name}", flush=True)
            connection.hincrby(progress_key, "restaurant_jobs_done", 1)
            return {"status": "details_passed", "review_status": "complete",
                    "restaurant_name": name}

        already_saved = database.count_saved_reviews(city_name, url)
        resume_page = max(2, checkpoint["next_page"])
        _set_worker_state(connection, progress_key, name, "Preparing review extraction", resume_page)
        print(
            f"[REVIEWS START] {name}: saved={already_saved}; resume_page={resume_page}",
            flush=True,
        )
        last_saved_count = [already_saved]

        def save_page(page, items, total_reviews, next_page):
            saved = database.save_review_page(
                city_name, restaurant, page, items, total_reviews, next_page
            )
            delta = max(0, saved - last_saved_count[0])
            if delta:
                connection.hincrby(progress_key, "reviews_saved_total", delta)
            last_saved_count[0] = max(last_saved_count[0], saved)
            _set_worker_state(connection, progress_key, name, "Saved review page", page)
            print(
                f"[REVIEW PAGE SAVED] {name}: page={page}; received={len(items)};"
                f" new_saved={delta}; saved_total={saved}; reported_total={total_reviews}",
                flush=True,
            )
            return saved

        try:
            _texts, reported_total, review_status = original.extract_reviews(
                session,
                restaurant,
                stop_check=lambda: _cancelled(connection, run_id),
                max_reviews=None,
                start_page=resume_page,
                already_saved_count=already_saved,
                on_page=save_page,
                on_progress=lambda stage, page: _set_worker_state(
                    connection, progress_key, name, stage, page
                ),
            )
            saved_total = database.count_saved_reviews(city_name, url)
            database.set_review_status(
                city_name, url, review_status, reported_total
            )

            if review_status == "cancelled":
                connection.hincrby(progress_key, "reviews_cancelled", 1)
                print(f"[REVIEWS CANCELLED] {name} | saved={saved_total}", flush=True)
            elif review_status == "complete":
                if saved_total == 0 and reported_total == 0:
                    connection.hincrby(progress_key, "reviews_no_reviews", 1)
                    print(f"[REVIEWS NO REVIEWS] {name}", flush=True)
                else:
                    connection.hincrby(progress_key, "reviews_completed", 1)
                    print(
                        f"[REVIEWS COMPLETED] {name}: stored={saved_total};"
                        f" reported_total={reported_total}", flush=True
                    )
            elif review_status == "partial_page_limit":
                connection.hincrby(progress_key, "reviews_limited", 1)
                print(
                    f"[REVIEWS LIMITED TO 5 PAGES] {name}: stored={saved_total};"
                    f" Zomato reported total={reported_total}", flush=True
                )
            else:
                connection.hincrby(progress_key, "reviews_partial", 1)
                print(
                    f"[REVIEWS PARTIAL] {name}: status={review_status};"
                    f" stored={saved_total}; reported_total={reported_total}",
                    flush=True,
                )
        except Exception as review_exc:
            try:
                database.set_review_status(city_name, url, "error")
            except Exception:
                pass
            connection.hincrby(progress_key, "reviews_failed", 1)
            print(
                f"[REVIEWS FAILED] {name}: {type(review_exc).__name__}: {review_exc}",
                flush=True,
            )
            # Details are already safely stored. Review failure must not change
            # the restaurant's detail-extraction result.
            review_status = "error"

        connection.hincrby(progress_key, "restaurant_jobs_done", 1)
        return {
            "status": "details_passed",
            "review_status": review_status,
            "restaurant_name": name,
            "stored_reviews": database.count_saved_reviews(city_name, url),
        }

    except Exception as exc:
        if details_saved:
            # The restaurant document is already durable. A later review-stage
            # problem must not downgrade the restaurant's details result.
            connection.hincrby(progress_key, "reviews_failed", 1)
            connection.hincrby(progress_key, "restaurant_jobs_done", 1)
            try:
                if database:
                    database.set_review_status(city_name, url, "error")
            except Exception:
                pass
            print(
                f"[REVIEWS FAILED AFTER DETAILS SAVE] {name}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return {
                "status": "details_passed",
                "review_status": "error",
                "restaurant_name": name,
            }

        connection.hincrby(progress_key, "details_failed", 1)
        connection.hincrby(progress_key, "restaurant_jobs_done", 1)
        print(
            f"[RESTAURANT DETAILS FAILED] {name} | {city_name} | "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        raise
    finally:
        connection.hdel(progress_key, f"worker:{os.getpid()}")
        if session:
            session.close()
        if database:
            database.close()
        connection.close()
        time.sleep(REQUEST_DELAY)
