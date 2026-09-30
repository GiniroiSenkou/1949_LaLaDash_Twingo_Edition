# LaLaDash/audio_daemon.py
from __future__ import annotations
import json, socket, threading, time, signal, sys
from pathlib import Path

# Use your existing sophisticated engine (or port the internals here)
# If you prefer pygame/vlc, you can swap it; the interface stays the same.
try:
    from Modules.Modding.audio_engine import AudioEngine
except Exception as e:
    print("audio_daemon: failed to import audio_engine:", e)
    sys.exit(1)

HOST = "127.0.0.1"
PORT = 34211
SETTINGS_PATH = Path("Data/settings.ini")

class EngineServer:
    def __init__(self, host=HOST, port=PORT):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(self.addr)
        self.sock.settimeout(0.25)

        self.engine = AudioEngine(settings_path=SETTINGS_PATH, on_log=self._log)
        self._rpm = 0
        self._gear = None
        self._last_update = 0.0
        self._quit = False

        self._rx = threading.Thread(target=self._rx_loop, daemon=True)
        self._tx = threading.Thread(target=self._tx_loop, daemon=True)

    def _log(self, msg: str):
        print("[engine]", msg)

    def start(self):
        self._rx.start()
        self._tx.start()
        print(f"audio_daemon: listening on udp://{self.addr[0]}:{self.addr[1]}")
        while not self._quit:
            time.sleep(0.2)

    def stop(self):
        self._quit = True
        try:
            self.sock.close()
        except Exception:
            pass
        try:
            self.engine.stop_all()
            self.engine.close()
        except Exception:
            pass

    def _rx_loop(self):
        # receives JSON dicts: {"rpm": int, "gear": int|null}
        while not self._quit:
            try:
                data, _ = self.sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
                rpm = int(msg.get("rpm", 0))
                gear = msg.get("gear", None)
                if gear is not None:
                    gear = int(gear)
                self._rpm = max(0, rpm)
                self._gear = gear
                self._last_update = time.time()
            except Exception as e:
                print("audio_daemon: bad packet:", e)

    def _tx_loop(self):
        # engine tick @ ~120Hz; if no updates for 1s, idle fade
        tick = 1.0 / 120.0
        idle_timeout = 1.0
        while not self._quit:
            now = time.time()
            if now - self._last_update > idle_timeout:
                rpm, gear = 0, None
            else:
                rpm, gear = self._rpm, self._gear
            try:
                self.engine.process_sample(rpm, gear)
            except Exception as e:
                print("audio_daemon: engine error:", e)
            time.sleep(tick)

def _main():
    srv = EngineServer()

    def _sig(*_):
        try:
            srv.stop()                      # ← this now calls engine.close()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        srv.start()
    finally:
        # safety net (also runs on unhandled exception)
        srv.stop()                          # ← idempotent; engine.close() safe to call again


if __name__ == "__main__":
    _main()
