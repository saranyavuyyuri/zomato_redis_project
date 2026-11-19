"""Show restaurant queue depth and active jobs for this project."""
import redis
from rq import Queue, Worker
from config import REDIS_URL, RESTAURANT_QUEUE

connection = redis.from_url(REDIS_URL)
queue = Queue(RESTAURANT_QUEUE, connection=connection)
print(f"Waiting restaurant jobs: {queue.count}")
print("\nACTIVE RESTAURANT WORKERS")
for worker in Worker.all(connection=connection):
    if any(q.name == RESTAURANT_QUEUE for q in worker.queues):
        job = worker.get_current_job()
        if job:
            city = job.args[1] if len(job.args) > 1 else "?"
            print(f"{worker.name} | {worker.get_state()} | {city} | {job.args[0].get('restaurant_name', '?')}")
        else:
            print(f"{worker.name} | {worker.get_state()} | idle")
connection.close()
