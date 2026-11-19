# Zomato Redis/RQ + Streamlit

This version uses **one restaurant queue and three restaurant workers**. Each
worker handles one restaurant end-to-end: detail extraction, MongoDB save, then
review pagination and review persistence. There is no separate review-worker
group and the app does not enqueue review jobs.

## Architecture

- Streamlit UI: `app.py`
- URL producer: `main.py`
- Worker: `worker.py`
- Worker runner/manager: `worker_runner.py`, `worker_supervisor.py`, `worker_manager.py`
- Redis queue: `zomato:restaurant_jobs` (3 workers)
- MongoDB database: `zomato_redis`
- Restaurant details: city collection, e.g. `alappuzha`
- Restaurant URLs: city collection plus `_url`, e.g. `alappuzha_url`
- Review documents: city collection plus `_reviews`, e.g. `alappuzha_reviews`
- Review checkpoints: city collection plus `_review_progress`
- Runtime progress/cancellation: Redis only; no extraction metadata is added to
  restaurant documents.

## Setup

1. Start Redis and MongoDB.
2. Create and activate a virtual environment, then install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Copy `cookies.txt.example` to `cookies.txt` beside `app.py`. Put the current
   complete browser Cookie request-header value in `cookies.txt`, including the
   `csrf` cookie. If the browser request has a separate matching
   `x-zomato-csrft` header, add it on its own line.
4. Keep `cookies.txt` private. It is ignored by Git and is not included in
   project ZIP distributions.
5. **Before starting this version, stop any old Streamlit app and old workers.**
   Run:

   ```bash
   python3 stop_old_workers.py
   ```

   The script detects both legacy direct workers and supervised workers. It
   requests a graceful RQ shutdown and waits for active restaurant jobs to
   finish. It does not clear Redis queues or delete MongoDB records. Do not
   start the app while old worker processes are still alive.
6. Run only:

   ```bash
   streamlit run app.py
   ```

7. Select a city. The app starts exactly three supervised restaurant workers
   and begins URL discovery. Use **Stop this collection** before selecting
   another city.

## Stopping the application

- Press **Ctrl+C** in the terminal running `streamlit run app.py`.
- The worker supervisors share the app's foreground process group, so they
  receive the terminal interrupt too. Each supervisor forwards a graceful
  shutdown signal to its isolated RQ worker.
- RQ finishes the restaurant job already in progress, then exits without
  consuming another queued job. Pending Redis jobs remain queued for a later
  restart; MongoDB records already saved remain intact.
- To stop workers separately, run `python3 stop_old_workers.py`. It waits up to
  three minutes and reports any worker still finishing a long job. Do not use
  `kill -9` for normal shutdown.

## Correct counting rules

- **Restaurants found**: unique restaurant URLs discovered for the selected run.
- **Details passed**: restaurant detail extraction succeeded and the restaurant
  document was saved to MongoDB.
- **Details failed**: restaurant detail extraction or its MongoDB save failed.
- Review extraction outcomes are reported separately. A restaurant with zero
  reviews, partial reviews, or a review API error is **not** counted as a failed
  restaurant if its detail document was successfully saved.
- The progress percentage is based on restaurant jobs completed through the
  review stage, not the number of review pages.

## Reviews and CSV

- All available review documents are saved to MongoDB without a per-restaurant
  review cap. They are stored in the city-specific `_reviews` collection,
  keyed by restaurant URL and review ID.
- `_review_progress` stores page checkpoints so interrupted review extraction
  can resume without duplicating already-saved reviews.
- The downloadable CSV includes at most the first 100 review texts per
  restaurant. This is an export-only limit and does not truncate MongoDB data.
- If the reviews endpoint returns no review items and declares one page with no
  reported review total, the restaurant is recorded as having no available
  reviews rather than as a review failure.

## City isolation

- Each run has a unique run ID stored in Redis.
- The currently active run ID is checked by both producer and workers.
- Starting another city cancels queued jobs belonging to the previous run and
  signals its active workers to stop at the next cooperative checkpoint.
- Existing MongoDB data is not deleted when changing cities.
- A network request already in progress may finish before cancellation is
  observed. Review pages already saved remain safe and resumable.

## Notes

- Do not run `main.py` or `worker_runner.py` manually while using Streamlit.
- Worker logs are written to `logs/`.
- Ratings and postal codes can legitimately be absent from Zomato's response.
  Empty source values are kept empty; the scraper does not invent them.
