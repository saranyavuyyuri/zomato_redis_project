"""Streamlit dashboard for the Zomato Redis/RQ extraction pipeline."""
import csv
import html
import io
import json
import uuid
import threading

import redis
import streamlit as st
from rq import Queue
from rq.job import Job
from streamlit_autorefresh import st_autorefresh

from config import (
    REDIS_URL, RESTAURANT_QUEUE, LEGACY_REVIEW_QUEUE, ACTIVE_RUN_KEY,
    MONGO_DB, run_key, MAX_CSV_REVIEWS_PER_RESTAURANT,
)
from main import enqueue_city
from worker_manager import ensure_workers
from database import RestaurantDatabase

CITIES = [
    "Agra", "Ahmedabad", "Ajmer", "Alappuzha", "Allahabad", "Amravati",
    "Amritsar", "Aurangabad", "Bengaluru", "Bhopal", "Bhubaneswar",
    "Chandigarh", "Chennai", "Coimbatore", "Cuttack", "Darjeeling",
    "Dehradun", "Delhi NCR", "Dharamshala", "Gangtok", "Goa", "Gorakhpur",
    "Guntur", "Guwahati", "Gwalior", "Haridwar", "Hyderabad", "Indore",
    "Jabalpur", "Jaipur", "Jalandhar", "Jammu", "Jamnagar", "Jamshedpur",
    "Jhansi", "Jodhpur", "Junagadh", "Kanpur", "Khajuraho", "Khamgaon",
    "Kharagpur", "Kochi", "Kolhapur", "Kolkata", "Kota", "Lucknow",
    "Ludhiana", "Madurai", "Manali", "Mangalore", "Manipal", "Meerut",
    "Mumbai", "Mussoorie", "Mysore", "Nagpur", "Nainital", "Nashik",
    "Neemrana", "Ooty", "Palakkad", "Patiala", "Patna", "Puducherry",
    "Pune", "Pushkar", "Raipur", "Rajkot", "Ranchi", "Rishikesh", "Salem",
    "Shimla", "Siliguri", "Srinagar", "Surat", "Thrissur", "Tirupati",
    "Trichy", "Trivandrum", "Udaipur", "Vadodara", "Varanasi", "Vellore",
    "Vijayawada", "Visakhapatnam",
]

st.set_page_config(
    page_title="Zomato • Restaurant Studio",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(r"""
<style>
:root {
  --ink:#172b4d; --muted:#64748b; --line:#e2e8f0;
  --surface:#ffffff; --canvas:#f5f7fb; --accent:#176b87;
}
.stApp { background:var(--canvas); color:var(--ink); }
html, body, [class*="css"] { font-family:Inter, "Segoe UI", sans-serif; }
.block-container { max-width:1320px; padding-top:1.6rem; padding-bottom:2.5rem; }
#MainMenu, footer {visibility:hidden;}
.hero {
  border-radius:16px; padding:26px 30px; margin-bottom:22px;
  background:linear-gradient(115deg,#12344a 0%,#176b87 100%);
  color:white; box-shadow:0 8px 24px rgba(15,45,65,.10);
}
.eyebrow {font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:700;color:#b8dce8;}
.hero h1 {font-size:clamp(28px,3.5vw,40px);line-height:1.12;margin:8px 0;color:#fff;font-weight:750;}
.hero p {color:#d8e8ef;margin:0;font-size:14px;}
.pill-row {display:flex;flex-wrap:wrap;gap:8px;margin-top:17px;}
.pill {background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.18);
  border-radius:7px;padding:7px 11px;color:#f1f7fa;font-size:11px;font-weight:650;}
.section-title {font-size:22px;color:var(--ink);font-weight:750;margin:4px 0 3px;}
.section-sub {color:var(--muted);font-size:13px;margin-bottom:14px;}
.panel {background:var(--surface);border:1px solid var(--line);border-radius:14px;
  padding:20px 22px;box-shadow:0 3px 12px rgba(15,35,55,.035);margin-bottom:16px;}
.metric-card {height:108px;padding:16px 18px;border-radius:12px;border:1px solid var(--line);
  background:#fff;box-shadow:0 2px 8px rgba(15,35,55,.025);}
.metric-label {font-size:11px;text-transform:uppercase;letter-spacing:1px;font-weight:750;color:var(--muted);}
.metric-value {font-size:31px;line-height:1.2;font-weight:750;color:var(--ink);margin-top:9px;}
.metric-icon {float:right;font-size:19px;}
.metric-pink {background:#f8fbfd;}
.metric-mint {background:#f1f8f5;}
.metric-lav {background:#fff7f5;}
.metric-peach {background:#f4f8fb;}
.status-chip {display:inline-block;border-radius:6px;padding:6px 10px;font-size:11px;
  font-weight:750;background:#eaf2f6;color:#24566d;}
div[data-testid="stSelectbox"] > label, div[data-testid="stTextInput"] > label {font-weight:650;color:#334155;}
div[data-testid="stSelectbox"] [data-baseweb="select"] > div {border-radius:9px!important;border-color:#cbd5e1!important;background:#fff;}
.stButton > button, .stDownloadButton > button {border-radius:9px;border:1px solid #cbd5e1;
  font-weight:650;min-height:42px;transition:background .15s ease;}
.stButton > button[kind="primary"], .stDownloadButton > button {
  background:#176b87;color:white;border:0;box-shadow:none;}
.stButton > button[kind="primary"]:hover, .stDownloadButton > button:hover {background:#12556c;color:white;}
div[data-testid="stProgress"] > div > div {background:#176b87;border-radius:99px;}
div[data-testid="stProgress"] {background:#e2e8f0;border-radius:99px;padding:2px;}
div[data-testid="stDataFrame"] {border:1px solid var(--line);border-radius:10px;overflow:hidden;}
div[data-testid="stAlert"] {border-radius:10px;}
.footer-note {text-align:center;color:#94a3b8;font-size:11px;padding:20px 0 4px;}
.small-note {font-size:12px;color:var(--muted);}
@media(max-width:700px){.hero{padding:22px 20px}.panel{padding:16px}.block-container{padding-left:1rem;padding-right:1rem;}}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="hero">
  <div class="eyebrow">Restaurant data collection</div>
  <h1>Zomato Restaurant Data Pipeline</h1>
  <p>Discover restaurant URLs, extract restaurant details, and collect available reviews.</p>
  <div class="pill-row">
    <span class="pill">MongoDB · zomato_redis</span>
    <span class="pill">3 restaurant workers</span>
    <span class="pill">Details and reviews in one job</span>
  </div>
</div>
""", unsafe_allow_html=True)

for key, default in (("run_id", None), ("submitted_city", None)):
    if key not in st.session_state:
        st.session_state[key] = default


def _run_producer(city, run_id):
    try:
        enqueue_city(city, run_id)
    except Exception as exc:
        connection = redis.from_url(REDIS_URL, decode_responses=True)
        connection.hset(run_key(run_id, "progress"), mapping={
            "status": "producer_failed",
            "producer_error": f"{type(exc).__name__}: {exc}",
        })
        connection.close()


def _cancel_queued_jobs(connection, run_id):
    """Cancel only queued jobs belonging to the specified run."""
    for queue_name in (RESTAURANT_QUEUE, LEGACY_REVIEW_QUEUE):
        queue = Queue(queue_name, connection=connection)
        for job_id in queue.get_job_ids():
            try:
                job = Job.fetch(job_id, connection=connection)
                if (job.meta or {}).get("run_id") == run_id:
                    job.cancel()
            except Exception:
                pass


def start_selected_city(city):
    # Refuse to launch alongside old worker code. Stop it first with the
    # included stop_old_workers.py utility so stale workers cannot consume jobs.
    ensure_workers()

    run_id = uuid.uuid4().hex[:12]
    connection = redis.from_url(REDIS_URL, decode_responses=True)

    previous_run = connection.get(ACTIVE_RUN_KEY)
    if previous_run and previous_run != run_id:
        connection.set(run_key(previous_run, "cancel"), "1")
        connection.hset(run_key(previous_run, "progress"), "status", "cancelling")
        _cancel_queued_jobs(connection, previous_run)
        connection.hset(run_key(previous_run, "progress"), "status", "cancelled")

    connection.delete(run_key(run_id, "cancel"))
    connection.set(ACTIVE_RUN_KEY, run_id)
    connection.hset(run_key(run_id, "progress"), mapping={
        "city_requested": city,
        "status": "starting",
        "discovered": 0,
        "restaurant_queued": 0,
        "details_passed": 0,
        "details_failed": 0,
        "restaurant_jobs_done": 0,
        "reviews_saved_total": 0,
        "reviews_completed": 0,
        "reviews_no_reviews": 0,
        "reviews_limited": 0,
        "reviews_partial": 0,
        "reviews_failed": 0,
        "reviews_cancelled": 0,
    })
    connection.close()

    st.session_state.run_id = run_id
    st.session_state.submitted_city = city
    threading.Thread(target=_run_producer, args=(city, run_id), daemon=True).start()


def cancel_run(run_id):
    """Cancel this run's waiting jobs and signal active workers cooperatively."""
    connection = redis.from_url(REDIS_URL, decode_responses=True)
    connection.set(run_key(run_id, "cancel"), "1")
    connection.hset(run_key(run_id, "progress"), "status", "cancelling")
    _cancel_queued_jobs(connection, run_id)
    if connection.get(ACTIVE_RUN_KEY) == run_id:
        connection.delete(ACTIVE_RUN_KEY)
    connection.hset(run_key(run_id, "progress"), "status", "cancelled")
    connection.close()


def load_progress(run_id):
    connection = redis.from_url(REDIS_URL, decode_responses=True)
    progress = connection.hgetall(run_key(run_id, "progress"))
    connection.close()
    return progress


def csv_bytes(records):
    output = io.StringIO()
    fields = [
        "restaurant_name", "restaurant_url", "address", "postal_code", "city",
        "mobile_number", "opening_hours", "cuisine", "rating", "reviews_count", "reviews",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        row = dict(record)
        reviews = row.get("reviews", [])
        if isinstance(reviews, list):
            row["reviews"] = " | ".join(
                str(review) for review in reviews[:MAX_CSV_REVIEWS_PER_RESTAURANT]
                if review not in (None, "")
            )
        elif isinstance(reviews, str):
            row["reviews"] = reviews
        writer.writerow(row)
    return output.getvalue().encode("utf-8-sig")



def dataframe_safe_records(records):
    """Return display-only rows with consistent scalar values for Streamlit/Arrow."""
    safe_rows = []
    for record in records:
        safe = {}
        for key, value in dict(record).items():
            if value is None:
                safe[key] = ""
            elif isinstance(value, list):
                # Reviews can be stored as a list while legacy records contain a string.
                safe[key] = " | ".join(
                    str(item) for item in value if item not in (None, "")
                )
            elif isinstance(value, dict):
                safe[key] = ", ".join(
                    f"{k}: {v}" for k, v in value.items()
                    if v not in (None, "")
                )
            elif isinstance(value, (str, int, float, bool)):
                safe[key] = value
            else:
                safe[key] = str(value)
        safe_rows.append(safe)
    return safe_rows


def metric_card(label, value, emoji, style):
    return f"""
    <div class="metric-card {style}">
      <span class="metric-icon">{emoji}</span>
      <div class="metric-label">{label}</div>
      <div class="metric-value">{value:,}</div>
    </div>"""


if not st.session_state.run_id:
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Select a city</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">Choose a city to discover restaurants and extract their details.</div>', unsafe_allow_html=True)
    selected = st.selectbox("Choose your city", ["Select a city"] + CITIES, index=0)
    start_col, tip_col = st.columns([1, 2])
    with start_col:
        launch = st.button("Start collection", type="primary", width="stretch",
                           disabled=(selected == "Select a city"))
    with tip_col:
        st.markdown('<div class="small-note" style="padding:12px 4px">Your workers start automatically. No need to open separate worker terminals.</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)
    if launch and selected != "Select a city":
        try:
            start_selected_city(selected)
            st.rerun()
        except Exception as exc:
            st.error(f"Could not start extraction: {type(exc).__name__}: {exc}")
else:
    run_id = st.session_state.run_id
    city_name = st.session_state.submitted_city
    st_autorefresh(interval=2500, key=f"refresh_{run_id}")
    progress = load_progress(run_id)
    status = progress.get("status", "starting")

    status_labels = {
        "starting": "Starting",
        "discovering": "Discovering restaurant URLs",
        "processing": "Collecting restaurant details and reviews",
        "completed": "Collection complete",
        "cancelled": "Collection stopped",
        "cancelling": "Stopping safely",
        "producer_failed": "Producer error",
        "no_restaurants_found": "No restaurants found",
    }
    status_label = status_labels.get(status, status.replace("_", " ").title())
    safe_city = html.escape(str(progress.get("city") or city_name))

    st.markdown(f"""
    <div class="panel">
      <div class="eyebrow">CURRENT CITY</div>
      <div class="section-title">{safe_city}</div>
      <span class="status-chip">{status_label}</span>
    </div>
    """, unsafe_allow_html=True)

    if status == "discovering":
        st.info("Discovering restaurant URLs. They are being saved to MongoDB as they are found.")
    elif status == "producer_failed":
        st.error(progress.get("producer_error", "The city extraction could not start."))
    elif status == "cancelled":
        st.warning("This city's extraction has been stopped. Your saved data is safe.")

    discovered = int(progress.get("discovered", 0) or 0)
    details_passed = int(progress.get("details_passed", 0) or 0)
    details_failed = int(progress.get("details_failed", 0) or 0)
    # Restaurant passed/failed counts describe detail extraction only.
    # Review outcomes are separate and never double-count a restaurant.
    passed = details_passed
    failed = details_failed
    details_done = details_passed + details_failed
    restaurant_jobs_done = int(progress.get("restaurant_jobs_done", details_done) or 0)
    reviews_saved_total = int(progress.get("reviews_saved_total", 0) or 0)
    reviews_completed = int(progress.get("reviews_completed", 0) or 0)
    reviews_no_reviews = int(progress.get("reviews_no_reviews", 0) or 0)
    reviews_limited = int(progress.get("reviews_limited", 0) or 0)
    reviews_partial = int(progress.get("reviews_partial", 0) or 0)
    reviews_failed = int(progress.get("reviews_failed", 0) or 0)
    percent = min(100, int((restaurant_jobs_done / discovered) * 100)) if discovered else 0

    if discovered > 0 and restaurant_jobs_done >= discovered and status == "processing":
        connection = redis.from_url(REDIS_URL, decode_responses=True)
        connection.hset(run_key(run_id, "progress"), "status", "completed")
        if connection.get(ACTIVE_RUN_KEY) == run_id:
            connection.delete(ACTIVE_RUN_KEY)
        connection.close()
        status = "completed"
        percent = 100
    if status in {"completed", "no_restaurants_found"}:
        percent = 100 if status == "completed" else 0

    # Each worker publishes its current restaurant and stage in this run's
    # Redis progress hash. Show all active workers instead of noisy connection logs.
    active_workers = []
    for field, raw_state in progress.items():
        if not field.startswith("worker:"):
            continue
        try:
            worker_state = json.loads(raw_state)
            active_workers.append(worker_state)
        except (TypeError, ValueError):
            continue

    st.markdown('<div class="panel"><div class="section-title">Currently processing</div><div class="section-sub">Live activity reported by each worker.</div>', unsafe_allow_html=True)
    if active_workers:
        for worker_state in sorted(active_workers, key=lambda item: int(item.get("pid", 0))):
            worker_name = html.escape(str(worker_state.get("restaurant_name") or "Unknown restaurant"))
            worker_stage = html.escape(str(worker_state.get("stage") or "Working"))
            worker_pid = html.escape(str(worker_state.get("pid") or "-"))
            page_number = worker_state.get("page")
            page_suffix = f" · page {int(page_number)}" if page_number is not None else ""
            st.markdown(
                f'<div style="padding:10px 12px;margin:8px 0;border:1px solid #e2e8f0;border-radius:9px;background:#f8fafc">'
                f'<div style="font-weight:750;color:#172b4d">{worker_name}</div>'
                f'<div class="small-note">Worker {worker_pid} · {worker_stage}{page_suffix}</div></div>',
                unsafe_allow_html=True,
            )
    elif status in {"completed", "cancelled", "producer_failed", "no_restaurants_found"}:
        st.info("No restaurant is currently being processed.")
    else:
        st.info("Workers are waiting for restaurant jobs or discovering URLs.")
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown(f"""
    <div class="panel">
      <div class="section-title">Collection progress</div>
      <div class="section-sub">Restaurant details are saved as each worker completes a restaurant.</div>
      <div style="font-size:34px;font-weight:800;color:#493342;margin:4px 0 8px">{percent}% <span style="font-size:13px;font-weight:600;color:#958795">complete</span></div>
    </div>
    """, unsafe_allow_html=True)
    st.progress(percent / 100)

    metric_cols = st.columns(4)
    cards = [
        ("Restaurants found", discovered, "🍽️", "metric-peach"),
        ("Passed", passed, "", "metric-mint"),
        ("Failed", failed, "", "metric-lav"),
        ("Review texts saved", reviews_saved_total, "", "metric-pink"),
    ]
    for col, (label, value, emoji, style) in zip(metric_cols, cards):
        with col:
            st.markdown(metric_card(label, value, emoji, style), unsafe_allow_html=True)

    st.markdown('<div style="height:14px"></div>', unsafe_allow_html=True)
    left, right = st.columns(2)
    with left:
        st.markdown(f"""
        <div class="panel">
          <div class="metric-label">Restaurant details</div>
          <div style="font-size:23px;font-weight:800;margin:7px 0;color:#493342">{details_passed:,} <span style="font-size:13px;color:#958795">passed</span></div>
          <div class="small-note">{details_failed:,} need another look</div>
        </div>""", unsafe_allow_html=True)
    with right:
        st.markdown(f"""
        <div class="panel">
          <div class="metric-label">Review collection</div>
          <div style="font-size:23px;font-weight:800;margin:7px 0;color:#172b4d">{reviews_saved_total:,} <span style="font-size:13px;color:#64748b">review texts saved</span></div>
          <div class="small-note">{reviews_completed:,} completed · {reviews_no_reviews:,} with no reviews ·  {reviews_partial:,} partial · {reviews_failed:,} errors</div>
        </div>""", unsafe_allow_html=True)

    try:
        database = RestaurantDatabase()
        records = database.export_records(
            progress.get("city") or city_name,
            review_limit=MAX_CSV_REVIEWS_PER_RESTAURANT,
        )
        database.close()
        st.markdown('<div class="panel"><div class="section-title">Restaurant records</div><div class="section-sub">Records are loaded directly from MongoDB.</div>', unsafe_allow_html=True)
        if records:
            st.dataframe(dataframe_safe_records(records), width="stretch", hide_index=True)
            safe_file_city = (progress.get("city") or city_name).lower().replace(" ", "_")
            st.download_button(
                "Download CSV", data=csv_bytes(records),
                file_name=f"{safe_file_city}_restaurants.csv", mime="text/csv",
                width="stretch",
            )
        else:
            st.info("Your restaurant records will appear here as soon as the first details are saved.")
        st.markdown('</div>', unsafe_allow_html=True)
    except Exception as exc:
        st.warning(f"MongoDB data could not be loaded: {exc}")

    if status not in {"completed", "cancelled", "producer_failed", "no_restaurants_found"}:
        if st.button("Stop this collection", type="secondary", width="stretch"):
            cancel_run(run_id)
            st.rerun()
    if status in {"completed", "cancelled", "producer_failed", "no_restaurants_found"}:
        if st.button("Select another city", type="primary", width="stretch"):
            st.session_state.run_id = None
            st.session_state.submitted_city = None
            st.rerun()

st.markdown("""
<div class="footer-note">
  Redis + RQ + MongoDB
  <br>Workers are managed automatically by the app.
</div>
""", unsafe_allow_html=True)
