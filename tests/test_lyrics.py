from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from Modules.Music import lyrics


class ParseLrcTests(unittest.TestCase):
    def test_single_stamp_lines_parsed_in_order(self):
        text = (
            "[00:01.00]First line\n"
            "[00:03.50]Second line\n"
            "[00:10.25]Third line\n"
        )
        lines = lyrics.parse_lrc(text)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0].time_ms, 1_000)
        self.assertEqual(lines[1].time_ms, 3_500)
        self.assertEqual(lines[2].time_ms, 10_250)
        self.assertEqual(lines[0].text, "First line")
        self.assertEqual(lines[2].text, "Third line")

    def test_multi_stamp_line_expands_into_multiple_entries(self):
        text = "[00:01.00][00:05.00]Refrain\n"
        lines = lyrics.parse_lrc(text)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].time_ms, 1_000)
        self.assertEqual(lines[1].time_ms, 5_000)
        self.assertTrue(all(l.text == "Refrain" for l in lines))

    def test_metadata_lines_are_skipped(self):
        text = (
            "[ar:Artist Name]\n"
            "[ti:Title]\n"
            "[00:00.00]Line\n"
        )
        lines = lyrics.parse_lrc(text)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].text, "Line")

    def test_plain_text_falls_back_to_untimed_lines(self):
        text = "Line A\nLine B\n"
        lines = lyrics.parse_lrc(text)
        self.assertEqual(len(lines), 2)
        self.assertIsNone(lines[0].time_ms)
        self.assertIsNone(lines[1].time_ms)

    def test_empty_input_returns_empty(self):
        self.assertEqual(lyrics.parse_lrc(""), [])
        self.assertEqual(lyrics.parse_lrc(None), [])

    def test_centiseconds_normalized_to_milliseconds(self):
        # LRC uses .xx (centiseconds); we promote to milliseconds.
        lines = lyrics.parse_lrc("[00:00.50]Half second\n")
        self.assertEqual(lines[0].time_ms, 500)


class LocalLookupTests(unittest.TestCase):
    def test_lyrics_folder_artist_title_takes_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(lyrics, "LYRICS_FOLDER", tmp):
                # Audio dir gets a sidecar too — folder must still win.
                audio_dir = os.path.join(tmp, "music")
                os.makedirs(audio_dir, exist_ok=True)
                audio_path = os.path.join(audio_dir, "song.mp3")
                open(audio_path, "w").close()
                with open(os.path.join(audio_dir, "song.lrc"), "w", encoding="utf-8") as f:
                    f.write("[00:00.00]Sidecar wins?\n")
                with open(os.path.join(tmp, "Artist - Title.lrc"), "w", encoding="utf-8") as f:
                    f.write("[00:00.00]Folder wins\n")

                result = lyrics.load_lyrics_for_audio(audio_path, title="Title", artist="Artist")
                self.assertIsNotNone(result)
                self.assertEqual(result.source, "lyrics_folder")
                self.assertEqual(result.lines[0].text, "Folder wins")

    def test_sidecar_used_when_folder_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(lyrics, "LYRICS_FOLDER", tmp):
                audio_dir = os.path.join(tmp, "music")
                os.makedirs(audio_dir, exist_ok=True)
                audio_path = os.path.join(audio_dir, "song.mp3")
                open(audio_path, "w").close()
                with open(os.path.join(audio_dir, "song.lrc"), "w", encoding="utf-8") as f:
                    f.write("[00:00.00]Sidecar text\n")

                result = lyrics.load_lyrics_for_audio(audio_path, title="Title", artist="Artist")
                self.assertIsNotNone(result)
                self.assertEqual(result.source, "sidecar")
                self.assertTrue(result.synced)

    def test_no_local_lyrics_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(lyrics, "LYRICS_FOLDER", tmp):
                result = lyrics.load_lyrics_for_audio(
                    os.path.join(tmp, "nope.mp3"), title="X", artist="Y"
                )
                self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
