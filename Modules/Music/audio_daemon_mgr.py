# LaLaDash/audio_daemon_mgr.py
from __future__ import annotations
import atexit
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Optional

HOST = "127.0.0.1"
PORT = 34211

_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_started_by_us: bool = False

def _port_in_use(host: str = HOST, port: int = PORT) -> bool:
    """Return True if something has bound the UDP port (i.e., daemon already running)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((host, port))
        # If bind succeeds, nothing else is bound — release it immediately.
        return False
    except OSError:
        # Binding failed: someone else (likely the daemon) has the port.
        return True
    finally:
        try: s.close()
        except: pass

def start() -> None:
    """Start audio_daemon.py IFF not already running; remember if we launched it."""
    global _proc, _started_by_us
    with _lock:
        if _port_in_use():
            _proc = None
            _started_by_us = False
            return

        # Spawn the daemon in the same Python interpreter
        py = sys.executable or "python3"
        daemon_path = os.path.join(os.path.dirname(__file__), "audio_daemon.py")
        _proc = subprocess.Popen([py, "-u", daemon_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _started_by_us = True

    # Give it a moment to bind; if it didn’t, treat as not started
    for _ in range(20):  # ~2s
        time.sleep(0.1)
        if _port_in_use():
            return
        # If the child died immediately, stop tracking
        with _lock:
            if _proc is not None and _proc.poll() is not None:
                _proc = None
                _started_by_us = False
                return

def is_running() -> bool:
    """True if port is bound or the child process we spawned is alive."""
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return True
    return _port_in_use()

def stop() -> None:
    """Stop the daemon only if we started it."""
    global _proc, _started_by_us
    with _lock:
        if not _started_by_us:
            return
        if _proc is None:
            _started_by_us = False
            return

        try:
            if _proc.poll() is None:
                if os.name == "nt":
                    _proc.terminate()
                else:
                    _proc.send_signal(signal.SIGTERM)
                try:
                    _proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    _proc.kill()
            _proc = None
        finally:
            _started_by_us = False

def ensure_started() -> None:
    """Idempotent helper—safe to call multiple times."""
    if not is_running():
        start()

# Make sure we clean up if WE launched it
atexit.register(stop)
