from pymongo import MongoClient, ASCENDING, UpdateOne
from config import MONGO_URI, MONGO_DB

RESTAURANT_FIELDS = [
    "restaurant_name", "restaurant_url", "address", "postal_code", "city",
    "mobile_number", "opening_hours", "cuisine", "rating", "reviews_count", "reviews"
]


class RestaurantDatabase:
    """MongoDB access for city restaurant records and independently stored reviews.

    Review texts live in a city-specific companion collection. This avoids the
    16 MB MongoDB document limit for restaurants with very large review histories
    and keeps review extraction checkpoints out of the restaurant documents.
    """

    def __init__(self):
        self.client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        self.client.admin.command("ping")
        self.database = self.client[MONGO_DB]

    @staticmethod
    def _collection_name(city):
        import re
        name = re.sub(r"[^a-zA-Z0-9_-]+", "_", city.strip()).strip("_").lower()
        if not name:
            raise ValueError("Invalid city name for MongoDB collection")
        return name

    def get_collection(self, city):
        collection = self.database[self._collection_name(city)]
        for index in collection.list_indexes():
            keys = list(index.get("key", {}).items())
            if keys == [("restaurant_url", ASCENDING)] and index.get("unique"):
                return collection
        collection.create_index(
            [("restaurant_url", ASCENDING)], unique=True,
            partialFilterExpression={"restaurant_url": {"$type": "string", "$gt": ""}},
            name="unique_nonempty_restaurant_url",
        )
        return collection

    def get_url_collection(self, city):
        return self.database[f"{self._collection_name(city)}_url"]

    def get_reviews_collection(self, city):
        collection = self.database[f"{self._collection_name(city)}_reviews"]
        collection.create_index(
            [("restaurant_url", ASCENDING), ("review_id", ASCENDING)],
            unique=True, name="unique_restaurant_review_id",
        )
        collection.create_index(
            [("restaurant_url", ASCENDING), ("page", ASCENDING), ("position", ASCENDING)],
            name="reviews_export_order",
        )
        return collection

    def get_review_progress_collection(self, city):
        collection = self.database[f"{self._collection_name(city)}_review_progress"]
        collection.create_index([("restaurant_url", ASCENDING)], unique=True)
        return collection

    def save_restaurant_url(self, city, restaurant):
        url = restaurant.get("restaurant_url")
        if not url:
            return None
        document = {
            "restaurant_url": url,
            "restaurant_id": restaurant.get("restaurant_id", ""),
            "restaurant_name": restaurant.get("restaurant_name", ""),
            "city": city,
        }
        return self.get_url_collection(city).update_one(
            {"restaurant_url": url}, {"$set": document}, upsert=True
        )

    def save_restaurant(self, city, restaurant):
        collection = self.get_collection(city)
        document = {field: restaurant.get(field, "") for field in RESTAURANT_FIELDS}
        # A detail job must never erase review data saved by a review worker.
        if document.get("reviews") in (None, "", []):
            document.pop("reviews", None)
        url = document.get("restaurant_url")
        identity = ({"restaurant_url": url} if url else {
            "restaurant_name": document.get("restaurant_name", ""),
            "city": document.get("city", city),
        })
        return collection.update_one(identity, {"$set": document}, upsert=True)

    def get_review_checkpoint(self, city, restaurant_url):
        doc = self.get_review_progress_collection(city).find_one(
            {"restaurant_url": restaurant_url},
            {"_id": 0, "next_page": 1, "total_reviews": 1, "status": 1},
        ) or {}
        return {
            "next_page": int(doc.get("next_page", 2) or 2),
            "total_reviews": int(doc.get("total_reviews", 0) or 0),
            "status": doc.get("status", "pending"),
        }

    def count_saved_reviews(self, city, restaurant_url):
        return self.get_reviews_collection(city).count_documents(
            {
                "restaurant_url": restaurant_url,
                "review_text": {"$type": "string", "$ne": ""},
            }
        )

    def save_review_page(self, city, restaurant, page, items, total_reviews, next_page):
        """Idempotently upsert one fetched page, then advance its checkpoint.

        The page's review IDs make retries safe: a crash between writing review
        records and advancing the checkpoint cannot create duplicate records.
        """
        url = restaurant.get("restaurant_url", "")
        if not url:
            raise ValueError("Cannot save reviews without restaurant_url")
        reviews = self.get_reviews_collection(city)
        operations = []
        restaurant_name = restaurant.get("restaurant_name", "")
        for position, (review_id, review_text) in enumerate(items):
            # The user's requirement is written review text only. Keep the page
            # checkpoint even for textless reviews, but never insert empty text.
            text = str(review_text or "").strip()
            if not text:
                continue
            rid = str(review_id or f"page-{page}-position-{position}")
            operations.append(UpdateOne(
                {"restaurant_url": url, "review_id": rid},
                {"$set": {
                    "restaurant_url": url,
                    "review_id": rid,
                    "review_text": text,
                    "restaurant_name": restaurant_name,
                    "city": city,
                    "page": int(page),
                    "position": int(position),
                }},
                upsert=True,
            ))
        if operations:
            reviews.bulk_write(operations, ordered=False)

        # Keep the restaurant's public total-review-count field current.
        self.get_collection(city).update_one(
            {"restaurant_url": url},
            {"$set": {"reviews_count": str(int(total_reviews or 0))}},
        )

        progress = self.get_review_progress_collection(city)
        progress.update_one(
            {"restaurant_url": url},
            {
                "$set": {
                    "restaurant_url": url,
                    "restaurant_name": restaurant_name,
                    "city": city,
                    "total_reviews": int(total_reviews or 0),
                    "last_saved_page": int(page),
                    "status": "in_progress",
                },
                "$max": {"next_page": int(next_page)},
            },
            upsert=True,
        )
        return reviews.count_documents({
            "restaurant_url": url,
            "review_text": {"$type": "string", "$ne": ""},
        })

    def set_review_status(self, city, restaurant_url, status, total_reviews=None):
        values = {"status": status}
        if total_reviews is not None:
            values["total_reviews"] = int(total_reviews or 0)
        self.get_review_progress_collection(city).update_one(
            {"restaurant_url": restaurant_url}, {"$set": values}, upsert=True
        )

    def export_records(self, city, review_limit=100):
        records = list(self.get_collection(city).find({}, {"_id": 0}))
        review_collection = self.get_reviews_collection(city)
        for record in records:
            url = record.get("restaurant_url", "")
            if url:
                cursor = review_collection.find(
                    {"restaurant_url": url}, {"_id": 0, "review_text": 1}
                ).sort([("page", ASCENDING), ("position", ASCENDING)])
                if review_limit is not None:
                    cursor = cursor.limit(int(review_limit))
                texts = [item.get("review_text", "") for item in cursor if item.get("review_text")]
                if texts:
                    record["reviews"] = texts
                elif isinstance(record.get("reviews"), str):
                    # Preserve legacy data from before reviews were split out.
                    record["reviews"] = record["reviews"]
            elif isinstance(record.get("reviews"), list) and review_limit is not None:
                record["reviews"] = record["reviews"][:int(review_limit)]
        return records

    def count(self, city):
        return self.get_collection(city).count_documents({})

    def close(self):
        self.client.close()
