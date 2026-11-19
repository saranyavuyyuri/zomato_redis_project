"""Keep an RQ worker detached, but forward terminal shutdown signals to it.

The supervisor stays in Streamlit's foreground process group. When the user
presses Ctrl+C, it asks the isolated RQ worker to perform RQ's warm shutdown:
finish the current restaurant job, then exit without taking another queued job.
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    worker = None
    shutting_down = False

    def request_shutdown(signum, _frame):
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        print(
            f"[SUPERVISOR] Received {signal.Signals(signum).name}; "
            "requesting graceful RQ worker shutdown...",
            flush=True,
        )
        if worker is not None and worker.poll() is None:
            try:
                # RQ handles SIGINT as a warm shutdown: current job finishes,
                # then the worker exits without reserving another queued job.
                os.kill(worker.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    command = [
        sys.executable, "-u", str(BASE_DIR / "worker_runner.py"),
        "--name", args.name,
    ]
    print(f"[SUPERVISOR] Starting isolated worker: {args.name}", flush=True)
    worker = subprocess.Popen(
        command,
        cwd=str(BASE_DIR),
        stdin=subprocess.DEVNULL,
        # Isolate the RQ worker so Ctrl+C reaches this supervisor first.
        # The supervisor forwards the signal and waits for the active job.
        start_new_session=True,
        close_fds=True,
    )

    try:
        return_code = worker.wait()
    except KeyboardInterrupt:
        # Defensive fallback for environments that raise KeyboardInterrupt
        # rather than delivering the installed signal handler normally.
        request_shutdown(signal.SIGINT, None)
        return_code = worker.wait()

    print(
        f"[SUPERVISOR] Worker {args.name} exited with code {return_code}",
        flush=True,
    )
    return return_code if return_code is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
