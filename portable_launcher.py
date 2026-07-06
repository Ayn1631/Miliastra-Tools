from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
START_PORT = 8501


def find_free_port(start: int = START_PORT, attempts: int = 50) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free port in range {start}-{start + attempts - 1}")


def open_browser_when_ready(url: str, timeout: float = 30.0) -> None:
    health = f"{url}/_stcore/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=0.8) as response:
                if response.status == 200:
                    webbrowser.open(url, new=1)
                    return
        except Exception:
            time.sleep(0.25)
    webbrowser.open(url, new=1)


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    app_path = app_dir / "streamlit_app.py"
    if not app_path.exists():
        print(f"[ERROR] Missing app entry: {app_path}")
        return 2

    port = find_free_port()
    url = f"http://{HOST}:{port}"

    env = os.environ.copy()
    bypass = "127.0.0.1,localhost"
    env["NO_PROXY"] = bypass
    env["no_proxy"] = bypass

    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        f"--server.address={HOST}",
        f"--server.port={port}",
        "--server.headless=true",
        "--server.fileWatcherType=none",
        "--browser.gatherUsageStats=false",
    ]

    print(f"[i2gia] Starting: {url}")
    print(f"[i2gia] Python: {sys.executable}")

    threading.Thread(
        target=open_browser_when_ready,
        args=(url,),
        daemon=True,
    ).start()

    process = subprocess.Popen(command, cwd=str(app_dir), env=env)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
