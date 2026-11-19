"""Start and reuse exactly three supervised restaurant workers."""
import json
import os
import subprocess
import sys
from pathlib import Path

import redis

from config import REDIS_URL, WORKER_GROUPS, WORKER_PID_KEY

BASE_DIR = Path(__file__).resolve().parent
LEGACY_PROJECT_DIRS = [
    BASE_DIR,
    BASE_DIR.parent / "zomato_redis_review_pipeline",
]


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _proc_args(entry):
    try:
        raw = (entry / "cmdline").read_bytes()
        return [part.decode(errors="ignore") for part in raw.split(b"\0") if part]
    except (OSError, PermissionError):
        return []


def _proc_ppid(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # comm is parenthesized and may contain spaces; fields after its final
        # ')' begin with state, then PPID.
        tail = stat[stat.rfind(")") + 2:].split()
        return int(tail[1])
    except (OSError, ValueError, IndexError):
        return None


def _matches_project(args):
    return any(
        arg.endswith(("worker_runner.py", "worker_supervisor.py"))
        and any(str(project_dir) in arg for project_dir in LEGACY_PROJECT_DIRS)
        for arg in args
    )


def _legacy_worker_processes(allowed_pids=()):
    """Find unmanaged workers; ignore runner children owned by a supervisor."""
    found = []
    proc = Path("/proc")
    if not proc.exists():
        return found

    supervisor_pids = set()
    entries = [entry for entry in proc.iterdir() if entry.name.isdigit()]
    snapshots = {}
    for entry in entries:
        args = _proc_args(entry)
        if args:
            snapshots[int(entry.name)] = args
            if any(arg.endswith("worker_supervisor.py") for arg in args):
                supervisor_pids.add(int(entry.name))

    for pid, args in snapshots.items():
        if pid in allowed_pids or not _matches_project(args):
            continue
        is_runner = any(arg.endswith("worker_runner.py") for arg in args)
        if is_runner and _proc_ppid(pid) in supervisor_pids:
            continue
        found.append(pid)
    return found


def ensure_workers():
    connection = redis.from_url(REDIS_URL, decode_responses=True)
    connection.ping()
    raw = connection.get(WORKER_PID_KEY)
    try:
        pids = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        pids = {}

    registered = [int(pid) for pid in pids.get("restaurant", [])]
    legacy = _legacy_worker_processes(allowed_pids=registered)
    if legacy:
        connection.close()
        raise RuntimeError(
            "Unmanaged/old worker processes are still running for this project "
            f"(PIDs: {', '.join(map(str, legacy))}). Stop them first with: "
            "python3 stop_old_workers.py"
        )

    live = [pid for pid in registered if _alive(pid)]
    started = []

    for index in range(len(live) + 1, WORKER_GROUPS["restaurant"] + 1):
        name = f"restaurant_worker_{index}_{os.getpid()}"
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / f"{name}.log"
        log_handle = open(log_path, "a", buffering=1)
        process = subprocess.Popen(
            [
                sys.executable, "-u", str(BASE_DIR / "worker_supervisor.py"),
                "--name", name,
            ],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            # Critical: supervisor remains in Streamlit's foreground process
            # group, so terminal Ctrl+C reaches it. The RQ worker itself is
            # isolated by worker_supervisor.py and gets a graceful signal.
            start_new_session=False,
            close_fds=True,
        )
        log_handle.close()
        live.append(process.pid)
        started.append({
            "type": "restaurant", "name": name,
            "pid": process.pid, "log": str(log_path),
        })
        print(
            f"Started supervised {name} | PID={process.pid} | log={log_path}",
            flush=True,
        )

    pids = {"restaurant": live[:WORKER_GROUPS["restaurant"]]}
    connection.set(WORKER_PID_KEY, json.dumps(pids))
    connection.close()
    return started, pids
