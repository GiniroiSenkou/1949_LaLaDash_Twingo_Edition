# settings_audio.py
from __future__ import annotations

import re
import shutil
import subprocess
import weakref
from dataclasses import dataclass
from typing import Dict, Optional, List

try:
    import vlc
except Exception:
    vlc = None


# ─────────────────────────────────────────────
# 1) App VLC registry (embedded players)
# ─────────────────────────────────────────────
@dataclass
class VlcEntry:
    name: str
    player_ref: "weakref.ReferenceType"
    group: str = "Misc"
    volume_hint: int = 0


class VlcRegistry:
    def __init__(self):
        self._items: Dict[str, VlcEntry] = {}

    def register(self, name: str, player, *, group: str = "Misc") -> None:
        if not name or player is None:
            return
        self._items[name] = VlcEntry(
            name=name,
            player_ref=weakref.ref(player),
            group=group,
            volume_hint=self._safe_get_volume(player) or 0,
        )

    def unregister(self, name: str) -> None:
        self._items.pop(name, None)

    def get_player(self, name: str):
        e = self._items.get(name)
        return e.player_ref() if e else None

    def list_alive(self) -> Dict[str, VlcEntry]:
        alive: Dict[str, VlcEntry] = {}
        dead = []
        for k, e in self._items.items():
            p = e.player_ref()
            if p is None:
                dead.append(k)
            else:
                e.volume_hint = self._safe_get_volume(p) or e.volume_hint
                alive[k] = e
        for k in dead:
            self._items.pop(k, None)
        return alive

    def set_volume(self, name: str, vol: int) -> bool:
        p = self.get_player(name)
        if p is None:
            self.unregister(name)
            return False
        vol = int(max(0, min(200, vol)))
        try:
            p.audio_set_volume(vol)
            if name in self._items:
                self._items[name].volume_hint = vol
            return True
        except Exception:
            return False


    @staticmethod
    def _safe_get_volume(player) -> Optional[int]:
        try:
            v = player.audio_get_volume()
            return int(v) if v is not None else None
        except Exception:
            return None


VLC_REGISTRY = VlcRegistry()


# ─────────────────────────────────────────────
# 2) System VLC streams (PulseAudio / PipeWire)
# ─────────────────────────────────────────────
@dataclass
class SystemStream:
    stream_id: str          # pactl sink-input id
    name: str               # application.name / media.name
    app: str                # application.name (e.g. "VLC media player")
    vol_percent: int        # best-effort
    mute: bool = False


class SystemAudio:
    """
    Lists + controls system audio streams via pactl.
    Works on PulseAudio OR PipeWire-Pulse (PipeWire compatibility layer).
    """

    @staticmethod
    def _run(cmd: List[str]) -> str:
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
            return out
        except Exception:
            return ""

    # Resolved once. This used to spawn `bash -lc "command -v pactl"` on every
    # call — a *login* shell, so it sourced the user's profile each time — and
    # every list/set call below starts with one. On the Pi that was tens of ms
    # of main-thread stall per audio-mixer read.
    _pactl_path: Optional[str] = None
    _pactl_probed: bool = False

    @staticmethod
    def has_pactl() -> bool:
        if not SystemAudio._pactl_probed:
            SystemAudio._pactl_path = shutil.which("pactl")
            SystemAudio._pactl_probed = True
        return bool(SystemAudio._pactl_path)

    @staticmethod
    def list_vlc_streams() -> List[SystemStream]:
        if not SystemAudio.has_pactl():
            return []

        txt = SystemAudio._run(["pactl", "list", "sink-inputs"])
        if not txt.strip():
            return []

        blocks = re.split(r"\n(?=Sink Input #\d+)", txt)
        streams: List[SystemStream] = []

        for b in blocks:
            m_id = re.search(r"Sink Input #(\d+)", b)
            if not m_id:
                continue
            sid = m_id.group(1)

            # application.name might be "VLC media player"
            app = SystemAudio._find_prop(b, "application.name") or ""
            media = SystemAudio._find_prop(b, "media.name") or ""
            binary = SystemAudio._find_prop(b, "application.process.binary") or ""

            # filter: anything VLC-ish
            if "vlc" not in (app + " " + media + " " + binary).lower():
                continue

            vol = SystemAudio._parse_volume_percent(b) or 100
            mute = ("Mute: yes" in b)

            title = media.strip() or app.strip() or f"VLC stream {sid}"
            streams.append(SystemStream(stream_id=sid, name=title, app=app or "VLC", vol_percent=vol, mute=mute))

        return streams

    @staticmethod
    def set_stream_volume(stream_id: str, vol_percent: int) -> bool:
        if not SystemAudio.has_pactl():
            return False
        vol_percent = int(max(0, min(150, vol_percent)))  # allow some boost
        out = SystemAudio._run(["pactl", "set-sink-input-volume", str(stream_id), f"{vol_percent}%"])
        return True if out is not None else False


    @staticmethod
    def _find_prop(block: str, key: str) -> Optional[str]:
        # matches: application.name = "VLC media player"
        m = re.search(rf'{re.escape(key)}\s*=\s*"([^"]+)"', block)
        return m.group(1) if m else None

    @staticmethod
    def _parse_volume_percent(block: str) -> Optional[int]:
        # Example line:
        # Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB
        m = re.search(r"/\s*(\d+)%\s*/", block)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return None
        return None


# ─────────────────────────────────────────────
# 3) “Glass bar” row widget (nice slider)
# ─────────────────────────────────────────────
