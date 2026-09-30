from __future__ import annotations

import os
from pathlib import Path
from threading import RLock
from typing import Iterable, Optional

# Canonical audio-extension list. Kept here as a fallback default so this module
# has no import cycle risk with preferences; callers pass the real one through.
_DEFAULT_AUDIO_EXTS = (
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus",
    ".wma", ".aiff", ".aif", ".alac", ".mp4",
)


_LOCK = RLock()
_CACHE: dict[tuple[str, str], object] = {}


def _audio_sig(exts: Iterable[str]) -> str:
    return "|".join(sorted(str(x).lower() for x in exts))


def _dir_mtime_ns(path: Path) -> int:
    """mtime of the top folder AND its immediate sub-folders.

    A recursive scan cached on the top folder's mtime alone would go stale
    whenever a file changed inside an album sub-directory without touching the
    root. Folding the children in is cheap (one stat each) and covers the case
    that actually happens: dropping a new album folder in, or re-casing files
    inside one.
    """
    try:
        sig = path.stat().st_mtime_ns
    except OSError:
        return -1
    try:
        for child in os.scandir(path):
            if child.is_dir(follow_symlinks=False):
                try:
                    sig ^= child.stat().st_mtime_ns
                except OSError:
                    pass
    except OSError:
        pass
    return sig


def invalidate(prefix: Optional[str] = None) -> None:
    with _LOCK:
        if not prefix:
            _CACHE.clear()
            return
        norm = os.path.abspath(prefix)
        doomed = [k for k in _CACHE if k[0].startswith(norm)]
        for key in doomed:
            _CACHE.pop(key, None)


def _walk_audio(path: Path, exts_lower: tuple[str, ...]) -> list[str]:
    """Every audio file under `path`, as a "/"-joined relative path, sorted.

    Recursive: a track filed under `Artist/Album/track.mp3` was invisible to
    the old top-level `os.listdir`, so it could sit tagged in music.json and
    never resolve. The relative path is exactly what
    `os.path.join(MUSIC_FOLDER, title)` expects on the resolve side.
    """
    out: list[str] = []
    if not path.exists():
        return out
    try:
        for root, _dirs, files in os.walk(path):
            rel_root = os.path.relpath(root, path)
            for fn in files:
                if fn.lower().endswith(exts_lower):
                    rel = fn if rel_root == "." else os.path.join(rel_root, fn)
                    out.append(rel.replace(os.sep, "/"))
    except Exception:
        pass
    out.sort(key=str.lower)
    return out


def music_filenames(base_dir: str, *, exts: Iterable[str] = _DEFAULT_AUDIO_EXTS) -> list[str]:
    """Relative paths of every audio file under `base_dir`, recursively.

    Was top-level-only and .mp3/.wav/.flac-only. Both limits meant real files
    were treated as "not in the library": nested tracks never got auto-added,
    and .m4a/.ogg/.opus were ignored entirely.
    """
    path = Path(base_dir).resolve()
    exts_lower = tuple(str(x).lower() for x in exts)
    key = (str(path), f"music:{_audio_sig(exts)}")
    mtime_ns = _dir_mtime_ns(path)
    with _LOCK:
        cached = _CACHE.get(key)
        if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == mtime_ns:
            return list(cached[1])
    out = _walk_audio(path, exts_lower)
    with _LOCK:
        _CACHE[key] = (mtime_ns, list(out))
    return out


def music_index(base_dir: str, *, exts: Iterable[str] = _DEFAULT_AUDIO_EXTS) -> dict[str, str]:
    """Case-insensitive resolver: `{key: real relative path}`.

    Keys are the case-folded relative path AND the case-folded bare filename, so
    a music.json title that has drifted in case (`ACdc - TNT.MP3` on disk as
    `ACDC - TNT.MP3`) or lost its sub-folder prefix still finds its file. The
    bare-name keys only win when unambiguous — a filename that occurs in more
    than one folder is left out rather than resolved to an arbitrary one.
    """
    path = Path(base_dir).resolve()
    key = (str(path), f"music_idx:{_audio_sig(exts)}")
    mtime_ns = _dir_mtime_ns(path)
    with _LOCK:
        cached = _CACHE.get(key)
        if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == mtime_ns:
            data = cached[1]
            if isinstance(data, dict):
                return dict(data)

    rels = _walk_audio(path, tuple(str(x).lower() for x in exts))
    index: dict[str, str] = {}
    basename_hits: dict[str, list[str]] = {}
    for rel in rels:
        index[rel.casefold()] = rel
        basename_hits.setdefault(os.path.basename(rel).casefold(), []).append(rel)
    for base_cf, hits in basename_hits.items():
        if len(hits) == 1 and base_cf not in index:
            index[base_cf] = hits[0]

    with _LOCK:
        _CACHE[key] = (mtime_ns, dict(index))
    return dict(index)


