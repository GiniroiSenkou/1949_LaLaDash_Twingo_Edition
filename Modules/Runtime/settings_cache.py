from __future__ import annotations

import configparser
import io
import os
import shutil
import time
from dataclasses import dataclass
from threading import RLock
from typing import Optional


def _default_settings_path() -> str:
    return os.path.join(os.getcwd(), "Data", "settings.ini")


@dataclass
class _CacheEntry:
    parser: configparser.ConfigParser
    mtime_ns: int
    checked_at: float = 0.0


# How long a cache entry is trusted before its mtime is re-stat'ed. Every
# in-process write goes through save()/set_value(), which refresh the entry
# directly, so this only delays picking up an *external* edit of the INI.
# os.stat() on the dev PC's media mount measured 35 us — more than the whole
# rest of a read — and the hot paths do hundreds of reads a second.
_STAT_TTL_S = 0.5


_LOCK = RLock()
_CACHE: dict[str, _CacheEntry] = {}


def _normalize(path: Optional[str]) -> str:
    return os.path.abspath(path or _default_settings_path())


def _read_ini(cfg: configparser.ConfigParser, path: str) -> bool:
    """Read `path` into `cfg`. True only if the file existed *and* parsed.

    configparser.read() silently skips a missing file but raises on a
    corrupt one (MissingSectionHeaderError, DuplicateOptionError, ...); both
    cases come back as False here so callers can fall back to the .bak.
    """
    if not os.path.exists(path):
        return False
    try:
        return bool(cfg.read(path, encoding="utf-8"))
    except UnicodeDecodeError:
        try:
            return bool(cfg.read(path))
        except Exception as exc:
            print(f"[settings] could not parse {path}: {exc}", flush=True)
            return False
    except Exception as exc:
        print(f"[settings] could not parse {path}: {exc}", flush=True)
        return False


# Paths already recovered from their .bak this run — one log line each, not
# one per 500 ms re-stat while the file stays empty.
_RECOVERED: set[str] = set()


def _read_ini_lenient(path: str):
    """Parse `path` tolerating duplicate sections/options (last value wins).

    A single stray duplicate section — however it got there — otherwise makes
    the strict parse raise, which drops the whole file to defaults and looks
    like "nothing saves." Returns a parser with sections, or None."""
    try:
        cfg = configparser.ConfigParser(strict=False)
        cfg.read(path, encoding="utf-8")
        return cfg if cfg.sections() else None
    except Exception:
        return None


def _atomic_write(path: str, text: str) -> None:
    """tmp + fsync + os.replace. Keeps the previous file as .bak *only* if it
    still parses — a corrupt file (the one being healed) must not become the
    backup."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    prev = configparser.ConfigParser()
    if _read_ini(prev, path) and prev.sections():
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
    os.replace(tmp, path)


def _parser_from_disk(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    ok = _read_ini(cfg, path)
    if ok and cfg.sections():
        return cfg

    # Strict parse failed but the file has content (e.g. a duplicated section):
    # read it leniently so real settings are not lost, and heal the file so the
    # next strict read succeeds. This must come before the .bak fallback — the
    # live file's values are newer than the backup's.
    if os.path.exists(path) and os.path.getsize(path) > 0:
        lcfg = _read_ini_lenient(path)
        if lcfg is not None:
            if path not in _RECOVERED:
                _RECOVERED.add(path)
                print(f"[settings] {os.path.basename(path)} had a duplicate/"
                      f"malformed section — recovered {len(lcfg.sections())} "
                      f"sections leniently and rewrote it", flush=True)
            try:
                _atomic_write(path, _serialize(lcfg))
            except Exception:
                pass
            return lcfg

    # The INI is missing, empty or unparseable. The car's Pi browns out on its
    # USB supply, and a power cut during the old truncate-then-write save()
    # left a 0-byte settings.ini on 2026-09-1x; the app then rebuilt only the
    # three sections it writes at runtime and silently ran on defaults for
    # everything else ([OBD], [Calibration], [DashCal], ...). save() now keeps
    # the previous good file as .bak, so use it when it is better than what
    # is on disk, and copy it back so the repair survives the next boot even
    # if nothing else gets saved this run.
    bak = path + ".bak"
    bcfg = configparser.ConfigParser()
    if _read_ini(bcfg, bak) and len(bcfg.sections()) > len(cfg.sections()):
        if path not in _RECOVERED:
            _RECOVERED.add(path)
            print(
                f"[settings] {os.path.basename(path)} is "
                f"{'unreadable' if not ok else 'empty'} — recovered "
                f"{len(bcfg.sections())} sections from .bak",
                flush=True,
            )
        try:
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                shutil.copyfile(bak, path)
        except OSError:
            pass
        return bcfg
    return cfg


def _serialize(cfg: configparser.ConfigParser) -> str:
    buf = io.StringIO()
    cfg.write(buf)
    return buf.getvalue()


def _clone(cfg: configparser.ConfigParser) -> configparser.ConfigParser:
    copy = configparser.ConfigParser()
    payload = _serialize(cfg)
    if payload.strip():
        copy.read_string(payload)
    return copy


def _current_mtime_ns(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return -1


def _shared_parser(path: Optional[str], *, fresh: bool = False) -> configparser.ConfigParser:
    """The cached parser itself — **read-only**, never handed to a mutator.

    Scalar reads (`get`/`getboolean`/...) go through this instead of
    `load_copy()`. `load_copy()` deep-copies by serialising the whole parser to
    a string and re-parsing it, which measured **951 us per call** on the dev PC
    for an 8.4 KB INI. With `Telemetry.update()` re-reading driving flags at
    8-20 Hz and QML bindings reaching settings on every re-evaluation, that
    "cache" was costing well over a hundred full INI parses a second, all of
    them holding the GIL and stalling the Qt main thread. A scalar read cannot
    mutate the parser, so it can safely share it.
    """
    resolved = _normalize(path)
    now = time.monotonic()
    with _LOCK:
        entry = _CACHE.get(resolved)
        if entry is not None and not fresh and (now - entry.checked_at) < _STAT_TTL_S:
            return entry.parser
        mtime_ns = _current_mtime_ns(resolved)
        if fresh or entry is None or entry.mtime_ns != mtime_ns:
            parser = _parser_from_disk(resolved)
            entry = _CacheEntry(parser=parser, mtime_ns=mtime_ns, checked_at=now)
            _CACHE[resolved] = entry
        else:
            entry.checked_at = now
        return entry.parser


def shared(path: Optional[str] = None) -> configparser.ConfigParser:
    """Public alias for the read-only cached parser.

    Callers must treat the result as immutable — use `load_copy()` if you intend
    to `set()` anything on it.
    """
    return _shared_parser(path)


def load_copy(path: Optional[str] = None, *, fresh: bool = False) -> configparser.ConfigParser:
    with _LOCK:
        return _clone(_shared_parser(path, fresh=fresh))


def invalidate(path: Optional[str] = None) -> None:
    resolved = _normalize(path)
    with _LOCK:
        _CACHE.pop(resolved, None)


def save(cfg: configparser.ConfigParser, path: Optional[str] = None) -> bool:
    """Atomically replace the INI: tmp + fsync + os.replace, previous file
    kept as .bak (same shape as trips_store._write_file). A brownout at any
    point leaves either the old file, the new file, or no file plus a good
    .bak that _parser_from_disk() recovers — never a truncated INI."""
    resolved = _normalize(path)
    try:
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
    except Exception:
        pass

    # A save must never *drop* sections. Nothing in the app removes a section,
    # so a cfg with fewer sections than the file already holds is a stale or
    # corrupt read about to overwrite real settings — this is exactly how "all
    # my toggles reset on the next boot" happens (a set_value() built on a
    # momentarily-empty shared parser wipes every other section). A strict read
    # is used on purpose: a genuinely corrupt file parses as 0 sections here,
    # so the guard steps aside and lets the healed/new content replace it.
    on_disk = configparser.ConfigParser()
    if _read_ini(on_disk, resolved) and on_disk.sections():
        disk_n = len(on_disk.sections())
        if len(cfg.sections()) < disk_n and disk_n >= 3:
            print(
                f"[settings] refusing to overwrite {os.path.basename(resolved)} "
                f"({disk_n} sections) with {len(cfg.sections())} section(s) — "
                f"would drop settings",
                flush=True,
            )
            return False

    tmp = resolved + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            cfg.write(f)
            f.flush()
            os.fsync(f.fileno())
        # Only a file that still parses is worth keeping — a corrupt one must
        # not push a good .bak out.
        prev = configparser.ConfigParser()
        if _read_ini(prev, resolved) and prev.sections():
            try:
                os.replace(resolved, resolved + ".bak")
            except OSError:
                pass
        os.replace(tmp, resolved)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    with _LOCK:
        _CACHE[resolved] = _CacheEntry(
            parser=_clone(cfg),
            mtime_ns=_current_mtime_ns(resolved),
            checked_at=time.monotonic(),
        )
    return True


def get(section: str, option: str, fallback=None, *, path: Optional[str] = None):
    cfg = _shared_parser(path)
    try:
        return cfg.get(section, option, fallback=fallback)
    except Exception:
        return fallback


def getboolean(section: str, option: str, fallback: bool = False, *, path: Optional[str] = None) -> bool:
    cfg = _shared_parser(path)
    try:
        return cfg.getboolean(section, option, fallback=fallback)
    except Exception:
        return fallback


def getint(section: str, option: str, fallback: int = 0, *, path: Optional[str] = None) -> int:
    cfg = _shared_parser(path)
    try:
        return cfg.getint(section, option, fallback=fallback)
    except Exception:
        return fallback


def getfloat(section: str, option: str, fallback: float = 0.0, *, path: Optional[str] = None) -> float:
    cfg = _shared_parser(path)
    try:
        return cfg.getfloat(section, option, fallback=fallback)
    except Exception:
        return fallback


def set_value(section: str, option: str, value, *, path: Optional[str] = None) -> bool:
    cfg = load_copy(path)
    if not cfg.has_section(section):
        cfg.add_section(section)
    cfg.set(section, option, str(value))
    return save(cfg, path)
