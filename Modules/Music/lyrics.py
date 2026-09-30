"""Lyrics lookup + LRC parsing (local sources only).

Pure-Python module (no Qt dependencies). Resolution order on a track:
  1. <LYRICS_FOLDER>/<Artist> - <Title>.lrc
  2. <LYRICS_FOLDER>/<Title>.lrc
  3. Sidecar .lrc next to the audio file
  4. Embedded USLT (MP3) / LYRICS vorbis comment (FLAC, OGG)

The app ships with its lyrics in LYRICS_FOLDER, so lookup is entirely local —
there is no network fetch. Lyrics are read whenever a matching file/tag exists.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from preferences import LYRICS_FOLDER

try:
    from mutagen import File as _MutagenFile
except Exception:
    _MutagenFile = None


_LRC_TIME_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")
_LRC_META_RE = re.compile(r"^\[[a-zA-Z]+:[^\]]*\]\s*$")
_SANITIZE_RE = re.compile(r'[\\/:*?"<>|]+')


@dataclass
class LyricLine:
    time_ms: Optional[int]
    text: str
    estimated: bool = False   # time carried from the previous stamp, not its own


@dataclass
class LyricsResult:
    lines: List[LyricLine] = field(default_factory=list)
    synced: bool = False
    partial: bool = False   # synced, but some lines had no stamp of their own
    source: str = ""   # "lyrics_folder" | "sidecar" | "tag"


# ── filename helpers ───────────────────────────────────────────────────────

def _sanitize(name: str) -> str:
    name = _SANITIZE_RE.sub(" ", str(name or "")).strip()
    return re.sub(r"\s+", " ", name)


def _candidate_filenames(title: str, artist: str) -> List[str]:
    t = _sanitize(title)
    a = _sanitize(artist)
    names: List[str] = []
    if a and t:
        names.append(f"{a} - {t}.lrc")
    if t:
        names.append(f"{t}.lrc")
    return names


# ── LRC parsing ────────────────────────────────────────────────────────────

def parse_lrc(text: str) -> List[LyricLine]:
    """Parse LRC content into lines in **file order**. Tolerant of multi-stamp
    lines, blank lines, and metadata tags like [ar:Artist] / [ti:Title]
    (skipped).

    A line with no timestamp keeps `time_ms=None` here. If the file has *any*
    stamps at all it is treated as synced and those untimed lines are given a
    carried time (the previous stamp, or 0 before the first) and marked
    `estimated=True`, so a *partially*-stamped file still scrolls sensibly
    instead of dumping its plain lines at the bottom where they never move.
    A file with no stamps at all is left untimed (unsynced) in file order."""
    lines: List[LyricLine] = []
    if not text:
        return lines
    for raw in text.splitlines():
        s = raw.rstrip()
        if not s:
            continue
        if _LRC_META_RE.match(s):
            continue
        stamps: List[int] = []
        m = _LRC_TIME_RE.match(s)
        while m:
            mm = int(m.group(1)); ss = int(m.group(2))
            cs = m.group(3) or "0"
            # normalize to milliseconds whether centi or milli
            ms = int(cs.ljust(3, "0")[:3])
            stamps.append(mm * 60_000 + ss * 1_000 + ms)
            s = s[m.end():]
            m = _LRC_TIME_RE.match(s)
        text_part = s.strip()
        if stamps:
            for t in stamps:
                lines.append(LyricLine(time_ms=t, text=text_part))
        else:
            # No timestamp — keep as a plain line, in place (not shoved to the
            # end). text_part may be "" for a deliberate blank between verses.
            lines.append(LyricLine(time_ms=None, text=text_part))

    has_stamp = any(l.time_ms is not None for l in lines)
    if not has_stamp:
        # Fully unsynced: keep file order, no carried times.
        return [l for l in lines if l.text]

    # Partially/fully synced: multi-stamp lines can be out of file order, so
    # a stable sort on the real stamps puts them right; untimed lines carry
    # the previous stamp so they land with the line they follow. Do the sort
    # on a copy that pins each untimed line just after its predecessor.
    ordered: List[LyricLine] = []
    last = 0
    for idx, l in enumerate(lines):
        if l.time_ms is not None:
            last = l.time_ms
            ordered.append((l.time_ms, idx, l))
        else:
            if not l.text:
                continue   # drop blank untimed separators once synced
            ordered.append((last, idx, LyricLine(time_ms=last, text=l.text, estimated=True)))
    ordered.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in ordered]


def _is_synced(lines: Iterable[LyricLine]) -> bool:
    return any(l.time_ms is not None for l in lines)


def _is_partial(lines: Iterable[LyricLine]) -> bool:
    return any(getattr(l, "estimated", False) for l in lines)


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


# ── candidate discovery (folder + sidecar) ─────────────────────────────────

def _candidates_in_lyrics_folder(title: str, artist: str) -> List[str]:
    out: List[str] = []
    for fname in _candidate_filenames(title, artist):
        out.append(os.path.join(LYRICS_FOLDER, fname))
    return out


def _sidecar_candidates(audio_path: str, title: str) -> List[str]:
    if not audio_path:
        return []
    audio_dir = os.path.dirname(audio_path)
    stem, _ = os.path.splitext(os.path.basename(audio_path))
    stem_variants = {stem}
    t = _sanitize(title)
    if t:
        stem_variants.add(t)
    out: List[str] = []
    for s in stem_variants:
        out.append(os.path.join(audio_dir, f"{s}.lrc"))
    out.append(os.path.join(audio_dir, "lyrics", f"{stem}.lrc"))
    return out


def _first_existing(paths: Iterable[str]) -> Optional[str]:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


# ── embedded tag fallback ──────────────────────────────────────────────────

def _from_embedded_tag(audio_path: str) -> Optional[str]:
    if not audio_path or _MutagenFile is None:
        return None
    try:
        mf = _MutagenFile(audio_path)
    except Exception:
        return None
    if mf is None:
        return None
    # ID3 USLT frames (MP3)
    try:
        tags = getattr(mf, "tags", None)
        if tags is not None:
            for key in tags.keys() if hasattr(tags, "keys") else []:
                if str(key).startswith("USLT"):
                    frame = tags[key]
                    raw = getattr(frame, "text", None)
                    if isinstance(raw, str) and raw.strip():
                        return raw
                    if isinstance(raw, list) and raw:
                        first = raw[0]
                        if isinstance(first, str) and first.strip():
                            return first
    except Exception:
        pass
    # Vorbis comments (FLAC/OGG)
    try:
        for key in ("lyrics", "LYRICS", "Lyrics"):
            val = mf.get(key) if hasattr(mf, "get") else None
            if val:
                if isinstance(val, list):
                    val = val[0] if val else None
                if isinstance(val, str) and val.strip():
                    return val
    except Exception:
        pass
    return None


# ── public API ─────────────────────────────────────────────────────────────

def load_lyrics_for_audio(audio_path: str,
                          title: str = "",
                          artist: str = "") -> Optional[LyricsResult]:
    """Synchronous local lookup. Returns None if no local source matched.

    Order: Lyrics folder (artist-title, then title) → sidecar → embedded tag.
    """
    folder_hit = _first_existing(_candidates_in_lyrics_folder(title, artist))
    if folder_hit:
        text = _read_text(folder_hit) or ""
        lines = parse_lrc(text)
        if lines:
            return LyricsResult(lines=lines, synced=_is_synced(lines), partial=_is_partial(lines),
                                source="lyrics_folder")

    sidecar_hit = _first_existing(_sidecar_candidates(audio_path, title))
    if sidecar_hit:
        text = _read_text(sidecar_hit) or ""
        lines = parse_lrc(text)
        if lines:
            return LyricsResult(lines=lines, synced=_is_synced(lines), partial=_is_partial(lines),
                                source="sidecar")

    tag_text = _from_embedded_tag(audio_path)
    if tag_text:
        lines = parse_lrc(tag_text)
        if lines:
            return LyricsResult(lines=lines, synced=_is_synced(lines), partial=_is_partial(lines),
                                source="tag")

    return None
