from __future__ import annotations

import os
import random


def track_key(track: dict | None) -> str:
    if not isinstance(track, dict):
        return ""
    for key in ("path", "file", "source_path", "title"):
        raw = str(track.get(key, "") or "").strip()
        if raw:
            if os.path.isabs(raw):
                return os.path.normpath(raw)
            return os.path.normpath(raw).replace("\\", "/")
    return ""


def dedupe_tracks(tracks: list[dict] | None) -> list[dict]:
    seen: set[str] = set()
    deduped: list[dict] = []
    for track in list(tracks or []):
        key = track_key(track)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(track)
    return deduped


def playlist_signature(tracks: list[dict] | None) -> tuple[str, ...]:
    return tuple(track_key(track) for track in dedupe_tracks(tracks))


def build_queue(
    tracks: list[dict] | None,
    *,
    shuffle_enabled: bool,
    current_track_key: str = "",
    rng=random,
) -> tuple[list[int], int]:
    playlist = list(tracks or [])
    queue = list(range(len(playlist)))
    if shuffle_enabled and len(queue) > 1:
        rng.shuffle(queue)

    queue_pos = 0
    current_key = str(current_track_key or "").strip()
    if current_key:
        current_index = next(
            (index for index, track in enumerate(playlist) if track_key(track) == current_key),
            None,
        )
        if current_index is not None and current_index in queue:
            queue_pos = queue.index(current_index)
    return queue, queue_pos
