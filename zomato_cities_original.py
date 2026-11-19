#!/usr/bin/env python3
"""
Dynamic Zomato Dineout restaurant extractor.

- Prompts for ANY city name; no city IDs are hardcoded.
- Uses Requests for the city page and Dineout API; no browser automation.
- Reuses server-returned search metadata for every subsequent page.
- Saves each restaurant record immediately to its city collection.
- Stores exactly these fields:
  restaurant_name, restaurant_url, address, postal_code, city,
  mobile_number, opening_hours, cuisine, rating, reviews_count, reviews

Use only where you have permission to access the website. Zomato's endpoints are
undocumented and may change. HTTP 401/403 is treated as a session/access failure.
"""
import html
import json
import os
import re
import sys
import time
from getpass import getpass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# ---------------------------------------------------------------------------
# Keep cookies.txt beside dynamic.py. Paste the complete Cookie request-header
# value into it. Optionally add: x-zomato-csrft: YOUR_MATCHING_TOKEN
# Environment variables ZOMATO_COOKIE_HEADER / ZOMATO_CSRF_TOKEN override it.
# Never commit cookies.txt to GitHub or share it.
# ---------------------------------------------------------------------------

COOKIE_FILE = Path(__file__).resolve().with_name("cookies.txt")
COOKIE_HEADER = os.getenv("ZOMATO_COOKIE_HEADER", "")
CSRF_TOKEN = os.getenv("ZOMATO_CSRF_TOKEN", "")

BASE_URL = "https://www.zomato.com"
LOCATION_SEARCH_URL = BASE_URL + "/webroutes/location/search"
LOCATION_GET_URL = BASE_URL + "/webroutes/location/get"
SEARCH_URL = BASE_URL + "/webroutes/search/home"
GET_PAGE_URL = BASE_URL + "/webroutes/getPage"
REVIEWS_URL = BASE_URL + "/webroutes/reviews/loadMore"

MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/")
MONGO_DB = os.getenv("MONGO_DB", "zomato_dynamic_city_db")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.0"))
PAGINATION_DELAY = float(os.getenv("PAGINATION_DELAY", "3.0"))  # wait between search pages
REVIEW_PAGE_DELAY = float(os.getenv("REVIEW_PAGE_DELAY", "5.0"))  # wait between review pages
MAX_RESTAURANTS = int(os.getenv("MAX_RESTAURANTS", "0"))  # 0 = all discovered
MAX_REVIEW_PAGES = 5  # fetch only review pages 1-5; still record Zomato total count


def log(message):
    print(message, flush=True)


def parse_json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


def clean(value):
    """Convert HTML/text/list values to a clean, comma-separated cell value."""
    if value is None:
        return ""
    if isinstance(value, dict):
        # Common API wrappers for displayable values.
        for key in ("text", "name", "title", "value", "phoneStr", "timing"):
            if value.get(key) not in (None, ""):
                return clean(value[key])
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            part = clean(item)
            if part and part not in parts:
                parts.append(part)
        return ", ".join(parts)
    text = html.unescape(str(value))
    text = re.sub(r"<br\s*/?>", ", ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def slugify(value):
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def city_url_slug(city_name):
    """Map resolved display names to Zomato's canonical city URL slugs."""
    normalized = slugify(city_name)
    aliases = {
        "bengaluru": "bangalore",
    }
    return aliases.get(normalized, normalized)


def parse_cookie_header(raw):
    cookies = {}
    for item in raw.split(";"):
        item = item.strip()
        if "=" in item:
            key, value = item.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


def load_cookie_file():
    """Read a Cookie header and optional x-zomato-csrft line from cookies.txt."""
    if not COOKIE_FILE.is_file():
        return "", ""

    raw_cookie = ""
    csrf_header = ""
    for raw_line in COOKIE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("cookie:"):
            raw_cookie = line.split(":", 1)[1].strip()
        elif line.lower().startswith("x-zomato-csrft:"):
            csrf_header = line.split(":", 1)[1].strip()
        elif "=" in line and not raw_cookie:
            raw_cookie = line
    return raw_cookie, csrf_header


def create_session():
    raw = COOKIE_HEADER.strip()
    file_csrf = ""
    if not raw:
        raw, file_csrf = load_cookie_file()

    if not raw:
        raise RuntimeError(
            f"No Cookie header found. Create {COOKIE_FILE} beside dynamic.py "
            "and paste your complete browser Cookie request-header value into it."
        )

    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()

    cookie_values = parse_cookie_header(raw)
    csrf = CSRF_TOKEN.strip() or file_csrf.strip() or cookie_values.get("csrf", "")
    if not csrf:
        raise RuntimeError(
            "No CSRF token found. Include the csrf cookie in cookies.txt or add "
            "a separate line: x-zomato-csrft: YOUR_MATCHING_TOKEN"
        )

    session = requests.Session()
    session.cookies.update(cookie_values)
    session.headers.update({
        "accept": "*/*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": BASE_URL,
        "user-agent": os.getenv(
            "ZOMATO_USER_AGENT",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36",
        ),
        "x-zomato-csrft": csrf,
    })
    source = "ZOMATO_COOKIE_HEADER" if COOKIE_HEADER.strip() else str(COOKIE_FILE)
    log(f"Loaded {len(cookie_values)} cookies from {source}")
    return session


def refresh_session_cookies(session):
    """Reload cookies.txt into the existing Requests session after a 401/403."""
    if not sys.stdin.isatty():
        raise PermissionError(
            "Zomato rejected the session and interactive cookie refresh is unavailable. "
            "Update cookies.txt and rerun the script."
        )

    log("\\nZomato rejected the current session (HTTP 401/403).")
    log("1. Open Zomato in Chrome and refresh/login if needed.")
    log("2. Replace cookies.txt with the fresh Cookie header and matching CSRF token.")
    input("3. Save cookies.txt, then press ENTER here to retry this same request... ")

    raw_cookie, file_csrf = load_cookie_file()
    if not raw_cookie:
        raise PermissionError("cookies.txt is empty; the request cannot be retried.")

    if raw_cookie.lower().startswith("cookie:"):
        raw_cookie = raw_cookie.split(":", 1)[1].strip()

    cookie_values = parse_cookie_header(raw_cookie)
    csrf = file_csrf.strip() or cookie_values.get("csrf", "")
    if not csrf:
        raise PermissionError(
            "No matching CSRF token found in cookies.txt. Add the csrf cookie "
            "or a separate x-zomato-csrft: line."
        )

    # Update the existing session so city/location context and pipeline state remain.
    session.cookies.update(cookie_values)
    session.headers["x-zomato-csrft"] = csrf
    log(f"Reloaded {len(cookie_values)} cookies. Retrying the interrupted request once.")
    return True


def request_json(session, method, url, **kwargs):
    # If the session expires mid-run, let the user refresh cookies.txt and retry
    # this exact request once. Already completed MongoDB writes remain intact.
    response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
    if response.status_code in (401, 403):
        first_status = response.status_code
        refresh_session_cookies(session)
        response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        if response.status_code in (401, 403):
            raise PermissionError(
                f"HTTP {response.status_code} after refreshing cookies.txt "
                f"(initial response was HTTP {first_status}). Zomato is still "
                "rejecting this endpoint/session. Check the browser request and "
                "matching CSRF token."
            )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        filename = "zomato_non_json_response.html"
        with open(filename, "w", encoding="utf-8") as handle:
            handle.write(response.text)
        raise RuntimeError(
            f"Expected JSON but received non-JSON content. Saved response to {filename}."
        )


def recursive_candidates(obj):
    """Yield only objects from recognized location-suggestion arrays."""
    suggestion_keys = {
        "location_suggestions", "locationSuggestions",
        "suggestions", "results", "locations",
    }

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in suggestion_keys and isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item
            # Search nested response wrappers, but do not yield arbitrary
            # nested dictionaries (icons/labels often contain unrelated `id`).
            if isinstance(value, (dict, list)):
                yield from recursive_candidates(value)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield from recursive_candidates(item)
            elif isinstance(item, list):
                yield from recursive_candidates(item)


def first_value(obj, *keys, default=None):
    if not isinstance(obj, dict):
        return default
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return value
    return default


def resolve_city(session, city_query):
    log(f"\n[1/5] Searching for city: {city_query}")
    data = request_json(
        session, "GET", LOCATION_SEARCH_URL,
        params={"q": city_query, "lat": "0", "lon": "0"},
        headers={"referer": BASE_URL + "/"},
    )

    # IMPORTANT: only entity_id/entityId identify a selectable Zomato location.
    # Do not use a generic `id`: nested icons and UI components may have one.
    candidates = list(recursive_candidates(data))
    unique = []
    seen = set()
    skipped_without_entity_id = 0

    for item in candidates:
        entity_id = first_value(item, "entity_id", "entityId")
        if entity_id in (None, "", 0, "0"):
            skipped_without_entity_id += 1
            continue

        name = first_value(
            item, "entity_name", "entityName", "display_title",
            "displayTitle", "title", "name", default=""
        )
        entity_type = first_value(
            item, "entity_type", "entityType", "location_type", "locationType", default=""
        )
        key = (str(entity_id), str(name).strip().casefold(), str(entity_type).strip().casefold())
        if key not in seen:
            seen.add(key)
            unique.append(item)

    if skipped_without_entity_id:
        log(f"Ignored {skipped_without_entity_id} suggestions without a valid entity_id.")

    if not unique:
        with open("location_search_response.json", "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        raise RuntimeError(
            "Location search returned no selectable results with entity_id/entityId. "
            "Saved location_search_response.json for inspection."
        )

    def suggestion_name(item):
        return str(first_value(
            item, "entity_name", "entityName", "display_title", "displayTitle",
            "title", "name", default="Unknown location"
        )).strip()

    def suggestion_type(item):
        return str(first_value(
            item, "entity_type", "entityType", "location_type", "locationType", default=""
        )).strip().casefold()

    def suggestion_id(item):
        # Deliberately excludes generic `id`.
        return first_value(item, "entity_id", "entityId")

    def normalized_name(value):
        value = value.strip().casefold()
        value = re.sub(r"[,\s]+india$", "", value).strip()
        return re.sub(r"\s+", " ", value)

    requested = normalized_name(city_query)

    # Exact requested city is the first choice. If unavailable, present only
    # genuine city-level results before falling back to other valid locations.
    exact_city = [
        item for item in unique
        if suggestion_type(item) == "city"
        and normalized_name(suggestion_name(item)) == requested
    ]
    city_options = [item for item in unique if suggestion_type(item) == "city"]
    exact_named = [
        item for item in unique
        if normalized_name(suggestion_name(item)) == requested
    ]
    options = exact_city or city_options or exact_named or unique

    log("\nMatching locations:")
    for index, item in enumerate(options, 1):
        log(
            f"  {index}. {suggestion_name(item)} | "
            f"type={suggestion_type(item) or 'unknown'} | "
            f"entity_id={suggestion_id(item)}"
        )

    if exact_city:
        selected = exact_city[0]
        log("Automatically selected the exact city-level match.")
    elif len(options) == 1:
        selected = options[0]
        log("Automatically selected the only valid location.")
    else:
        while True:
            try:
                choice = int(input(f"Choose the correct location (1-{len(options)}): ").strip())
                if 1 <= choice <= len(options):
                    selected = options[choice - 1]
                    break
            except ValueError:
                pass
            log("Please enter one of the displayed numbers.")

    entity_id = suggestion_id(selected)
    entity_type = first_value(
        selected, "entity_type", "entityType", "location_type", "locationType", default="city"
    )
    lat = first_value(selected, "entity_latitude", "latitude", "lat", default="0")
    lon = first_value(selected, "entity_longitude", "longitude", "lon", "lng", default="0")
    title = suggestion_name(selected)

    if entity_id in (None, "", 0, "0"):
        raise RuntimeError(
            "Selected suggestion has no valid entity_id. This should not happen; "
            "the candidate filter rejected such results."
        )

    get_params = {
        "lat": lat,
        "lon": lon,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "userDefinedLatitude": lat,
        "userDefinedLongitude": lon,
        "placeId": first_value(selected, "place_id", "placeId", default="0"),
        "placeType": first_value(selected, "place_type", "placeType", default=""),
        "placeName": first_value(selected, "place_name", "placeName", default=""),
        "cellId": first_value(selected, "cell_id", "cellId", default="0"),
        "addressId": first_value(selected, "address_id", "addressId", default="0"),
        "isOrderLocation": first_value(selected, "is_order_location", "isOrderLocation", default=1),
        "forceEntityName": title,
        "res_id": 0,
        "pageType": "search",
        "persist": "true",
    }
    selected_response = request_json(
        session, "GET", LOCATION_GET_URL,
        params=get_params,
        headers={"referer": BASE_URL + "/" + city_url_slug(city_query)},
    )
    details = selected_response.get("locationDetails", {}) if isinstance(selected_response, dict) else {}
    if not isinstance(details, dict) or not details.get("entityId"):
        with open("location_get_response.json", "w", encoding="utf-8") as handle:
            json.dump(selected_response, handle, ensure_ascii=False, indent=2)
        raise RuntimeError("City selection did not return locationDetails.entityId.")

    city_name = first_value(details, "cityName", default=city_query)
    city = {
        "city_name": city_name,
        "city_id": first_value(details, "cityId", default=details.get("entityId")),
        "country_id": first_value(details, "countryId", default=1),
        "country_name": first_value(details, "countryName", default="India"),
        "entity_id": details.get("entityId"),
        "entity_type": first_value(details, "entityType", default="city"),
        "latitude": first_value(details, "latitude", default=lat),
        "longitude": first_value(details, "longitude", default=lon),
        "user_defined_latitude": first_value(details, "userDefinedLatitude", default=lat),
        "user_defined_longitude": first_value(details, "userDefinedLongitude", default=lon),
        "place_id": str(first_value(details, "placeId", default="0")),
        "cell_id": str(first_value(details, "cellId", default="0")),
        "delivery_subzone_id": first_value(details, "deliverySubzoneId", default=0),
        "place_type": first_value(details, "placeType", default=""),
        "place_name": first_value(details, "placeName", default=city_name),
        "display_title": first_value(details, "displayTitle", default=city_name),
        "fetch_from_google": bool(details.get("fetchFromGoogle", False)),
        "fetched_from_cookie": bool(details.get("fetchedFromCookie", False)),
        "o2_serviceable": bool(details.get("o2Serviceable", True)),
        "is_o2_city": bool(details.get("isO2City", True)),
    }

    session.headers["referer"] = (
        f"{BASE_URL}/{city_url_slug(city_name)}/restaurants?category=2"
    )
    cookie_updates = {
        "fbcity": city.get("city_id"),
        "ltv": first_value(details, "ltv", default=city.get("city_id")),
        "lty": first_value(details, "lty", default=city.get("entity_type", "city")),
    }
    for key, value in cookie_updates.items():
        if value not in (None, ""):
            session.cookies.set(key, str(value), domain="www.zomato.com")

    log(
        f"Selected city: {city_name} | cityId={city['city_id']} | "
        f"entityId={city['entity_id']} | entityType={city['entity_type']}"
    )
    return city


def page_sections(data):
    page = data.get("page_data") if isinstance(data.get("page_data"), dict) else data
    return page.get("sections", {}) if isinstance(page, dict) else {}



def initial_search_context(session, city):
    """
    Read Zomato's genuine search ID from the city page, then use the search API.
    """
    page_url = f"{BASE_URL}/{city_url_slug(city['city_name'])}/restaurants?context=dineout"
    log("\n[2/5] Loading the selected city's page with Requests...")
    response = session.get(
        page_url,
        headers={"referer": BASE_URL + "/"},
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code in (401, 403):
        raise PermissionError(
            f"HTTP {response.status_code} while opening the city page. "
            "Refresh your browser cookies and matching CSRF token."
        )
    response.raise_for_status()
    session.headers["referer"] = page_url

    search_id, total, postback, search_metadata, location = (
        extract_search_context_from_html(response.text)
    )
    if not search_id:
        with open("city_restaurants_page.html", "w", encoding="utf-8") as handle:
            handle.write(response.text)
        raise RuntimeError(
            "The city page did not expose a genuine search ID. "
            "Saved city_restaurants_page.html. Refresh the session or check "
            "whether Zomato changed its page metadata; no ID was invented."
        )
    if not location:
        raise RuntimeError(
            "The city page did not expose its resolved location context. "
            "Saved city_restaurants_page.html; refusing to send a mismatched "
            "location in the search API request."
        )

    location_fields = {
        "city_name": "cityName",
        "city_id": "cityId",
        "country_id": "countryId",
        "country_name": "countryName",
        "entity_id": "entityId",
        "entity_type": "entityType",
        "latitude": "latitude",
        "longitude": "longitude",
        "user_defined_latitude": "userDefinedLatitude",
        "user_defined_longitude": "userDefinedLongitude",
        "place_id": "placeId",
        "cell_id": "cellId",
        "delivery_subzone_id": "deliverySubzoneId",
        "place_type": "placeType",
        "place_name": "placeName",
        "display_title": "displayTitle",
        "o2_serviceable": "o2Serviceable",
        "is_o2_city": "isO2City",
        "fetch_from_google": "fetchFromGoogle",
        "fetched_from_cookie": "fetchedFromCookie",
        "address_id": "addressId",
        "location_type": "locationType",
        "is_order_location": "isOrderLocation",
        "entity_name": "entityName",
        "order_location_name": "orderLocationName",
        "is_o2_only_city": "isO2OnlyCity",
    }
    for city_key, location_key in location_fields.items():
        if location_key in location:
            city[city_key] = location[location_key]

    city_id = city.get("city_id")
    ltv = first_value(location, "ltv", default=city_id)
    lty = first_value(location, "lty", default=city.get("entity_type", "city"))
    locus = {
        "addressId": location.get("addressId", 0),
        "lat": location.get("latitude", 0),
        "lng": location.get("longitude", 0),
        "cityId": city_id,
        "ltv": ltv,
        "lty": lty,
        "fetchFromGoogle": location.get("fetchFromGoogle", False),
        "dszId": location.get("deliverySubzoneId", 0),
        "fen": location.get("entityName") or location.get("displayTitle", ""),
    }
    session.cookies.set("fbcity", str(city_id), domain="www.zomato.com")
    session.cookies.set("ltv", str(ltv), domain="www.zomato.com")
    session.cookies.set("lty", str(lty), domain="www.zomato.com")
    session.cookies.set(
        "locus",
        quote_plus(json.dumps(locus, separators=(",", ":"))),
        domain="www.zomato.com",
    )

    offset = int(postback.get("solr_offset", 0))
    page = int(postback.get("page", 1))
    shown = int(postback.get("total_restaurants_shown", offset))
    log(f"[2/5] Genuine search ID found: {search_id}")
    return {
        "page": page,
        "offset": offset,
        "shown": shown,
        "total": total,
        "search_id": search_id,
        "search_metadata": search_metadata,
        "location": location,
    }


def extract_search_context_from_html(body):
    match = re.search(
        r'window\.__PRELOADED_STATE__\s*=\s*JSON\.parse\(("(?:\\.|[^"\\])*")\)',
        body,
    )
    if not match:
        return None, 0, {}, {}, {}

    try:
        state = json.loads(json.loads(match.group(1)))
        pages = state.get("pages", {})
        current_page = pages.get("current", {})
        page_url = current_page.get("pageUrl")
        page = pages.get("search", {}).get(page_url, {})
        sections = page.get("sections", {})
        section = sections.get("SECTION_SEARCH_META_INFO", {})
        metadata = section.get("searchMetaData", {})
        location = state.get("location", {}).get("currentLocation", {})
        if not isinstance(metadata, dict):
            return None, 0, {}, {}, {}
        if not isinstance(location, dict):
            location = {}

        previous = parse_json(metadata.get("previousSearchParams"))
        postback = parse_json(metadata.get("postbackParams"))
        search_id = (
            postback.get("search_id")
            or previous.get("PreviousSearchId")
        )
        total = int(metadata.get("totalResults") or 0)
        if not search_id:
            return None, total, postback, metadata, location
        return search_id, total, postback, metadata, location
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None, 0, {}, {}, {}


def make_filters(search_id, page, offset, shown, total, search_metadata=None):
    if search_metadata:
        metadata = dict(search_metadata)
    else:
        previous = {
            "PreviousSearchId": search_id,
            "PreviousSearchFilter": [
                '{"category_context":"go_out_home"}',
                "",
                '{"context":"dineout_home"}',
            ],
        }
        postback = {
            "search_id": search_id,
            "solr_offset": offset,
            "page": page,
            "total_restaurants_shown": shown,
            "total_results_shown": shown,
            "vg_set": True,
        }
        metadata = {
            "previousSearchParams": json.dumps(previous, separators=(",", ":")),
            "postbackParams": json.dumps(postback, separators=(",", ":")),
            "totalResults": total,
            "hasMore": True,
            "getInactive": False,
        }

    filters = {
        "searchMetadata": metadata,
        "dineoutAdsMetaData": {},
        "appliedFilter": [
            {
                "filterType": "category_sheet",
                "filterValue": "go_out_home",
                "isHidden": True,
                "isApplied": True,
                "postKey": '{"category_context":"go_out_home"}',
            },
            {
                "filterType": "context",
                "filterValue": "dineout_home",
                "isHidden": True,
                "isApplied": True,
                "postKey": '{"context":"dineout_home"}',
            },
        ],
        "urlParamsForAds": {},
    }
    return json.dumps(filters, separators=(",", ":"))


def make_search_payload(city, search_id, page, offset, shown, total, search_metadata=None):
    payload = {
        "context": "dineout",
        "filters": make_filters(
            search_id, page, offset, shown, total, search_metadata
        ),
        "addressId": city.get("address_id", 0),
        "entityId": str(city["entity_id"]),
        "entityType": str(city["entity_type"]),
        "locationType": str(city.get("location_type", "")),
        "isOrderLocation": city.get("is_order_location", 1),
        "cityId": str(city["city_id"]),
        "latitude": float(city["latitude"]),
        "longitude": float(city["longitude"]),
        "userDefinedLatitude": float(city["user_defined_latitude"]),
        "userDefinedLongitude": float(city["user_defined_longitude"]),
        "entityName": str(city.get("entity_name", city["display_title"])),
        "orderLocationName": str(city.get("order_location_name", city["display_title"])),
        "cityName": str(city["city_name"]),
        "countryId": str(city.get("country_id", 1)),
        "countryName": str(city["country_name"]),
        "displayTitle": str(city["display_title"]),
        "o2Serviceable": city["o2_serviceable"],
        "placeId": str(city["place_id"]),
        "deliverySubzoneId": city["delivery_subzone_id"],
        "placeType": str(city["place_type"]),
        "cellId": str(city["cell_id"]),
        "placeName": str(city["place_name"]),
        "isO2City": city["is_o2_city"],
        "fetchFromGoogle": city["fetch_from_google"],
        "fetchedFromCookie": city["fetched_from_cookie"],
        "isO2OnlyCity": city.get("is_o2_only_city", False),
        "address_template": [],
        "otherRestaurantsUrl": "",
    }
    return payload


def normalize_search_metadata(meta):
    """Return the nested server metadata in a predictable Python structure."""
    meta = meta if isinstance(meta, dict) else {}
    previous_raw = meta.get("previousSearchParams", "{}")
    postback_raw = meta.get("postbackParams", "{}")
    previous = parse_json(previous_raw)
    postback = parse_json(postback_raw)

    normalized = dict(meta)
    normalized["_previous"] = previous
    normalized["_postback"] = postback
    return normalized


def next_search_metadata(meta, fallback_page, fallback_offset, fallback_shown):
    """
    Use Zomato's returned pagination cursor unchanged.

    The server's postbackParams already contains the next offset.
    Do not add another 12 to it.
    """

    normalized = normalize_search_metadata(meta)

    previous = normalized.get("_previous") or {}
    postback = normalized.get("_postback") or {}

    search_id = (
        postback.get("search_id")
        or previous.get("PreviousSearchId")
    )

    if not search_id:
        return None, None, None, None

    try:
        response_page = int(postback.get("page", fallback_page))
        response_offset = int(
            postback.get("solr_offset", fallback_offset)
        )
    except (TypeError, ValueError):
        return None, None, None, None

    # Advance the request page, but preserve the server's offset.
    next_page = response_page + 1
    next_offset = response_offset

    # Keep the original server-issued metadata unchanged.
    next_meta = dict(meta)

    return search_id, next_page, next_offset, next_meta

def search_meta(data):
    sections = page_sections(data)
    section = sections.get("SECTION_SEARCH_META_INFO", {})
    if isinstance(section, dict):
        meta = section.get("searchMetaData", section)
        return meta if isinstance(meta, dict) else {}
    return {}


def search_cards(data):
    cards = page_sections(data).get("SECTION_SEARCH_RESULT", [])
    return cards if isinstance(cards, list) else []


def card_to_restaurant(card, city_name):
    info = card.get("info") if isinstance(card, dict) else {}
    info = info if isinstance(info, dict) else {}
    action = card.get("cardAction") or {}
    click_url = action.get("clickUrl") if isinstance(action, dict) else None
    if not click_url:
        click_url = info.get("url") or info.get("urlSlug")
    restaurant_url = urljoin(BASE_URL, click_url) if click_url else ""

    restaurant_id = first_value(info, "resId", "res_id", "restaurantId")
    restaurant_name = first_value(info, "name", "resName", "title")
    if not restaurant_id and not restaurant_url:
        return None

    cuisines = info.get("cuisine") or info.get("cuisines") or []
    if isinstance(cuisines, list):
        cuisines = clean([
            item.get("name", item) if isinstance(item, dict) else item
            for item in cuisines
        ])
    else:
        cuisines = clean(cuisines)

    rating = info.get("rating")
    rating_new = info.get("ratingNew")
    if isinstance(rating_new, dict):
        dining = (rating_new.get("ratings") or {}).get("DINING") or {}
        rating = dining.get("rating") or rating

    review_count = first_value(
        info, "ratingCount", "reviewCount", "reviewsCount", "ratingsCount",
        default=""
    )
    if isinstance(review_count, dict):
        review_count = first_value(review_count, "text", "count", default="")

    return {
        "restaurant_id": str(restaurant_id) if restaurant_id is not None else "",
        "restaurant_name": clean(restaurant_name),
        "restaurant_url": restaurant_url,
        "city": city_name,
        "cuisine": cuisines,
        "rating": clean(rating),
        "reviews_count": clean(review_count),
    }



def discover_restaurants(
    session, city, search_id,
    start_page, start_offset, start_shown, start_total,
    search_metadata=None, url_collection=None, stop_check=None, progress_callback=None,
):
    log("\n[3/5] Discovering restaurants with sequential pagination...")
    discovered = {}
    previous_signatures = set()
    pages_done = 0

    page = int(start_page)
    offset = int(start_offset)
    shown = int(start_shown)
    total = int(start_total or 0)
    has_more = True

    while has_more:
        if stop_check and stop_check():
            log("Discovery cancelled by the dashboard.")
            break
        payload = make_search_payload(
            city, search_id, page, offset, shown, total,
            search_metadata=search_metadata,
        )
        data = request_json(session, "POST", SEARCH_URL, json=payload)
        cards = search_cards(data)
        meta = search_meta(data)
        normalized = normalize_search_metadata(meta)

        postback = normalized.get("_postback") or {}

        log(
            f"PAGINATION DEBUG | "
            f"Request page={page} | "
            f"Request offset={offset} | "
            f"Server page={postback.get('page')} | "
            f"Server offset={postback.get('solr_offset')} | "
            f"Server shown={postback.get('total_restaurants_shown')} | "
            f"hasMore={meta.get('hasMore')} | "
            f"total={meta.get('totalResults')}"
        )

        if not cards:
            with open("search_response_unrecognized.json", "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            if pages_done == 0:
                with open("search_first_request.json", "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                raise RuntimeError(
                    "The first Dineout API response contained zero restaurant "
                    f"cards (totalResults={meta.get('totalResults', 0)}, "
                    f"hasMore={meta.get('hasMore', False)}). "
                    "Saved search_response_unrecognized.json. If the city "
                    "page shows restaurants, refresh the session cookies and "
                    "retry. The request used the genuine search ID from the "
                    "city page; no search ID was fabricated."
                )
            log(f"Page {page} returned zero cards; stopping pagination.")
            with open("empty_page_diagnostic.json", "w", encoding="utf-8") as f:
                json.dump({
                    "request_payload": payload,
                    "response": data,
                    "request_page": page,
                    "request_offset": offset,
                    "metadata": meta
                }, f, indent=2, ensure_ascii=False)
            break

        ids = []
        added = 0
        for card in cards:
            item = card_to_restaurant(card, city["city_name"])
            if not item:
                continue
            key = item["restaurant_id"] or item["restaurant_url"]
            ids.append(key)
            if key not in discovered:
                discovered[key] = item
                added += 1
            if url_collection is not None and item.get("restaurant_url"):
                url_collection.update_one(
                    {"restaurant_url": item["restaurant_url"]},
                    {"$set": {
                        "restaurant_url": item["restaurant_url"],
                        "restaurant_id": item.get("restaurant_id", ""),
                        "restaurant_name": item.get("restaurant_name", ""),
                        "city": city["city_name"],
                    }},
                    upsert=True,
                )

        signature = tuple(ids)
        if signature in previous_signatures:
            log(f"Page {page} repeated a previous result set; stopping safely.")
            break
        previous_signatures.add(signature)
        pages_done += 1

        try:
            server_total = int(meta.get("totalResults") or 0)
        except (TypeError, ValueError):
            server_total = 0
        if server_total:
            total = server_total

        has_more = bool(meta.get("hasMore", False))
        normalized = normalize_search_metadata(meta)
        postback = normalized.get("_postback") or {}
        log(
            f"Page {page}: {len(cards)} cards; {added} new; "
            f"{len(discovered)} unique; hasMore={has_more}; total={total}"
        )
        if pages_done == 1:
            log(f"Using Zomato's genuine search ID: {search_id}")
        if progress_callback:
            progress_callback(len(discovered), total, page)

        if not has_more:
            break

        if not search_id:
            log("No genuine server-issued search ID; stopping rather than inventing one.")
            break

        next_id, next_page, next_offset, next_meta = next_search_metadata(
            meta, page, offset, shown
        )
        if not next_id or not next_meta:
            log("The response did not provide a usable next-page cursor; stopping safely.")
            break

        search_id = next_id
        page = next_page
        offset = next_offset
        shown = next_offset
        search_metadata = next_meta

        log(f"Waiting {PAGINATION_DELAY:.1f}s before requesting the next search page...")
        time.sleep(PAGINATION_DELAY)

    return list(discovered.values()), total


def recursive_find(obj, wanted_keys):
    if isinstance(obj, dict):
        for key in wanted_keys:
            value = obj.get(key)
            if value not in (None, "", [], {}):
                return value
        for value in obj.values():
            result = recursive_find(value, wanted_keys)
            if result not in (None, "", [], {}):
                return result
    elif isinstance(obj, list):
        for value in obj:
            result = recursive_find(value, wanted_keys)
            if result not in (None, "", [], {}):
                return result
    return None


def get_page(session, page_path):
    return request_json(
        session, "GET", GET_PAGE_URL,
        params={"page_url": page_path, "location": "", "isMobile": "0"},
        headers={"referer": urljoin(BASE_URL, page_path)},
    )


def _normalise_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _format_hours_value(value):
    """
    Convert common Zomato timing shapes into readable text.
    Supports strings, lists, day->time mappings, and nested day/time objects.
    """
    if value is None or value == "":
        return ""

    if isinstance(value, str):
        return clean(value)

    if isinstance(value, (int, float)):
        return str(value)

    if isinstance(value, list):
        parts = []
        for item in value:
            part = _format_hours_value(item)
            if part and part not in parts:
                parts.append(part)
        return "; ".join(parts)

    if isinstance(value, dict):
        # Prefer explicit display/text fields before trying structured schedules.
        preferred = (
            "text", "timingText", "displayText", "displayValue",
            "openingHours", "opening_hours", "hours", "timings",
            "timing", "value", "label", "title", "description",
        )
        parts = []
        for key in preferred:
            if key in value:
                part = _format_hours_value(value[key])
                if part and part not in parts:
                    parts.append(part)
        if parts:
            return "; ".join(parts)

        # Common shape: {"day": "Monday", "time": "12pm - 11pm"}.
        day = first_value(value, "day", "dayName", "weekday")
        time_value = first_value(
            value, "time", "hours", "timing", "openTime",
            "openingTime", "schedule", "value"
        )
        if day and time_value:
            time_text = _format_hours_value(time_value)
            if time_text:
                return f"{clean(day)}: {time_text}"

        # Common shape: {"Monday": "...", "Tuesday": "..."}.
        day_names = {
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        }
        day_parts = []
        for key, item in value.items():
            if _normalise_key(key) in day_names:
                item_text = _format_hours_value(item)
                if item_text:
                    day_parts.append(f"{key}: {item_text}")
        if day_parts:
            return "; ".join(day_parts)

        # Last resort: preserve useful scalar values in an unfamiliar timing
        # object, while excluding IDs and unrelated metadata.
        ignored = {
            "id", "type", "entityid", "resid", "restaurantid",
            "isopen", "open", "closed", "status", "icon",
        }
        for key, item in value.items():
            if _normalise_key(key) in ignored:
                continue
            if isinstance(item, (str, int, float)):
                item_text = clean(item)
            else:
                item_text = _format_hours_value(item)
            if item_text:
                day_parts.append(f"{key}: {item_text}")
        return "; ".join(day_parts)

    return clean(value)


def extract_opening_hours(obj):
    """
    Search the full getPage response, not just four selected sections.
    Zomato has returned timing under different section names and key casing.
    """
    target_keys = {
        "openinghours", "openinghour", "openinghourstext",
        "opening_hours", "timings", "timing", "timingtext",
        "restauranttiming", "restauranttimings", "businesshours",
        "operatinghours", "hours",
    }

    def walk(node):
        if isinstance(node, dict):
            # Check matching keys at this level first.
            for key, value in node.items():
                if _normalise_key(key) in {
                    _normalise_key(item) for item in target_keys
                }:
                    result = _format_hours_value(value)
                    if result:
                        return result
            # Then search nested sections/entities.
            for value in node.values():
                result = walk(value)
                if result:
                    return result
        elif isinstance(node, list):
            for value in node:
                result = walk(value)
                if result:
                    return result
        return ""

    return walk(obj)


def restaurant_details(session, restaurant):
    path = urlparse(restaurant["restaurant_url"]).path.rstrip("/")
    info_path = path if path.endswith("/info") else path + "/info"
    data = get_page(session, info_path)
    sections = page_sections(data)

    contact = sections.get("SECTION_RES_CONTACT", {})
    basic = sections.get("SECTION_BASIC_INFO", {})
    header = sections.get("SECTION_RES_HEADER_DETAILS", {})
    rating_section = sections.get("SECTION_RATING_DATA", {})
    selected = {
        "contact": contact,
        "basic": basic,
        "header": header,
        "rating": rating_section,
    }

    phone = ""
    if isinstance(contact, dict):
        phone_details = contact.get("phoneDetails") or {}
        if isinstance(phone_details, dict):
            phone = clean(phone_details.get("phoneStr"))
    if not phone:
        phone = clean(recursive_find(selected, [
            "phoneStr", "phoneNumber", "mobileNumber", "mobile", "phone"
        ]))

    address = clean(recursive_find(selected, ["address", "formattedAddress"]))
    postal = clean(recursive_find(selected, [
        "zipcode", "zipCode", "postalCode", "pincode", "pinCode"
    ]))

    # First search the complete info response. The previous implementation
    # searched only four sections and could miss timing data elsewhere.
    opening = extract_opening_hours(data)

    # Some restaurant pages expose timings on the main page rather than /info.
    # Use it only when /info contains no opening-hours value.
    if not opening:
        try:
            overview_data = get_page(session, path)
            opening = extract_opening_hours(overview_data)
        except PermissionError:
            raise
        except Exception as exc:
            log(f"    Opening-hours fallback request failed: {exc}")

    cuisine = clean(recursive_find(selected, ["cuisines", "cuisine"]))
    rating = clean(recursive_find(selected, ["rating", "aggregateRating"]))
    return {
        "address": address,
        "postal_code": postal,
        "mobile_number": phone,
        "opening_hours": opening,
        "cuisine": cuisine or restaurant.get("cuisine", ""),
        "rating": rating or restaurant.get("rating", ""),
    }



def _find_review_section(data):
    """Find SECTION_REVIEWS even when Zomato nests it under page_data."""
    if isinstance(data, dict):
        sections = page_sections(data)
        section = sections.get("SECTION_REVIEWS")
        if isinstance(section, dict):
            return section
        for value in data.values():
            found = _find_review_section(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_review_section(value)
            if found:
                return found
    return {}


def _review_entities(data):
    """Locate the REVIEWS entity map in Zomato's response."""
    if isinstance(data, dict):
        entities = data.get("entities")
        if isinstance(entities, dict) and "REVIEWS" in entities:
            return entities.get("REVIEWS") or {}
        for value in data.values():
            found = _review_entities(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _review_entities(value)
            if found:
                return found
    return {}


def extract_review_items(data):
    """
    Return [(review_id, reviewText), ...] plus the SECTION_REVIEWS metadata.

    Supports the usual sections/entities response and nested response layouts.
    Only reviewText is stored in MongoDB; IDs are used internally for
    pagination deduplication and are never added to the MongoDB schema.
    """
    section = _find_review_section(data)
    ids = []

    def collect_ids(node):
        if isinstance(node, dict):
            entity_ids = node.get("entity_ids")
            if isinstance(entity_ids, list):
                ids.extend(str(value) for value in entity_ids)
            for value in node.values():
                collect_ids(value)
        elif isinstance(node, list):
            for value in node:
                collect_ids(value)

    collect_ids(section.get("entities", []))
    entities = _review_entities(data)

    items = []
    if isinstance(entities, dict):
        for review_id in ids:
            review = entities.get(str(review_id), {})
            if isinstance(review, dict):
                # Track every review ID for pagination, even when reviewText is
                # blank. Only non-empty reviewText values are stored in MongoDB.
                text = clean(review.get("reviewText") or review.get("review_text"))
                items.append((str(review_id), text))
    elif isinstance(entities, list):
        for index, review in enumerate(entities):
            if isinstance(review, dict):
                review_id = first_value(
                    review, "reviewId", "review_id", "id", default=str(index)
                )
                # Retain the ID even for an empty reviewText so pagination
                # does not stop just because a page contains textless reviews.
                text = clean(review.get("reviewText") or review.get("review_text"))
                items.append((str(review_id), text))

    # Some response variants include review objects directly in the section.
    if not items:
        def walk_for_reviews(node):
            found = []
            if isinstance(node, dict):
                if "reviewText" in node or "review_text" in node:
                    text = clean(node.get("reviewText") or node.get("review_text"))
                    review_id = first_value(
                        node, "reviewId", "review_id", "id",
                        default=f"text:{text[:80]}"
                    )
                    found.append((str(review_id), text))
                for value in node.values():
                    found.extend(walk_for_reviews(value))
            elif isinstance(node, list):
                for value in node:
                    found.extend(walk_for_reviews(value))
            return found

        items = walk_for_reviews(section)

    # Preserve order while removing repeated IDs within one response.
    deduped = []
    seen = set()
    for review_id, text in items:
        if review_id not in seen:
            seen.add(review_id)
            deduped.append((review_id, text))
    return deduped, section


def extract_review_texts(data):
    """Compatibility wrapper used by any other existing code."""
    items, section = extract_review_items(data)
    return [text for _, text in items], section


def extract_reviews(
    session, restaurant, stop_check=None, max_reviews=None,
    start_page=2, already_saved_count=0, on_page=None, on_progress=None,
):
    """Collect written review texts from the normal review pagination.

    The configured MAX_REVIEW_PAGES boundary is respected. The reported
    Zomato review count includes reviews that may not contain written text, so
    it is informational only and is never used as the count of saved texts.
    Each successfully processed page is checkpointed through ``on_page``.
    """
    path = urlparse(restaurant["restaurant_url"]).path.rstrip("/")
    if path.endswith("/info"):
        path = path[:-5]
    review_path = path + "/reviews"

    if on_progress:
        on_progress("Fetching reviews", 1)
    first = get_page(session, review_path)
    first_items, section = extract_review_items(first)
    label = clean(section.get("pageReviewsText", ""))
    match = re.search(r"of\s+([\d,]+)\s+reviews", label, re.I)
    total_reviews = int(match.group(1).replace(",", "")) if match else 0

    try:
        current_page = int(section.get("currentPage", 1) or 1)
    except (TypeError, ValueError):
        current_page = 1
    try:
        declared_pages = int(section.get("numberOfPages", 1) or 1)
    except (TypeError, ValueError):
        declared_pages = 1

    # Track review IDs for pagination even when the review has no written text.
    seen_ids = set()
    all_items = []
    for review_id, text in first_items:
        rid = str(review_id)
        if rid not in seen_ids:
            seen_ids.add(rid)
            all_items.append((rid, text))

    # On a fresh extraction, persist page 1. On resume, the existing checkpoint
    # already records it; do not replay it unless no text records were saved.
    if on_page and int(already_saved_count or 0) == 0 and int(start_page or 2) <= current_page + 1:
        on_page(current_page, first_items, total_reviews, current_page + 1)

    log(
        f"      Reviews: page 1 returned={len(first_items)} items; "
        f"written_texts_on_page={sum(1 for _, text in first_items if text)}; "
        f"Zomato reported total={total_reviews if match else 'unknown'}; "
        f"API declares {declared_pages} page(s); "
        f"configured page limit={MAX_REVIEW_PAGES or 'unlimited'}"
    )

    if declared_pages <= current_page:
        return ", ".join(text for _, text in all_items if text), total_reviews, "complete"

    status = "complete"
    page = max(int(start_page or 2), current_page + 1)

    while True:
        if stop_check and stop_check():
            status = "cancelled"
            log("      Review extraction cancelled by the dashboard.")
            break

        if MAX_REVIEW_PAGES and page > MAX_REVIEW_PAGES:
            status = "partial_page_limit"
            log(
                f"      Review page limit reached: processed pages 1-"
                f"{MAX_REVIEW_PAGES}; Zomato reported total="
                f"{total_reviews if match else 'unknown'}"
            )
            break

        if declared_pages and page > declared_pages:
            status = "complete"
            break

        try:
            if on_progress:
                on_progress("Fetching reviews", page)
            data = request_json(
                session, "GET", REVIEWS_URL,
                params={
                    "sort": "dd", "filter": "reviews-dining",
                    "res_id": str(restaurant.get("restaurant_id") or ""),
                    "page": str(page),
                },
                headers={"referer": urljoin(BASE_URL, review_path)},
            )
            more_items, more_section = extract_review_items(data)

            new_items = []
            for review_id, text in more_items:
                rid = str(review_id)
                if rid not in seen_ids:
                    seen_ids.add(rid)
                    all_items.append((rid, text))
                    new_items.append((rid, text))

            log(
                f"      Review page {page}: received={len(more_items)}, "
                f"new_ids={len(new_items)}, "
                f"written_texts={sum(1 for _, text in more_items if text)}"
            )

            try:
                response_page = int(more_section.get("currentPage", page) or page)
                next_page = max(page + 1, response_page + 1)
            except (TypeError, ValueError):
                next_page = page + 1

            has_more = first_value(
                more_section, "hasMore", "has_more", "hasNextPage", default=None
            )
            if isinstance(has_more, str):
                has_more = has_more.lower() == "true"

            if on_page:
                # Advance even when a page contains only reviews without text;
                # those IDs still represent a successfully processed page.
                checkpoint_next = next_page if new_items else page
                on_page(page, more_items, total_reviews, checkpoint_next)

            if has_more is False:
                status = "complete"
                break
            if declared_pages and response_page >= declared_pages:
                status = "complete"
                break
            if not more_items or not new_items:
                status = "partial_no_new_reviews"
                log(f"      No new review IDs on page {page}; stopping safely.")
                break

            page = next_page
            time.sleep(REVIEW_PAGE_DELAY)

        except PermissionError:
            status = "partial_401_403"
            log(f"      Review page {page} was blocked by the site; stopping without retry loops.")
            break
        except Exception as exc:
            log(f"      Review page {page} failed: {exc}")
            status = "partial_error"
            break

    all_review_texts = [text for _, text in all_items if text]
    log(
        f"      Review result: status={status}; unique_review_ids_seen={len(seen_ids)}; "
        f"written_texts_seen={len(all_review_texts)}; "
        f"reported_total_reviews={total_reviews}"
    )
    return ", ".join(all_review_texts), total_reviews, status


def mongo_collection_name(city_name):
    # MongoDB collection names cannot contain "." or start with "$".
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", city_name.strip()).strip("_")
    if not name:
        raise RuntimeError("City name cannot be converted to a MongoDB collection name.")
    return name.lower()


def main():
    city_query = input("Enter city name (example: Vijayawada, Hyderabad, Chennai): ").strip()
    if not city_query:
        log("City name is required.")
        return 1

    session = None
    mongo = None
    started = time.time()

    try:
        session = create_session()
        city = resolve_city(session, city_query)

        # Read Zomato's genuine search ID from the city page before calling the API.
        search_state = initial_search_context(session, city)
        start_page = search_state["page"]
        start_offset = search_state["offset"]
        start_shown = search_state["shown"]
        page_total = search_state["total"]
        search_id = search_state["search_id"]
        initial_search_metadata = search_state["search_metadata"]
        log(
            f"Initial API request: page={start_page}, offset={start_offset}, "
            f"search_id={search_id} (from Zomato page metadata)."
        )

        mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo.admin.command("ping")
        collection_name = mongo_collection_name(city["city_name"])
        collection = mongo[MONGO_DB][collection_name]

        # Unique restaurant ID is preferred; URL is a fallback identity.
        collection.create_index(
            [("restaurant_url", 1)],
            unique=True,
            partialFilterExpression={"restaurant_url": {"$type": "string", "$gt": ""}},
            name="unique_nonempty_restaurant_url",
        )

        log("=" * 68)
        log("ZOMATO DYNAMIC CITY DINEOUT PIPELINE")
        log(f"City: {city['city_name']}")
        log(f"MongoDB: {MONGO_DB}.{collection_name}")
        log("MongoDB fields: exactly the 11 requested fields.")
        log("Every restaurant is upserted immediately after extraction.")
        log("=" * 68)

        restaurants, reported_total = discover_restaurants(
            session, city, search_id,
            start_page, start_offset, start_shown, page_total,
            search_metadata=initial_search_metadata,
        )

        if MAX_RESTAURANTS > 0:
            restaurants = restaurants[:MAX_RESTAURANTS]
            log(f"TEST LIMIT enabled: processing {len(restaurants)} restaurants.")

        log("\n[4/5] Extracting restaurant details and reviews...")
        saved = detail_errors = review_errors = mongo_errors = 0

        for index, restaurant in enumerate(restaurants, 1):
            name = restaurant.get("restaurant_name") or restaurant.get("restaurant_id")
            log(f"\n[{index}/{len(restaurants)}] {name}")

            doc = {
                "restaurant_name": restaurant.get("restaurant_name", ""),
                "restaurant_url": restaurant.get("restaurant_url", ""),
                "address": "",
                "postal_code": "",
                "city": city["city_name"],
                "mobile_number": "",
                "opening_hours": "",
                "cuisine": restaurant.get("cuisine", ""),
                "rating": restaurant.get("rating", ""),
                "reviews_count": restaurant.get("reviews_count", ""),
                "reviews": "",
            }

            try:
                details = restaurant_details(session, restaurant)
                doc.update(details)
            except PermissionError:
                raise
            except Exception as exc:
                detail_errors += 1
                log(f"    Detail extraction failed: {exc}")

            try:
                reviews, count, review_status = extract_reviews(session, restaurant)
                doc["reviews"] = reviews
                if count:
                    doc["reviews_count"] = str(count)
                log(f"    Reviews: {review_status}; count={count}")
            except PermissionError:
                raise
            except Exception as exc:
                review_errors += 1
                log(f"    Review extraction failed: {exc}")

            # Store only the 11 requested fields; no diagnostic/raw fields.
            identity = (
                {"restaurant_url": doc["restaurant_url"]}
                if doc["restaurant_url"]
                else {"restaurant_name": doc["restaurant_name"], "city": doc["city"]}
            )
            try:
                result = collection.update_one(identity, {"$set": doc}, upsert=True)
                saved += 1
                if result.upserted_id:
                    log("    MongoDB: inserted")
                elif result.modified_count:
                    log("    MongoDB: updated")
                else:
                    log("    MongoDB: already current")
            except PyMongoError as exc:
                mongo_errors += 1
                log(f"    MongoDB write failed: {exc}")

            time.sleep(REQUEST_DELAY)

        log("\n[5/5] EXTRACTION SUMMARY")
        log(f"City: {city['city_name']}")
        log(f"Search API reported results: {reported_total}")
        log(f"Unique restaurant cards discovered: {len(restaurants)}")
        log(f"Restaurant records inserted/updated: {saved}")
        log(f"Detail extraction failures: {detail_errors}")
        log(f"Review extraction failures: {review_errors}")
        log(f"MongoDB write failures: {mongo_errors}")
        log(f"MongoDB collection: {MONGO_DB}.{collection_name}")
        log(f"Elapsed time: {(time.time() - started):.1f} seconds")
        return 0

    except KeyboardInterrupt:
        log("\nStopped by user. Already saved MongoDB records remain available.")
        return 130
    except Exception as exc:
        log(f"\nPIPELINE STOPPED: {exc}")
        return 1
    finally:
        if session:
            session.close()
        if mongo:
            mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())