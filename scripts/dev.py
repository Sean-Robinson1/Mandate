"""Start both services for local use: Valio Aimo's gateway on :8100 and the buyer's console on :8000.

    .venv/Scripts/python -m scripts.dev

They are separate processes that talk only over HTTP, as they would at two companies. Ctrl+C stops both.
"""
import subprocess
import sys
import time
import urllib.request

SERVICES = [
    ("Valio Aimo gateway", ["gateway.api:create_app", "--factory", "--port", "8100"], "http://127.0.0.1:8100/docs"),
    ("Buyer console", ["app.web:app", "--port", "8000"], "http://127.0.0.1:8000"),
]


def wait_until_up(url: str, seconds: float = 20) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except OSError:
            time.sleep(0.3)
    return False


def main() -> int:
    procs = []
    try:
        for name, args, url in SERVICES:
            procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", *args, "--log-level", "warning"]))
            print(f"{name:20} {url}  {'up' if wait_until_up(url) else 'NOT RESPONDING'}", flush=True)
        print("Ctrl+C to stop.", flush=True)
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
