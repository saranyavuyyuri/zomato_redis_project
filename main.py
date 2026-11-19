
import os
import redis

from rq import Queue

from config import (
    ZOMATO_COOKIE,
    ZOMATO_CSRF_TOKEN,
    REDIS_URL,
    QUEUE_NAME,
    REQUEST_TIMEOUT,
    REQUEST_DELAY,
    PAGINATION_DELAY
)

# Set authentication before importing the original scraper.
os.environ["ZOMATO_COOKIE_HEADER"] = ZOMATO_COOKIE
os.environ["ZOMATO_CSRF_TOKEN"] = ZOMATO_CSRF_TOKEN

import zomato_cities_original as original

from worker import process_restaurant_job


# Temporary test limit.
TEST_RESTAURANT_LIMIT = 5


def main():

    print("=" * 65)
    print("       ZOMATO REDIS + RQ RESTAURANT PRODUCER")
    print("=" * 65)

    city_query = input("\nEnter city name: ").strip()

    if not city_query:
        print("City name cannot be empty.")
        return

    connection = redis.from_url(REDIS_URL)
    session = None

    try:

        # 1. Verify Redis.
        connection.ping()

        print("\nRedis connected successfully!")

        queue = Queue(
            QUEUE_NAME,
            connection=connection
        )

        # 2. Configure original scraper.
        original.REQUEST_TIMEOUT = REQUEST_TIMEOUT
        original.REQUEST_DELAY = REQUEST_DELAY
        original.PAGINATION_DELAY = PAGINATION_DELAY

        # 3. Create authenticated session.
        session = original.create_session()

        # 4. Resolve city.
        print("\nResolving city...")

        city = original.resolve_city(
            session,
            city_query
        )

        print("\nResolved city:", city["city_name"])
        print("City ID:", city["city_id"])
        print("Entity ID:", city["entity_id"])

        # 5. Obtain genuine search context.
        print("\nGetting genuine search context...")

        search_state = original.initial_search_context(
            session,
            city
        )

        # 6. Discover restaurant listings.
        print("\nDiscovering restaurants...")

        restaurants, reported_total = (
            original.discover_restaurants(
                session,
                city,
                search_state["search_id"],
                search_state["page"],
                search_state["offset"],
                search_state["shown"],
                search_state["total"],
                search_metadata=search_state["search_metadata"]
            )
        )

        print("\nDISCOVERY COMPLETED")
        print("API reported total:", reported_total)
        print("Unique restaurants discovered:", len(restaurants))

        # 7. Limit test batch.
        restaurants = restaurants[:TEST_RESTAURANT_LIMIT]

        print(
            f"\nTEST MODE: Submitting only "
            f"{len(restaurants)} restaurants."
        )

        if not restaurants:
            print("No restaurants found.")
            return

        # 8. Submit jobs to Redis.
        print("\nSubmitting jobs to Redis...")

        queued = 0
        failed = 0

        for index, restaurant in enumerate(restaurants, 1):

            try:

                job = queue.enqueue(
                    process_restaurant_job,
                    restaurant,
                    city["city_name"],
                    job_timeout="30m",
                    result_ttl=86400,
                    failure_ttl=604800
                )

                queued += 1

                print(
                    f"[{index}/{len(restaurants)}] "
                    f"QUEUED: "
                    f"{restaurant.get('restaurant_name')}"
                )

            except Exception as error:

                failed += 1

                print("ENQUEUE ERROR:", error)

        # 9. Summary.
        print("\n" + "=" * 65)
        print("REDIS TEST SUMMARY")
        print("=" * 65)

        print("City:", city["city_name"])
        print("Discovered:", len(restaurants))
        print("Queued:", queued)
        print("Enqueue failures:", failed)
        print("Queue:", QUEUE_NAME)
        print("Waiting jobs:", queue.count)

        print("\nThree workers will process the restaurant jobs.")

    except Exception as error:

        print("\nPIPELINE ERROR:")
        print(type(error).__name__, ":", error)

    finally:

        if session:
            session.close()

        connection.close()


if __name__ == "__main__":
    main()
