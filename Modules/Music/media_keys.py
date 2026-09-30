"""Steering-wheel and head-unit buttons that MPRIS never sees.

`mpris-proxy` maps the AVRCP transport commands — play, pause, next, previous,
stop — onto our MPRIS object, and those already work. Everything else the car
sends is dropped on the floor: on the reference car (Renault Twingo III) that is the stalk's **Mode/OK**
button and the two telephone keys (answer and hang-up), which is why pressing
them does nothing today.

Those buttons do reach the Pi, just not over MPRIS. BlueZ turns AVRCP
passthrough operations and any HID keys from a paired device into a kernel
input device on `BUS_BLUETOOTH`, so they arrive as ordinary `EV_KEY` events on
`/dev/input/event*`. This module reads those devices directly and hands the
key codes to a callback.

Deliberately no dependency: `evdev` is not in requirements.txt and PEP 668
blocks installing it on the dev PC, so the 24-byte `struct input_event` is
unpacked by hand. Reading an evdev node does not consume the event, so this
never steals input from the touchscreen or from anything else listening.

Which code each button actually sends is car-specific and cannot be looked up,
so `last_key` records every unmapped press for the Settings page to display —
press the button, read the code, bind it. Defaults cover the codes BlueZ
normally emits for these functions.
"""

from __future__ import annotations

import glob
import os
import select
import struct
import threading
from typing import Callable, Dict, List, Optional

# struct input_event: { struct timeval time; __u16 type; __u16 code; __s32 value; }
# 24 bytes on 64-bit, 16 on 32-bit ARM — Debian on the Pi 4 can be either.
_EVENT_FORMAT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FORMAT)

EV_KEY = 0x01
KEY_DOWN = 1          # value 2 is auto-repeat, which we ignore
BUS_BLUETOOTH = 0x0005

# Names for the codes worth showing to a human. Anything absent is reported as
# its bare number, which is still enough to bind.
KEY_NAMES: Dict[int, str] = {
    148: "KEY_PROG1",
    158: "KEY_BACK",
    163: "KEY_NEXTSONG",
    164: "KEY_PLAYPAUSE",
    165: "KEY_PREVIOUSSONG",
    166: "KEY_STOPCD",
    169: "KEY_PHONE",
    171: "KEY_CONFIG",
    174: "KEY_EXIT",
    200: "KEY_PLAYCD",
    201: "KEY_PAUSECD",
    213: "KEY_SOUND",
    218: "KEY_CONNECT",
    223: "KEY_CANCEL",
    226: "KEY_MEDIA",
    231: "KEY_SEND",
    232: "KEY_REPLY",
    28:  "KEY_ENTER",
    0x160: "KEY_OK",
    0x161: "KEY_SELECT",
    0x175: "KEY_MODE",
    0x246: "KEY_VOICECOMMAND",
}

# The actions a button can be bound to. The bridge turns these into calls.
ACTIONS = (
    "",                 # unbound
    "mode_ok",
    "answer",
    "hangup",
    "play_pause",
    "next_track",
    "prev_track",
    "next_page",
    "prev_page",
    "toggle_side_panel",
    "mute_music",
)

# Defaults. Media transport codes are left unbound on purpose: mpris-proxy
# already delivers those through MPRIS, and binding them here would act twice
# on one press — a single "next" would skip two tracks.
DEFAULT_BINDINGS: Dict[int, str] = {
    0x160: "mode_ok",       # KEY_OK
    0x161: "mode_ok",       # KEY_SELECT
    0x175: "mode_ok",       # KEY_MODE
    28:    "mode_ok",       # KEY_ENTER
    226:   "mode_ok",       # KEY_MEDIA
    169:   "answer",        # KEY_PHONE
    231:   "answer",        # KEY_SEND
    218:   "answer",        # KEY_CONNECT
    223:   "hangup",        # KEY_CANCEL
    174:   "hangup",        # KEY_EXIT
    158:   "hangup",        # KEY_BACK
}


def key_name(code: int) -> str:
    return KEY_NAMES.get(int(code), f"KEY_{int(code)}")


def _device_bustype(event_name: str) -> int:
    """Bus the device sits on, from sysfs. 0 when it cannot be read."""
    path = f"/sys/class/input/{event_name}/device/id/bustype"
    try:
        with open(path) as f:
            return int(f.read().strip(), 16)
    except Exception:
        return 0


def _device_label(event_name: str) -> str:
    try:
        with open(f"/sys/class/input/{event_name}/device/name") as f:
            return f.read().strip()
    except Exception:
        return event_name


class MediaKeyListener:
    """Reads Bluetooth-sourced key presses on a daemon thread."""

    # Wake-ups while nothing is attached. The car's buttons only exist once it
    # is connected, and a device node appearing is not something select() can
    # be told about without inotify, so this is the rescan cadence. Long enough
    # to be invisible on a Pi 4B, short enough that a button works within a few
    # seconds of the head unit connecting.
    IDLE_RESCAN_S = 4.0
    ACTIVE_RESCAN_S = 30.0

    def __init__(self, on_key: Callable[[int, str], None]) -> None:
        self._on_key = on_key
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._fds: Dict[int, str] = {}       # fd -> device label
        self._paths: Dict[str, int] = {}     # /dev/input/eventN -> fd
        self.last_key: int = 0
        self.last_key_name: str = ""
        self.last_device: str = ""

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        if not glob.glob("/dev/input/event*"):
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="MediaKeyListener"
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None
        self._close_all()

    @property
    def device_count(self) -> int:
        return len(self._fds)

    @property
    def device_names(self) -> List[str]:
        return sorted(set(self._fds.values()))

    # ── internals ─────────────────────────────────────────────────────────
    def _close_all(self) -> None:
        for fd in list(self._fds):
            try:
                os.close(fd)
            except Exception:
                pass
        self._fds.clear()
        self._paths.clear()

    def _rescan(self) -> None:
        """Open every Bluetooth-bus input device we can read, drop the rest.

        Filtering on the bus rather than the device name is what keeps the
        touchscreen and any USB keyboard out of this: BlueZ is the only thing
        that creates BUS_BLUETOOTH input nodes.
        """
        found = set()
        for path in sorted(glob.glob("/dev/input/event*")):
            name = os.path.basename(path)
            if _device_bustype(name) != BUS_BLUETOOTH:
                continue
            found.add(path)
            if path in self._paths:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except Exception:
                # No permission (the user is usually not in `input`) or the
                # device vanished between glob and open. Neither is fatal.
                continue
            self._fds[fd] = _device_label(name)
            self._paths[path] = fd

        for path in list(self._paths):
            if path not in found:
                fd = self._paths.pop(path)
                self._fds.pop(fd, None)
                try:
                    os.close(fd)
                except Exception:
                    pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._rescan()
            fds = list(self._fds)
            timeout = self.ACTIVE_RESCAN_S if fds else self.IDLE_RESCAN_S
            if not fds:
                self._stop.wait(timeout)
                continue
            try:
                ready, _, bad = select.select(fds, [], fds, timeout)
            except Exception:
                self._close_all()
                self._stop.wait(1.0)
                continue

            for fd in bad:
                self._drop_fd(fd)

            for fd in ready:
                self._drain(fd)
        self._close_all()

    def _drop_fd(self, fd: int) -> None:
        self._fds.pop(fd, None)
        for path, other in list(self._paths.items()):
            if other == fd:
                self._paths.pop(path, None)
        try:
            os.close(fd)
        except Exception:
            pass

    def _drain(self, fd: int) -> None:
        label = self._fds.get(fd, "")
        while True:
            try:
                data = os.read(fd, _EVENT_SIZE * 16)
            except BlockingIOError:
                return
            except Exception:
                self._drop_fd(fd)
                return
            if not data:
                return
            for offset in range(0, len(data) - _EVENT_SIZE + 1, _EVENT_SIZE):
                _, _, etype, code, value = struct.unpack_from(
                    _EVENT_FORMAT, data, offset
                )
                if etype != EV_KEY or value != KEY_DOWN:
                    continue
                self.last_key = int(code)
                self.last_key_name = key_name(code)
                self.last_device = label
                try:
                    self._on_key(int(code), label)
                except Exception:
                    pass
            if len(data) < _EVENT_SIZE * 16:
                return
