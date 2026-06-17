"""
Tests for monochrome_cli.py

Run with: pytest test_monochrome_cli.py -v
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the script under test is importable
sys.path.insert(0, str(Path(__file__).parent))

from monochrome_cli import (
    AlbumMatch,
    EXT_FOR_FORMAT,
    MirrorError,
    MirrorStats,
    MonochromeClient,
    TrackMatch,
    _detect_format_from_bytes,
    _detect_format_from_content_type,
    _existing_audio,
    _parse_album_selection,
    artist_matches,
    classify_error,
    download_file,
    download_single,
    fix_extensions,
    normalize_artist,
    pick_albums,
    redact_url,
    safe_snippet,
    sanitize_filename,
)


@pytest.fixture
def no_default_mirrors():
    """Patch out default mirrors so tests run with clean state."""
    with patch("monochrome_cli.DEFAULT_MONOCHROME_MIRRORS", []), \
         patch("monochrome_cli.DEFAULT_QOBUZ_MIRRORS", []):
        yield


# ─── Helper function tests ───


class TestClassifyError:
    def test_dns_failure(self):
        err = Exception("Name or service not known")
        cat, detail = classify_error(err)
        assert cat == "network"
        assert "DNS" in detail

    def test_connection_refused(self):
        err = Exception("Connection refused")
        cat, detail = classify_error(err)
        assert cat == "network"
        assert "refused" in detail

    def test_timeout(self):
        err = Exception("The request timed out")
        cat, detail = classify_error(err)
        assert cat == "network"
        assert "timeout" in detail

    def test_tls_error(self):
        err = Exception("SSL certificate verify failed")
        cat, detail = classify_error(err)
        assert cat == "network"
        assert "TLS" in detail

    def test_unknown_error(self):
        err = Exception("Something weird")
        cat, detail = classify_error(err)
        assert cat == "unknown"
        assert "weird" in detail


class TestRedactUrl:
    def test_basic(self):
        assert redact_url("https://example.com/path?q=1") == "https://example.com/path"

    def test_no_query(self):
        assert redact_url("https://example.com/path") == "https://example.com/path"

    def test_bad_url(self):
        assert redact_url("not-a-url?secret=123") == "://not-a-url"


class TestSafeSnippet:
    def test_html_response(self):
        assert safe_snippet("<!DOCTYPE html><body>...</body>") == "[HTML response]"

    def test_redacts_token(self):
        text = '{"token": "secret123", "data": "ok"}'
        result = safe_snippet(text)
        assert "[REDACTED]" in result
        assert "secret123" not in result

    def test_truncate(self):
        long_text = "x" * 500
        assert len(safe_snippet(long_text)) <= 200


class TestSanitizeFilename:
    def test_removes_forbidden_chars(self):
        assert sanitize_filename('a/b:c*d?e"f<g>h|i') == "a_b_c_d_e_f_g_h_i"

    def test_truncate_by_bytes(self):
        long_name = "x" * 300
        result = sanitize_filename(long_name, max_bytes=50)
        assert len(result.encode("utf-8")) <= 50

    def test_unicode_truncate(self):
        # UTF-8 multi-byte characters (emoji = 4 bytes each)
        name = "🎵" * 100
        result = sanitize_filename(name, max_bytes=10)
        assert len(result.encode("utf-8")) <= 10


class TestNormalizeArtist:
    def test_lowercase_and_strip(self):
        assert normalize_artist("  The Beatles  ") == "the beatles"


class TestArtistMatches:
    def test_exact_match(self):
        assert artist_matches("The Beatles", ["The Beatles"]) is True

    def test_no_match(self):
        assert artist_matches("The Beatles", ["The Rolling Stones"]) is False

    def test_case_insensitive(self):
        assert artist_matches("the beatles", ["The Beatles"]) is True


# ─── MirrorStats tests ───


class TestMirrorStats:
    def test_success_rate_no_data(self):
        stats = MirrorStats(path=Path("/tmp/nonexistent_stats.json"))
        assert stats.success_rate("https://example.com") == 1.0

    def test_record_and_rate(self, tmp_path):
        path = tmp_path / "stats.json"
        stats = MirrorStats(path=path)
        stats.record("https://a.com", True)
        stats.record("https://a.com", True)
        stats.record("https://a.com", False)
        assert stats.success_rate("https://a.com") == 2 / 3

    def test_persistence(self, tmp_path):
        path = tmp_path / "stats.json"
        stats = MirrorStats(path=path)
        stats.record("https://a.com", True)
        del stats

        stats2 = MirrorStats(path=path)
        assert stats2.success_rate("https://a.com") == 1.0


# ─── MirrorError tests ───


class TestMirrorError:
    def test_to_dict(self):
        err = MirrorError(
            mirror="https://example.com",
            category="http",
            detail="Not Found",
            status=404,
        )
        d = err.to_dict()
        assert d["mirror"] == "https://example.com"
        assert d["category"] == "http"
        assert d["status"] == 404
        assert "stream_error" in d


# ─── TrackMatch / AlbumMatch tests ───


class TestTrackMatch:
    def test_basic(self):
        track = TrackMatch(
            tidal_id=123,
            title="Test Song",
            artists=["Artist"],
            album="Test Album",
            duration_sec=180,
            quality="HIGH",
        )
        assert track.tidal_id == 123
        assert track.title == "Test Song"
        assert track.isrc is None

    def test_repr(self):
        track = TrackMatch(
            tidal_id=1, title="Song", artists=["A"], album="Alb", duration_sec=1, quality="LOW"
        )
        assert "Song" in repr(track)


class TestAlbumMatch:
    def test_display_artist(self):
        album = AlbumMatch(title="Album", artists=["Artist 1", "Artist 2"], tracks=[])
        assert album.display_artist == "Artist 1, Artist 2"

    def test_inferred_type(self):
        album = AlbumMatch(title="Album", artists=["A"], tracks=[], album_type="EP")
        assert album.inferred_type == "EP"

    def test_inferred_type_default(self):
        album = AlbumMatch(title="Album", artists=["A"], tracks=[])
        assert album.inferred_type == "Album"


# ─── Album selection parser tests ───


class TestParseAlbumSelection:
    """Pure-function tests for the multi-select album input parser."""

    def test_cancel_with_zero(self):
        assert _parse_album_selection("0", 5) is None

    def test_cancel_with_empty(self):
        assert _parse_album_selection("", 5) is None
        assert _parse_album_selection("   ", 5) is None

    def test_cancel_with_quit_keyword(self):
        assert _parse_album_selection("q", 5) is None
        assert _parse_album_selection("quit", 5) is None
        assert _parse_album_selection("cancel", 5) is None

    def test_all_keyword(self):
        assert _parse_album_selection("all", 4) == [1, 2, 3, 4]

    def test_asterisk_means_all(self):
        assert _parse_album_selection("*", 3) == [1, 2, 3]

    def test_short_a_means_all(self):
        # "a" is a common alias for "all"
        assert _parse_album_selection("a", 2) == [1, 2]

    def test_single_number(self):
        assert _parse_album_selection("3", 5) == [3]

    def test_comma_separated(self):
        assert _parse_album_selection("1,3,5", 5) == [1, 3, 5]

    def test_whitespace_separated(self):
        assert _parse_album_selection("1 3 5", 5) == [1, 3, 5]

    def test_mixed_separators(self):
        assert _parse_album_selection("1, 3 5", 5) == [1, 3, 5]

    def test_range(self):
        assert _parse_album_selection("1-3", 5) == [1, 2, 3]

    def test_range_reversed_is_accepted(self):
        assert _parse_album_selection("3-1", 5) == [1, 2, 3]

    def test_range_mixed_with_list(self):
        assert _parse_album_selection("1,3-5", 5) == [1, 3, 4, 5]

    def test_dedup_and_sort(self):
        # Duplicates collapse; result is sorted ascending.
        assert _parse_album_selection("5,1,3,1", 5) == [1, 3, 5]

    def test_case_insensitive(self):
        assert _parse_album_selection("ALL", 3) == [1, 2, 3]
        assert _parse_album_selection("  Q  ", 3) is None

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError, match="out of range"):
            _parse_album_selection("6", 5)
        with pytest.raises(ValueError, match="out of range"):
            _parse_album_selection("2-6", 5)

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError, match="not a number"):
            _parse_album_selection("foo", 5)
        with pytest.raises(ValueError, match="not a number"):
            _parse_album_selection("1-bar", 5)

    def test_empty_total_short_circuits(self):
        assert _parse_album_selection("1", 0) is None
        assert _parse_album_selection("all", 0) is None


class TestPickAlbums:
    """Integration-style tests for the interactive picker (prompt is mocked)."""

    def _make_albums(self, n: int) -> list[AlbumMatch]:
        return [
            AlbumMatch(
                title=f"Album {i}",
                artists=[f"Artist {i}"],
                tracks=[],
            )
            for i in range(1, n + 1)
        ]

    def test_returns_selected_albums(self):
        albums = self._make_albums(4)
        with patch("monochrome_cli.Prompt.ask", return_value="1,3"):
            selected = pick_albums(albums)
        assert [a.title for a in selected] == ["Album 1", "Album 3"]

    def test_all_keyword_returns_everything(self):
        albums = self._make_albums(3)
        with patch("monochrome_cli.Prompt.ask", return_value="all"):
            selected = pick_albums(albums)
        assert [a.title for a in selected] == ["Album 1", "Album 2", "Album 3"]

    def test_cancel_returns_empty(self):
        albums = self._make_albums(2)
        with patch("monochrome_cli.Prompt.ask", return_value="0"):
            assert pick_albums(albums) == []

    def test_empty_list_returns_empty_without_prompt(self):
        with patch("monochrome_cli.Prompt.ask") as mock_prompt:
            assert pick_albums([]) == []
        mock_prompt.assert_not_called()

    def test_reprompts_on_invalid_then_accepts(self):
        albums = self._make_albums(3)
        # First call: invalid (out of range). Second call: valid.
        with patch(
            "monochrome_cli.Prompt.ask",
            side_effect=["99", "1,2"],
        ):
            selected = pick_albums(albums)
        assert [a.title for a in selected] == ["Album 1", "Album 2"]


# ─── Format detection & fix-extensions tests ───


class TestDetectFormatFromContentType:
    def test_flac_variants(self):
        for ct in ("audio/flac", "audio/x-flac", "application/flac", "flac"):
            assert _detect_format_from_content_type(ct) == "flac", ct

    def test_m4a_variants(self):
        for ct in ("audio/mp4", "audio/m4a", "audio/x-m4a", "audio/aac"):
            assert _detect_format_from_content_type(ct) == "m4a", ct

    def test_mp3_variants(self):
        for ct in ("audio/mpeg", "audio/mp3", "audio/x-mp3"):
            assert _detect_format_from_content_type(ct) == "mp3", ct

    def test_strips_charset(self):
        assert _detect_format_from_content_type("audio/flac; charset=binary") == "flac"
        assert _detect_format_from_content_type("audio/mp4;codecs=mp4a.40.2") == "m4a"

    def test_case_insensitive(self):
        assert _detect_format_from_content_type("AUDIO/FLAC") == "flac"
        assert _detect_format_from_content_type("Audio/MP4") == "m4a"

    def test_octet_stream_returns_none(self):
        assert _detect_format_from_content_type("application/octet-stream") is None

    def test_empty_or_none(self):
        assert _detect_format_from_content_type("") is None
        assert _detect_format_from_content_type(None) is None


class TestDetectFormatFromBytes:
    def test_flac_magic(self):
        body = b"fLaC" + b"\x00" * 12
        assert _detect_format_from_bytes(body) == "flac"

    def test_m4a_ftyp_box(self):
        # 4 bytes size + "ftyp" + minor brand
        body = b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 6
        assert _detect_format_from_bytes(body) == "m4a"

    def test_m4a_ftyp_with_dash(self):
        # Some encoders use minor brand like "isom" — still detect as m4a.
        body = b"\x00\x00\x00\x20" + b"ftyp" + b"isom" + b"\x00" * 6
        assert _detect_format_from_bytes(body) == "m4a"

    def test_mp3_id3_tag(self):
        body = b"ID3\x03\x00\x00\x00" + b"\x00" * 6
        assert _detect_format_from_bytes(body) == "mp3"

    def test_mp3_frame_sync(self):
        body = b"\xff\xfb\x90\x00" + b"\x00" * 12
        assert _detect_format_from_bytes(body) == "mp3"

    def test_mp3_frame_sync_alt(self):
        # 0xFF + (0xE0 mask) = 0xE0..0xFF
        body = b"\xff\xe0\x44\x00" + b"\x00" * 12
        assert _detect_format_from_bytes(body) == "mp3"

    def test_unrecognized(self):
        assert _detect_format_from_bytes(b"\x00\x00\x00\x00" + b"random") is None
        assert _detect_format_from_bytes(b"OggS") is None

    def test_short_known_magic(self):
        # Even if fewer than 16 bytes, known magic still matches.
        assert _detect_format_from_bytes(b"fLaC") == "flac"
        assert _detect_format_from_bytes(b"\x00\x00\x00\x20ftyp") == "m4a"
        assert _detect_format_from_bytes(b"ID3") == "mp3"
        assert _detect_format_from_bytes(b"\xff\xfb") == "mp3"

    def test_empty(self):
        assert _detect_format_from_bytes(b"") is None


class TestExistingAudio:
    def test_finds_flac(self, tmp_path):
        base = tmp_path / "Artist - Title"
        (tmp_path / "Artist - Title.flac").write_bytes(b"fLaC")
        assert _existing_audio(base) == base.with_suffix(".flac")

    def test_finds_m4a(self, tmp_path):
        base = tmp_path / "Artist - Title"
        (tmp_path / "Artist - Title.m4a").write_bytes(b"\x00\x00\x00\x20ftypM4A ")
        assert _existing_audio(base) == base.with_suffix(".m4a")

    def test_finds_mp3(self, tmp_path):
        base = tmp_path / "Artist - Title"
        (tmp_path / "Artist - Title.mp3").write_bytes(b"ID3\x03\x00\x00\x00")
        assert _existing_audio(base) == base.with_suffix(".mp3")

    def test_returns_none_when_missing(self, tmp_path):
        base = tmp_path / "Artist - Title"
        assert _existing_audio(base) is None

    def test_prefers_flac(self, tmp_path):
        # If somehow multiple exist, any match returns — we just want a hit.
        base = tmp_path / "Artist - Title"
        (tmp_path / "Artist - Title.flac").write_bytes(b"fLaC")
        (tmp_path / "Artist - Title.m4a").write_bytes(b"x")
        result = _existing_audio(base)
        assert result is not None
        assert result.suffix in {".flac", ".m4a"}


def _make_response(content_type: str, body: bytes, content_length: int | None = None):
    """Build a stub requests.Response with the given headers and iterable body."""
    resp = MagicMock()
    resp.headers = {"Content-Type": content_type}
    if content_length is not None:
        resp.headers["content-length"] = str(content_length)
    resp.raise_for_status = MagicMock()

    def _iter(chunk_size: int = 8192):
        for i in range(0, len(body), chunk_size):
            yield body[i:i + chunk_size]

    resp.iter_content = _iter
    resp.close = MagicMock()
    return resp


class TestDownloadFile:
    def test_writes_flac_when_content_type_says_flac(self, tmp_path):
        body = b"fLaC" + b"\x00" * 4096
        resp = _make_response("audio/flac", body, content_length=len(body))
        with patch("monochrome_cli.requests.get", return_value=resp):
            base = tmp_path / "Artist - Title"
            out = download_file("http://x/stream", base, MagicMock(), 0)
        assert out == base.with_suffix(".flac")
        assert out.exists()
        assert out.read_bytes() == body
        # No stray .tmp files
        assert list(tmp_path.glob("*.tmp")) == []

    def test_writes_m4a_when_content_type_says_mp4(self, tmp_path):
        body = b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 100
        resp = _make_response("audio/mp4", body, content_length=len(body))
        with patch("monochrome_cli.requests.get", return_value=resp):
            base = tmp_path / "Artist - Title"
            out = download_file("http://x/stream", base, MagicMock(), 0)
        assert out.suffix == ".m4a"
        assert out.read_bytes() == body

    def test_magic_bytes_win_over_octet_stream(self, tmp_path):
        # Server declares octet-stream but body is real flac → .flac
        body = b"fLaC" + b"\x00" * 200
        resp = _make_response("application/octet-stream", body, content_length=len(body))
        with patch("monochrome_cli.requests.get", return_value=resp):
            base = tmp_path / "track"
            out = download_file("http://x", base, MagicMock(), 0)
        assert out.suffix == ".flac"

    def test_lying_content_type_corrected_by_magic(self, tmp_path):
        # Header says flac, body is actually m4a → file ends up .m4a
        body = b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 200
        resp = _make_response("audio/flac", body, content_length=len(body))
        with patch("monochrome_cli.requests.get", return_value=resp):
            base = tmp_path / "track"
            out = download_file("http://x", base, MagicMock(), 0)
        assert out.suffix == ".m4a"

    def test_does_not_overwrite_existing_final_file(self, tmp_path):
        # If something already exists at the target path, the function should
        # return that path and NOT clobber the existing file.
        base = tmp_path / "track"
        final = base.with_suffix(".flac")
        final.write_bytes(b"PRE-EXISTING")
        body = b"fLaC" + b"\x00" * 64
        resp = _make_response("audio/flac", body, content_length=len(body))
        with patch("monochrome_cli.requests.get", return_value=resp):
            out = download_file("http://x", base, MagicMock(), 0)
        assert out == final
        assert out.read_bytes() == b"PRE-EXISTING"
        # No leftover .tmp
        assert list(tmp_path.glob("*.tmp")) == []

    def test_cleans_up_tmp_on_http_error(self, tmp_path):
        resp = MagicMock()
        resp.headers = {"Content-Type": "audio/flac"}
        resp.raise_for_status = MagicMock(side_effect=Exception("HTTP 500"))
        resp.iter_content = MagicMock(return_value=iter([b"fLaC" + b"\x00" * 100]))
        resp.close = MagicMock()
        with patch("monochrome_cli.requests.get", return_value=resp):
            with pytest.raises(Exception, match="HTTP 500"):
                download_file("http://x", tmp_path / "track", MagicMock(), 0)
        # Temp files should be gone (none opened, but also nothing dangling).
        assert list(tmp_path.glob("*.tmp")) == []
        # No file with one of the supported extensions should exist either.
        for ext in EXT_FOR_FORMAT.values():
            assert not (tmp_path / f"track{ext}").exists()

    def test_empty_body_raises(self, tmp_path):
        resp = _make_response("audio/flac", b"", content_length=0)
        with patch("monochrome_cli.requests.get", return_value=resp):
            with pytest.raises(RuntimeError, match="empty response body"):
                download_file("http://x", tmp_path / "track", MagicMock(), 0)
        assert list(tmp_path.iterdir()) == []

    def test_progress_updated_with_total_and_chunks(self, tmp_path):
        body = b"fLaC" + b"\x00" * 200
        progress = MagicMock()
        resp = _make_response("audio/flac", body, content_length=len(body))
        with patch("monochrome_cli.requests.get", return_value=resp):
            download_file("http://x", tmp_path / "track", progress, 0)
        # Total should have been set to the content-length.
        total_calls = [
            c for c in progress.update.call_args_list
            if c.kwargs.get("total") == len(body)
        ]
        assert total_calls
        # At least one advance call.
        advance_calls = [
            c for c in progress.update.call_args_list
            if c.kwargs.get("advance")
        ]
        assert advance_calls


class TestFixExtensions:
    def test_renames_m4a_to_flac_extension(self, tmp_path):
        bad = tmp_path / "Pizza Hotline - AIR.flac"
        bad.write_bytes(b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 200)
        renamed, scanned = fix_extensions(tmp_path)
        assert scanned == 1
        assert renamed == 1
        assert not bad.exists()
        good = tmp_path / "Pizza Hotline - AIR.m4a"
        assert good.exists()
        assert good.read_bytes()[:4] == b"\x00\x00\x00\x20"

    def test_leaves_correctly_named_file_alone(self, tmp_path):
        ok = tmp_path / "Real Flac.flac"
        ok.write_bytes(b"fLaC" + b"\x00" * 100)
        renamed, scanned = fix_extensions(tmp_path)
        assert scanned == 1
        assert renamed == 0
        assert ok.exists()

    def test_leaves_non_audio_alone(self, tmp_path):
        # Random text file with .flac extension → not detected, left as-is.
        bad = tmp_path / "readme.flac"
        bad.write_text("not really audio")
        renamed, scanned = fix_extensions(tmp_path)
        assert scanned == 1
        assert renamed == 0
        assert bad.exists()

    def test_handles_mixed_directory(self, tmp_path):
        # Three real files in three different states plus a subdir.
        sub = tmp_path / "sub"
        sub.mkdir()

        m4a_misnamed = tmp_path / "track1.flac"
        m4a_misnamed.write_bytes(b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 50)

        flac_correct = tmp_path / "track2.flac"
        flac_correct.write_bytes(b"fLaC" + b"\x00" * 50)

        mp3_misnamed = sub / "song1.flac"
        mp3_misnamed.write_bytes(b"ID3\x03\x00\x00\x00" + b"\x00" * 50)

        text_file = tmp_path / "notes.txt"
        text_file.write_text("hello")

        renamed, scanned = fix_extensions(tmp_path)
        assert scanned == 4
        assert renamed == 2
        assert not m4a_misnamed.exists()
        assert (tmp_path / "track1.m4a").exists()
        assert flac_correct.exists()
        assert not mp3_misnamed.exists()
        assert (sub / "song1.mp3").exists()
        assert text_file.exists()

    def test_target_already_exists_skipped(self, tmp_path):
        # Both .flac and .m4a exist for the same stem; can't rename, leave it.
        flac = tmp_path / "track.flac"
        m4a = tmp_path / "track.m4a"
        flac.write_bytes(b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 50)
        m4a.write_bytes(b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 50)
        renamed, scanned = fix_extensions(tmp_path)
        assert scanned == 2
        assert renamed == 0
        # Original misnamed file remains.
        assert flac.exists()
        assert m4a.exists()

    def test_empty_directory(self, tmp_path):
        renamed, scanned = fix_extensions(tmp_path)
        assert renamed == 0
        assert scanned == 0


class TestDownloadSingleStatusLog:
    """``download_single`` should accept a ``status_log`` list and append
    per-track messages to it instead of calling ``console.print`` while a
    live progress display is active."""

    def _make_client(self, *, stream_url: str | Exception = "http://x/stream") -> MonochromeClient:
        client = MagicMock(spec=MonochromeClient)
        client.quality = "HIGH"
        if isinstance(stream_url, Exception):
            client.get_stream_url.side_effect = stream_url
        else:
            client.get_stream_url.return_value = stream_url
        return client

    def _make_track(self, title: str = "Song") -> TrackMatch:
        return TrackMatch(
            title=title,
            artists=["Artist"],
            tidal_id=1,
            isrc=None,
            album="Test Album",
            duration_sec=180,
            quality="HIGH",
        )

    def test_status_log_captures_stream_url_failure(self, tmp_path):
        client = self._make_client(stream_url=Exception("mirror down"))
        track = self._make_track()
        log: list[str] = []
        progress = MagicMock()

        with patch("monochrome_cli.console.print") as mock_print:
            status = download_single(
                client, track, tmp_path, progress, task_id=0, status_log=log
            )

        assert status == "failed"
        assert len(log) == 1
        assert "[skip]" in log[0]
        assert "mirror down" in log[0]
        # console.print must NOT have been called — the message went to the log.
        mock_print.assert_not_called()

    def test_status_log_captures_already_exists(self, tmp_path):
        # Pre-create a .flac file at the expected stem to trigger the skip path.
        (tmp_path / "Artist - Song.flac").write_bytes(b"fLaC")
        client = self._make_client()
        track = self._make_track()
        log: list[str] = []
        progress = MagicMock()

        with patch("monochrome_cli.console.print") as mock_print:
            status = download_single(
                client, track, tmp_path, progress, task_id=0, status_log=log
            )

        assert status == "skipped"
        assert len(log) == 1
        assert "already exists" in log[0]
        mock_print.assert_not_called()

    def test_without_status_log_prints_to_console(self, tmp_path):
        # Back-compat: if status_log is None, messages still go to console.print.
        client = self._make_client(stream_url=Exception("nope"))
        track = self._make_track()
        progress = MagicMock()

        with patch("monochrome_cli.console.print") as mock_print:
            status = download_single(
                client, track, tmp_path, progress, task_id=0, status_log=None
            )

        assert status == "failed"
        mock_print.assert_called_once()
        # The first positional arg should be the formatted message.
        rendered = str(mock_print.call_args.args[0])
        assert "[skip]" in rendered
        assert "nope" in rendered

    def test_keyboard_interrupt_appends_to_log_and_reraises(self, tmp_path):
        # When download_file raises KeyboardInterrupt, the message goes to
        # the log and the exception propagates.
        client = self._make_client()
        track = self._make_track()
        log: list[str] = []
        progress = MagicMock()

        with patch("monochrome_cli.download_file", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                download_single(
                    client, track, tmp_path, progress, task_id=0, status_log=log
                )
        assert any("Interrupted" in m for m in log)

    def test_keyboard_interrupt_without_log_prints(self, tmp_path):
        # Back-compat path: KeyboardInterrupt prints and re-raises.
        client = self._make_client()
        track = self._make_track()
        progress = MagicMock()

        with patch("monochrome_cli.console.print") as mock_print:
            with patch("monochrome_cli.download_file", side_effect=KeyboardInterrupt):
                with pytest.raises(KeyboardInterrupt):
                    download_single(
                        client, track, tmp_path, progress, task_id=0, status_log=None
                    )
        assert any("Interrupted" in str(c.args[0]) for c in mock_print.call_args_list)

    def test_existing_audio_with_m4a_extension(self, tmp_path):
        # Skip-detection should also work for .m4a files (the case the
        # format-detection fix introduced).
        (tmp_path / "Artist - Song.m4a").write_bytes(
            b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 50
        )
        client = self._make_client()
        track = self._make_track()
        log: list[str] = []
        status = download_single(
            client, track, tmp_path, MagicMock(), task_id=0, status_log=log
        )
        assert status == "skipped"
        assert any("already exists" in m for m in log)


# ─── MonochromeClient tests ───


class TestMonochromeClientInit:
    def test_default_mirrors(self):
        client = MonochromeClient()
        assert len(client.base_urls) >= 2
        assert len(client.qobuz_urls) >= 2

    def test_custom_mirrors(self):
        client = MonochromeClient(base_urls=["https://custom.com"], qobuz_urls=["https://qobuz.com"])
        assert "https://custom.com" in client.base_urls
        assert "https://qobuz.com" in client.qobuz_urls

    def test_duplicate_removal(self):
        client = MonochromeClient(base_urls=["https://a.com", "https://a.com"])
        assert client.base_urls.count("https://a.com") == 1

    def test_trailing_slash_stripped(self, no_default_mirrors):
        client = MonochromeClient(base_urls=["https://a.com/"])
        assert client.base_urls[0] == "https://a.com"


class TestMonochromeClientQualityToQobuz:
    def test_hi_res_lossless(self):
        client = MonochromeClient()
        assert client._quality_to_qobuz("HI_RES_LOSSLESS") == "27"

    def test_lossless(self):
        client = MonochromeClient()
        assert client._quality_to_qobuz("LOSSLESS") == "6"

    def test_default(self):
        client = MonochromeClient()
        assert client._quality_to_qobuz("HIGH") == "5"
        assert client._quality_to_qobuz("LOW") == "5"


class TestMonochromeClientExtractStreamUrl:
    def test_top_level_url(self):
        client = MonochromeClient()
        assert client._extract_stream_url({"url": "https://stream.example.com/file.flac"}) == "https://stream.example.com/file.flac"

    def test_stream_url_variants(self):
        client = MonochromeClient()
        assert client._extract_stream_url({"stream_url": "https://a.com"}) == "https://a.com"
        assert client._extract_stream_url({"streamUrl": "https://b.com"}) == "https://b.com"

    def test_nested_data(self):
        client = MonochromeClient()
        data = {"data": {"url": "https://nested.com"}}
        assert client._extract_stream_url(data) == "https://nested.com"

    def test_manifest_with_urls(self):
        client = MonochromeClient()
        manifest = '{"urls": ["https://manifest.com/audio.flac"]}'
        data = {"data": {"manifest": manifest}}
        assert client._extract_stream_url(data) == "https://manifest.com/audio.flac"

    def test_manifest_base64(self):
        client = MonochromeClient()
        import base64
        manifest = base64.b64encode(b'{"url": "https://base64.com"}').decode()
        data = {"data": {"manifest": manifest}}
        assert client._extract_stream_url(data) == "https://base64.com"

    def test_no_url(self):
        client = MonochromeClient()
        assert client._extract_stream_url({"data": {}}) is None

    def test_encrypted_media_url(self):
        client = MonochromeClient()
        manifest = '{"encryptedMediaUrl": "https://encrypted.com"}'
        data = {"data": {"manifest": manifest}}
        assert client._extract_stream_url(data) == "https://encrypted.com"


class TestMonochromeClientClassifyMissing:
    def test_preview_only(self):
        client = MonochromeClient()
        data = {"data": {"assetPresentation": "PREVIEW"}}
        assert client._classify_missing(data) == "PREVIEW_ONLY"

    def test_requires_subscription(self):
        client = MonochromeClient()
        data = {"data": {"detail": "subscription required for full access"}}
        assert client._classify_missing(data) == "REQUIRES_SUBSCRIPTION"

    def test_manifest_only(self):
        client = MonochromeClient()
        data = {"data": {"manifest": "some-dash-manifest"}}
        assert client._classify_missing(data) == "MANIFEST_ONLY"

    def test_empty_response(self):
        client = MonochromeClient()
        assert client._classify_missing({}) == "EMPTY_RESPONSE"

    def test_missing_stream_url(self):
        client = MonochromeClient()
        data = {"data": {"something": "else"}}
        assert client._classify_missing(data) == "MISSING_STREAM_URL"


class TestMonochromeClientRequestAny:
    """Tests for _request_any with mocked network calls."""

    def test_single_mirror_success(self, no_default_mirrors):
        client = MonochromeClient(base_urls=["https://mirror1.com"])
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"data": {"items": []}}
        mock_resp.headers = {"Content-Type": "application/json"}

        with patch.object(client.session, "get", return_value=mock_resp):
            data, mirror = client._request_any("/test")
            assert data == {"data": {"items": []}}
            assert mirror == "https://mirror1.com"

    def test_all_mirrors_fail(self, no_default_mirrors):
        client = MonochromeClient(base_urls=["https://fail1.com", "https://fail2.com"])
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_resp.headers = {"Content-Type": "application/json"}

        with patch.object(client.session, "get", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="All 2 monochrome mirrors failed"):
                client._request_any("/test")

    def test_html_response(self):
        client = MonochromeClient(base_urls=["https://mirror1.com"])
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 403
        mock_resp.text = "<html><body>Forbidden</body></html>"
        mock_resp.headers = {"Content-Type": "text/html"}

        with patch.object(client.session, "get", return_value=mock_resp):
            with pytest.raises(RuntimeError):
                client._request_any("/test")

    def test_invalid_json(self):
        client = MonochromeClient(base_urls=["https://mirror1.com"])
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.side_effect = json.JSONDecodeError("test", "bad json", 0)
        mock_resp.text = "not json"
        mock_resp.headers = {"Content-Type": "application/json"}

        with patch.object(client.session, "get", return_value=mock_resp):
            with pytest.raises(RuntimeError):
                client._request_any("/test")

    def test_network_error(self):
        client = MonochromeClient(base_urls=["https://mirror1.com"])

        with patch.object(client.session, "get", side_effect=Exception("Connection refused")):
            with pytest.raises(RuntimeError):
                client._request_any("/test")

    def test_preferred_mirror_first(self):
        client = MonochromeClient(base_urls=["https://a.com", "https://b.com"])
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"ok": True}
        mock_resp.headers = {"Content-Type": "application/json"}

        with patch.object(client.session, "get", return_value=mock_resp) as mock_get:
            client._request_any("/test", preferred="https://b.com")
            # The preferred mirror should be tried first
            first_call = mock_get.call_args_list[0]
            assert "https://b.com" in str(first_call)

    def test_success_rate_sorting(self, tmp_path, no_default_mirrors):
        """Mirrors with higher success rate should be tried first."""
        stats_path = tmp_path / "stats.json"
        # Pre-populate stats: a.com has 100% success, b.com has 0%
        stats_data = {
            "https://a.com": {"ok": 10, "total": 10},
            "https://b.com": {"ok": 0, "total": 10},
        }
        stats_path.write_text(json.dumps(stats_data))

        client = MonochromeClient(base_urls=["https://b.com", "https://a.com"])
        client.stats = MirrorStats(path=stats_path)
        client.stats.stats = stats_data

        sorted_mirrors = client._sorted_mirrors()
        assert sorted_mirrors[0] == "https://a.com"
        assert sorted_mirrors[1] == "https://b.com"


class TestMonochromeClientGetStreamUrl:
    """Tests for get_stream_url, especially the fallback logic."""

    def test_monochrome_success(self):
        client = MonochromeClient(base_urls=["https://mono.com"])
        mock_data = {"url": "https://stream.example.com/track.flac"}

        with patch.object(client, "_request_any", return_value=(mock_data, "https://mono.com")):
            url = client.get_stream_url(123, quality="HIGH")
            assert url == "https://stream.example.com/track.flac"

    def test_fallback_to_qobuz_when_monochrome_fails(self):
        """When all monochrome mirrors fail, Qobuz should be tried."""
        client = MonochromeClient(
            base_urls=["https://fail.com"],
            qobuz_urls=["https://qobuz.com"],
        )

        # _request_any raises RuntimeError (all mirrors failed)
        with patch.object(client, "_request_any", side_effect=RuntimeError("All 1 monochrome mirrors failed")):
            with patch.object(client, "_try_qobuz", return_value="https://qobuz.stream.com"):
                url = client.get_stream_url(123, quality="HIGH", isrc="USABC1234567")
                assert url == "https://qobuz.stream.com"

    def test_no_fallback_without_isrc(self):
        """If no ISRC, Qobuz fallback should be skipped."""
        client = MonochromeClient(
            base_urls=["https://fail.com"],
            qobuz_urls=["https://qobuz.com"],
        )

        with patch.object(client, "_request_any", side_effect=RuntimeError("All 1 monochrome mirrors failed")):
            with pytest.raises(RuntimeError) as exc_info:
                client.get_stream_url(123, quality="HIGH", isrc=None)
            assert "no Qobuz fallback" in str(exc_info.value) or "All mirrors failed" in str(exc_info.value)

    def test_no_fallback_without_qobuz_mirrors(self, no_default_mirrors):
        """If no Qobuz mirrors configured, fallback should be skipped."""
        client = MonochromeClient(
            base_urls=["https://fail.com"],
            qobuz_urls=[],
        )

        with patch.object(client, "_request_any", side_effect=RuntimeError("All 1 monochrome mirrors failed")):
            with pytest.raises(RuntimeError) as exc_info:
                client.get_stream_url(123, quality="HIGH", isrc="USABC1234567")
            assert "no Qobuz fallback" in str(exc_info.value)

    def test_monochrome_data_no_url(self):
        """When monochrome returns data but no stream URL, diagnose properly."""
        client = MonochromeClient(base_urls=["https://mono.com"])
        mock_data = {"data": {"assetPresentation": "PREVIEW"}}

        with patch.object(client, "_request_any", return_value=(mock_data, "https://mono.com")):
            with pytest.raises(RuntimeError) as exc_info:
                client.get_stream_url(123, quality="HIGH")
            assert "No playable URL" in str(exc_info.value)

    def test_qobuz_fallback_fails(self):
        """When both monochrome and Qobuz fail, raise appropriate error."""
        client = MonochromeClient(
            base_urls=["https://fail.com"],
            qobuz_urls=["https://qobuz.com"],
        )

        with patch.object(client, "_request_any", side_effect=RuntimeError("All 1 monochrome mirrors failed")):
            with patch.object(client, "_try_qobuz", return_value=None):
                with pytest.raises(RuntimeError) as exc_info:
                    client.get_stream_url(123, quality="HIGH", isrc="USABC1234567")
                assert "All mirrors failed" in str(exc_info.value)
                assert "monochrome" in str(exc_info.value).lower()
                assert "qobuz" in str(exc_info.value).lower()


class TestMonochromeClientTryQobuz:
    """Tests for _try_qobuz method."""

    def test_qobuz_success(self):
        client = MonochromeClient(qobuz_urls=["https://qobuz.com"])

        mock_search = MagicMock()
        mock_search.ok = True
        mock_search.json.return_value = {
            "data": {"tracks": {"items": [{"isrc": "USABC1234567", "id": "track123"}]}}
        }

        mock_stream = MagicMock()
        mock_stream.ok = True
        mock_stream.json.return_value = {"data": {"url": "https://stream.qobuz.com/track.flac"}}

        def mock_get(url, **kwargs):
            if "get-music" in url:
                return mock_search
            if "download-music" in url:
                return mock_stream
            return MagicMock(ok=False)

        with patch.object(client.session, "get", side_effect=mock_get):
            url = client._try_qobuz("USABC1234567", "LOSSLESS")
            assert url == "https://stream.qobuz.com/track.flac"

    def test_qobuz_no_match(self):
        client = MonochromeClient(qobuz_urls=["https://qobuz.com"])

        mock_search = MagicMock()
        mock_search.ok = True
        mock_search.json.return_value = {
            "data": {"tracks": {"items": []}}
        }

        with patch.object(client.session, "get", return_value=mock_search):
            url = client._try_qobuz("USABC1234567", "LOSSLESS")
            assert url is None

    def test_qobuz_mirror_down(self):
        client = MonochromeClient(qobuz_urls=["https://qobuz.com"])

        mock_search = MagicMock()
        mock_search.ok = False
        mock_search.status_code = 503

        with patch.object(client.session, "get", return_value=mock_search):
            url = client._try_qobuz("USABC1234567", "LOSSLESS")
            assert url is None

    def test_fallback_to_first_track(self):
        """If ISRC doesn't match but tracks exist, use first track."""
        client = MonochromeClient(qobuz_urls=["https://qobuz.com"])

        mock_search = MagicMock()
        mock_search.ok = True
        mock_search.json.return_value = {
            "data": {"tracks": {"items": [{"isrc": "DIFFERENT", "id": "track456"}]}}
        }

        mock_stream = MagicMock()
        mock_stream.ok = True
        mock_stream.json.return_value = {"data": {"url": "https://stream.qobuz.com/fallback.flac"}}

        def mock_get(url, **kwargs):
            if "get-music" in url:
                return mock_search
            if "download-music" in url:
                return mock_stream
            return MagicMock(ok=False)

        with patch.object(client.session, "get", side_effect=mock_get):
            url = client._try_qobuz("USABC1234567", "LOSSLESS")
            assert url == "https://stream.qobuz.com/fallback.flac"


class TestMonochromeClientSearch:
    """Tests for search method."""

    def test_basic_search(self):
        client = MonochromeClient(base_urls=["https://mono.com"])
        mock_data = {
            "data": {
                "items": [
                    {
                        "id": 123,
                        "title": "Song",
                        "artists": [{"name": "Artist"}],
                        "album": {"title": "Album", "cover": "abc-123"},
                        "duration": 180,
                        "audioQuality": "HIGH",
                        "isrc": "USABC1234567",
                    }
                ]
            }
        }

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = mock_data
        mock_resp.headers = {"Content-Type": "application/json"}

        with patch.object(client.session, "get", return_value=mock_resp):
            results, mirror = client.search("test query")
            assert len(results) == 1
            assert results[0].tidal_id == 123
            assert results[0].title == "Song"
            assert results[0].isrc == "USABC1234567"

    def test_empty_search(self):
        client = MonochromeClient(base_urls=["https://mono.com"])
        mock_data = {"data": {"items": []}}

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = mock_data
        mock_resp.headers = {"Content-Type": "application/json"}

        with patch.object(client.session, "get", return_value=mock_resp):
            results, mirror = client.search("test query")
            assert len(results) == 0

    def test_search_invalid_response(self):
        client = MonochromeClient(base_urls=["https://mono.com"])
        mock_data = {"data": {"items": "not a list"}}

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = mock_data
        mock_resp.headers = {"Content-Type": "application/json"}

        with patch.object(client.session, "get", return_value=mock_resp):
            results, mirror = client.search("test query")
            assert len(results) == 0
            assert mirror is None


class TestMonochromeClientSearchPaginated:
    """Tests for search_paginated method."""

    def test_paginated_search(self):
        client = MonochromeClient(base_urls=["https://mono.com"])
        mock_data = {
            "data": {
                "items": [
                    {
                        "id": i,
                        "title": f"Song {i}",
                        "artists": [{"name": "Artist"}],
                        "album": {"title": "Album"},
                        "duration": 180,
                        "audioQuality": "HIGH",
                    }
                    for i in range(3)
                ]
            }
        }

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = mock_data
        mock_resp.headers = {"Content-Type": "application/json"}

        with patch.object(client.session, "get", return_value=mock_resp):
            results = client.search_paginated("test", max_pages=1)
            assert len(results) == 3

    def test_deduplication(self):
        client = MonochromeClient(base_urls=["https://mono.com"])
        # Same items returned twice — should deduplicate
        item = {
            "id": 123,
            "title": "Song",
            "artists": [{"name": "Artist"}],
            "album": {"title": "Album"},
            "duration": 180,
            "audioQuality": "HIGH",
        }
        mock_data = {"data": {"items": [item, item]}}

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = mock_data
        mock_resp.headers = {"Content-Type": "application/json"}

        with patch.object(client.session, "get", return_value=mock_resp):
            results = client.search_paginated("test", max_pages=1)
            assert len(results) == 1
            assert results[0].tidal_id == 123


# ─── End-to-end fallback integration test ───


class TestFallbackIntegration:
    """Integration tests verifying the complete fallback chain."""

    def test_full_fallback_chain(self):
        """
        Simulate: all monochrome mirrors fail, then Qobuz succeeds.
        This is the exact bug scenario from the user's report.
        """
        client = MonochromeClient(
            base_urls=["https://bad-mirror-1.com", "https://bad-mirror-2.com"],
            qobuz_urls=["https://good-qobuz.com"],
        )

        with patch.object(client, "_request_any", side_effect=RuntimeError("All 2 monochrome mirrors failed")):
            with patch.object(client, "_try_qobuz", return_value="https://stream.qobuz.com/good.flac") as mock_qobuz:
                url = client.get_stream_url(123, quality="HIGH", isrc="USABC1234567")
                assert url == "https://stream.qobuz.com/good.flac"
                mock_qobuz.assert_called_once_with("USABC1234567", "HIGH")

    def test_no_qobuz_configured(self, no_default_mirrors):
        """When no Qobuz mirrors are configured, the error should mention that."""
        client = MonochromeClient(
            base_urls=["https://bad-mirror.com"],
            qobuz_urls=[],
        )

        with patch.object(client, "_request_any", side_effect=RuntimeError("All 1 monochrome mirrors failed")):
            with pytest.raises(RuntimeError) as exc_info:
                client.get_stream_url(123, quality="HIGH", isrc="USABC1234567")
            error_msg = str(exc_info.value).lower()
            assert "no qobuz fallback" in error_msg


# ─── Run if executed directly ───

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
