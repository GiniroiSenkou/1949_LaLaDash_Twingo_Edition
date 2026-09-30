#!/usr/bin/env python3
"""Send one command to a running LaLaDash debug probe and print the reply.

The app must have been started with LALADASH_DEBUG=1 (see
Modules/Runtime/debug_probe.py). Override the socket location with
LALADASH_DEBUG_SOCK if XDG_RUNTIME_DIR differs from the app's.

    python3 Tools/probe_client.py ping
    python3 Tools/probe_client.py grab /tmp/shot.png
    python3 Tools/probe_client.py page 4
    python3 Tools/probe_client.py stats
"""
import os
import socket
import sys


def socket_path() -> str:
    override = os.environ.get("LALADASH_DEBUG_SOCK")
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime, "laladash-debug.sock")


def main(argv) -> int:
    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path = socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(20)
    try:
        sock.connect(path)
    except OSError as exc:
        print(f"cannot connect to {path}: {exc}", file=sys.stderr)
        return 1
    try:
        sock.sendall((" ".join(argv[1:]) + "\n").encode("utf-8"))
        chunks = []
        while True:
            block = sock.recv(65536)
            if not block:
                break
            chunks.append(block)
    finally:
        sock.close()
    reply = b"".join(chunks).decode("utf-8", "replace")
    sys.stdout.write(reply if reply.endswith("\n") else reply + "\n")
    return 1 if reply.lstrip().startswith("ERR") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
