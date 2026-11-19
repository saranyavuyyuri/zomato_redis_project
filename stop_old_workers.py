"""Gracefully stop this project's supervised and legacy RQ workers.

The command never clears Redis queues or deletes MongoDB data. It sends a
shutdown request and waits for the current restaurant jobs to finish.
"""
import json
import os
import signal
import time
from pathlib import Path

import redis

from config import REDIS_URL, WORKER_PID_KEY

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIRS = [BASE_DIR, BASE_DIR.parent / "zomato_redis_review_pipeline"]


def _args(entry):
    try:
        return [
            part.decode(errors="ignore")
            for part in (entry / "cmdline").read_bytes().split(b"\0")
            if part
        ]
    except (OSError, PermissionError):
        return []


def _ppid(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat[stat.rfind(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _is_running(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        state = stat[stat.rfind(")") + 2:].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return False


def find_workers():
    snapshots = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        args = _args(entry)
        if not args:
            continue
        pid = int(entry.name)
        matches = any(
            arg.endswith(("worker_runner.py", "worker_supervisor.py"))
            and any(str(directory) in arg for directory in PROJECT_DIRS)
            for arg in args
        )
        if matches:
            snapshots[pid] = args

    supervisors = {
        pid for pid, args in snapshots.items()
        if any(arg.endswith("worker_supervisor.py") for arg in args)
    }
    found = []
    for pid, args in snapshots.items():
        is_runner = any(arg.endswith("worker_runner.py") for arg in args)
        # A supervised runner is stopped through its supervisor, which forwards
        # the signal. Direct runners from earlier versions are signalled here.
        if is_runner and _ppid(pid) in supervisors:
            continue
        found.append((pid, args))
    return found


def _refresh_registry(stopped_pids):
    try:
        connection = redis.from_url(REDIS_URL, decode_responses=True)
        raw = connection.get(WORKER_PID_KEY)
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {}
            data["restaurant"] = [
                int(pid) for pid in data.get("restaurant", [])
                if int(pid) not in stopped_pids and _is_running(int(pid))
            ]
            connection.set(WORKER_PID_KEY, json.dumps(data))
        connection.close()
    except Exception as exc:
        print(f"Could not refresh worker PID registry (workers were still signalled): {exc}")


def main():
    workers = find_workers()
    if not workers:
        print("No worker processes found for this project.")
        _refresh_registry(set())
        return

    requested = set()
    for pid, args in workers:
        name = next((arg.split("worker_", 1)[-1] for arg in args if "worker_" in arg), "worker")
        print(f"Requesting graceful shutdown: PID={pid} ({name})")
        try:
            os.kill(pid, signal.SIGINT)
            requested.add(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f"Permission denied for PID {pid}; run this script as the same user that started it.")

    print("Waiting for active restaurant jobs to finish. No queued jobs will be deleted...")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        remaining = [pid for pid in requested if _is_running(pid)]
        if not remaining:
            break
        time.sleep(1)

    remaining = [pid for pid in requested if _is_running(pid)]
    _refresh_registry(requested - set(remaining))
    if remaining:
        print(
            "Still shutting down these PIDs: " + ", ".join(map(str, remaining))
            + ". They may be finishing a long restaurant job. Do not force-kill them; "
              "check their worker logs and run this command again."
        )
    else:
        print("All matching workers have stopped. Redis queues and MongoDB data are preserved.")


if __name__ == "__main__":
    main()
