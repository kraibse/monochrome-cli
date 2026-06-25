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
    DEFAULT_CFG_QUALITY,
    EXT_FOR_FORMAT,
    KEY_BACKSPACE,
    KEY_DOWN,
    KEY_ENTER,
    KEY_ESC,
    KEY_OTHER,
    KEY_QUIT,
    KEY_SPACE,
    KEY_UP,
    MirrorError,
    MirrorStats,
    MonochromeClient,
    PickerState,
    TrackMatch,
    VALID_QUALITIES,
    _TerminalRaw,
    _detect_format_from_bytes,
    _detect_format_from_content_type,
    _existing_audio,
    _parse_album_selection,
    _parse_key_bytes,
    _main,
    _read_one_key,
    _resolve_default_quality,
    _select_albums_with_keys,
    artist_matches,
    classify_error,
    download_album,
    download_albums,
    download_discography,
    download_file,
    download_single,
    fix_extensions,
    normalize_artist,
    pick_albums,
    redact_url,
    safe_snippet,
    sanitize_filename,
    select_albums,
    select_tracks,
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


class TestMainSingleTrackPath:
    """Regression: the single-track CLI branch must pass ``task_id`` to
    ``download_single``. Previously it omitted the argument, raising
    ``TypeError: download_single() missing 1 required positional argument: 'task_id'``.
    """

    def _track(self) -> TrackMatch:
        return TrackMatch(
            title="Song",
            artists=["Artist"],
            tidal_id=1,
            isrc=None,
            album="Album",
            duration_sec=180,
            quality="HIGH",
        )

    def test_download_single_receives_task_id(self, tmp_path):
        track = self._track()
        client = MagicMock()
        client.search.return_value = ([track], None)

        with patch("monochrome_cli.MonochromeClient", return_value=client), \
             patch("monochrome_cli.select_tracks", return_value=[track]), \
             patch("monochrome_cli.download_single", return_value="downloaded") as mock_dl, \
             patch("monochrome_cli.Progress") as mock_progress_cls, \
             patch("sys.argv", [
                 "monochrome_cli.py", "--no-tui",
                 "-o", str(tmp_path), "query",
             ]):
            progress_cm = mock_progress_cls.return_value.__enter__.return_value
            progress_cm.add_task.return_value = 42
            rc = _main()

        assert rc == 0
        mock_dl.assert_called_once()
        args, _kwargs = mock_dl.call_args
        # Positional signature: (client, track, output_dir, progress, task_id)
        assert len(args) >= 5
        assert args[4] == 42  # task_id must be the id returned by add_task
        progress_cm.add_task.assert_called_once()


class TestDownloadDiscography:
    """Discography mode routes through ``select_albums`` (TUI by default)
    and then a shared ``download_albums`` loop.
    """

    def _albums(self, n: int) -> list[AlbumMatch]:
        out: list[AlbumMatch] = []
        for i in range(n):
            tracks = [
                TrackMatch(
                    tidal_id=j,
                    title=f"Track {j}",
                    artists=["Artist"],
                    album=f"Album {i}",
                    duration_sec=180,
                    quality="LOSELESS",
                )
                for j in range(i + 1)
            ]
            out.append(AlbumMatch(title=f"Album {i}", artists=["Artist"], tracks=tracks))
        return out

    def test_empty_selection_returns_zero(self):
        client = MagicMock()
        with patch(
            "monochrome_cli.select_albums",
            return_value=[],
        ) as mock_pick:
            result = download_discography(
                client, self._albums(3), Path("/tmp"), "Artist",
                force_tui=True,
            )
        assert result == 0
        mock_pick.assert_called_once()
        client.assert_not_called()

    def test_selection_passes_force_tui(self):
        client = MagicMock()
        with patch(
            "monochrome_cli.select_albums",
            return_value=self._albums(2),
        ) as mock_pick, patch(
            "monochrome_cli.download_albums",
            return_value=5,
        ) as mock_loop:
            download_discography(
                client, self._albums(3), Path("/tmp"), "Daft Punk",
                force_tui=False,
            )
        # forward force_tui to the picker
        _args, kwargs = mock_pick.call_args
        assert kwargs.get("force_tui") is False
        # forward artist_folder to the loop
        _args, kwargs = mock_loop.call_args
        assert kwargs.get("artist_folder") == "Daft Punk"
        assert kwargs.get("summary_title") == "Discography Summary"

    def test_auto_force_tui_when_unspecified(self):
        with patch(
            "monochrome_cli.select_albums",
            return_value=[],
        ) as mock_pick:
            download_discography(
                MagicMock(), self._albums(1), Path("/tmp"), "Artist",
            )
        _args, kwargs = mock_pick.call_args
        assert kwargs.get("force_tui") is None  # auto-detect

    def test_download_albums_single_skips_summary(self):
        """A one-album selection should call download_album once and
        return its result, without rendering a summary table."""
        with patch(
            "monochrome_cli.download_album",
            return_value=3,
        ) as mock_dl:
            result = download_albums(
                MagicMock(), self._albums(1), Path("/tmp"),
                artist_folder="Artist",
            )
        assert result == 3
        assert mock_dl.call_count == 1

    def test_download_albums_multi_runs_loop(self):
        albums = self._albums(3)
        with patch(
            "monochrome_cli.download_album",
            side_effect=[5, 4, 3],
        ) as mock_dl:
            result = download_albums(
                MagicMock(), albums, Path("/tmp"),
                artist_folder="Artist",
                summary_title="Test Summary",
            )
        assert result == 12
        assert mock_dl.call_count == 3
        # index tuple is (i, total)
        for i, call in enumerate(mock_dl.call_args_list, 1):
            assert call.kwargs["album_index"] == (i, 3)
            assert call.kwargs["artist_folder"] == "Artist"


class TestAlbumMatchQuality:
    """AlbumMatch.quality should report the best (highest) quality across
    the album's tracks.
    """

    def _track(self, quality: str) -> TrackMatch:
        return TrackMatch(
            tidal_id=1,
            title="t",
            artists=["a"],
            album="alb",
            duration_sec=180,
            quality=quality,
        )

    def test_no_tracks_returns_dash(self):
        a = AlbumMatch(title="Empty", artists=["x"], tracks=[])
        assert a.quality == "—"

    def test_single_track_quality(self):
        a = AlbumMatch(title="t", artists=["x"], tracks=[self._track("LOSSLESS")])
        assert a.quality == "LOSSLESS"

    def test_best_of_mixed(self):
        a = AlbumMatch(
            title="t", artists=["x"],
            tracks=[self._track("LOW"), self._track("HI_RES_LOSSLESS"), self._track("HIGH")],
        )
        assert a.quality == "HI_RES_LOSSLESS"

    def test_ignores_tracks_with_no_quality(self):
        a = AlbumMatch(
            title="t", artists=["x"],
            tracks=[self._track(""), self._track("HIGH")],
        )
        assert a.quality == "HIGH"


class TestResolveDefaultQuality:
    """Priority: -q CLI > MONOCHROME_DL_QUALITY env > config.json "quality"
    > "HIGH" fallback. Invalid values fall back to HIGH with a warning.
    """

    def setup_method(self):
        self._env = os.environ.copy()

    def teardown_method(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_env_beats_config(self):
        real_get = os.environ.get
        with patch("monochrome_cli._cfg", {"quality": "HI_RES_LOSSLESS"}), \
             patch(
                 "monochrome_cli.os.environ.get",
                 side_effect=lambda k, *a: "LOSSLESS" if k == "MONOCHROME_DL_QUALITY" else real_get(k, *a),
             ):
            assert _resolve_default_quality() == "LOSSLESS"

    def test_config_used_when_no_env(self):
        real_get = os.environ.get
        with patch("monochrome_cli._cfg", {"quality": "HI_RES_LOSSLESS"}), \
             patch(
                 "monochrome_cli.os.environ.get",
                 side_effect=lambda k, *a: None if k == "MONOCHROME_DL_QUALITY" else real_get(k, *a),
             ):
            assert _resolve_default_quality() == "HI_RES_LOSSLESS"

    def test_fallback_high_when_nothing_set(self):
        real_get = os.environ.get
        with patch("monochrome_cli._cfg", {}), \
             patch(
                 "monochrome_cli.os.environ.get",
                 side_effect=lambda k, *a: None if k == "MONOCHROME_DL_QUALITY" else real_get(k, *a),
             ):
            assert _resolve_default_quality() == "HIGH"

    def test_invalid_env_value_falls_back(self):
        real_get = os.environ.get
        with patch("monochrome_cli._cfg", {}), \
             patch(
                 "monochrome_cli.os.environ.get",
                 side_effect=lambda k, *a: "NONSENSE" if k == "MONOCHROME_DL_QUALITY" else real_get(k, *a),
             ):
            assert _resolve_default_quality() == "HIGH"

    def test_valid_qualities_accepted(self):
        for q in VALID_QUALITIES:
            real_get = os.environ.get
            with patch("monochrome_cli._cfg", {}), \
                 patch(
                     "monochrome_cli.os.environ.get",
                     side_effect=lambda k, *a, _q=q: _q if k == "MONOCHROME_DL_QUALITY" else real_get(k, *a),
                 ):
                assert _resolve_default_quality() == q


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


# ─── TUI picker tests ───


class TestParseKeyBytes:
    """The byte→name normaliser is pure and must be exhaustively tested."""

    def test_arrows(self):
        assert _parse_key_bytes(b"\x1b[A") == KEY_UP
        assert _parse_key_bytes(b"\x1b[B") == KEY_DOWN

    def test_space_and_enter(self):
        assert _parse_key_bytes(b" ") == KEY_SPACE
        assert _parse_key_bytes(b"\r") == KEY_ENTER
        assert _parse_key_bytes(b"\n") == KEY_ENTER

    def test_backspace(self):
        assert _parse_key_bytes(b"\x7f") == KEY_BACKSPACE
        assert _parse_key_bytes(b"\x08") == KEY_BACKSPACE

    def test_q_lowercase_and_uppercase(self):
        assert _parse_key_bytes(b"q") == KEY_QUIT
        assert _parse_key_bytes(b"Q") == KEY_QUIT

    def test_a_lowercase_and_uppercase(self):
        # Used for "select all"; same name as a literal.
        assert _parse_key_bytes(b"a") == "a"
        assert _parse_key_bytes(b"A") == "a"

    def test_empty_and_unknown(self):
        assert _parse_key_bytes(b"") == "OTHER"
        assert _parse_key_bytes(b"x") == "OTHER"
        assert _parse_key_bytes(b"z") == "OTHER"

    def test_garbage_escape_returns_other(self):
        # Lone ESC byte is handled by _read_one_key, not here; a 2-byte
        # buffer that doesn't look like a sequence is "OTHER".
        assert _parse_key_bytes(b"\x1b[") == "OTHER"

    def test_known_terminator_bytes(self):
        assert _parse_key_bytes(b"\x03") == "CTRL_C"
        assert _parse_key_bytes(b"\x04") == "CTRL_D"

    def test_three_byte_sequences_have_correct_shape(self):
        """Regression: ESC [ A (up arrow) is 3 bytes; ESC [ [ A (4 bytes) is
        malformed and must NOT be returned as KEY_UP."""
        assert _parse_key_bytes(b"\x1b" + b"[" + b"A") == KEY_UP
        assert _parse_key_bytes(b"\x1b" + b"[" + b"B") == KEY_DOWN
        assert _parse_key_bytes(b"\x1b[A") == KEY_UP
        assert _parse_key_bytes(b"\x1b[[A") == KEY_OTHER  # malformed


class TestReadOneKey:
    """_read_one_key reads raw bytes from a _TerminalRaw. The interesting
    cases are the multi-byte ESC sequences — those previously failed when
    the buffer was concatenated incorrectly.
    """

    class _FakeRaw:
        def __init__(self, fd_marker: int = 7) -> None:
            self._fd = fd_marker
            self._buf = bytearray()
            self._is_win = False

        def feed(self, data: bytes) -> None:
            self._buf.extend(data)

        def read(self) -> bytes:
            if not self._buf:
                raise OSError("no data")
            return bytes([self._buf.pop(0)])

    def test_arrow_up_three_byte_sequence(self):
        raw = self._FakeRaw()
        raw.feed(b"\x1b[A")
        with patch("monochrome_cli.select.select", return_value=([raw._fd], [], [])):
            assert _read_one_key(raw) == KEY_UP

    def test_arrow_down_three_byte_sequence(self):
        raw = self._FakeRaw()
        raw.feed(b"\x1b[B")
        with patch("monochrome_cli.select.select", return_value=([raw._fd], [], [])):
            assert _read_one_key(raw) == KEY_DOWN

    def test_bare_escape_returns_esc(self):
        raw = self._FakeRaw()
        raw.feed(b"\x1b")
        with patch("monochrome_cli.select.select", return_value=([], [], [])):
            assert _read_one_key(raw) == KEY_ESC

    def test_lone_space_returns_space(self):
        raw = self._FakeRaw()
        raw.feed(b" ")
        assert _read_one_key(raw) == KEY_SPACE


class TestPickerState:
    """Pure state model. No I/O — every transition is just a method call."""

    def test_empty_state(self):
        st = PickerState(0)
        assert st.counts() == (0, 0, 0)
        assert st.confirmed() == []
        st.move(1)  # must be a no-op, not raise
        assert st.cursor == 0

    def test_initial_all_unmarked(self):
        st = PickerState(3)
        assert all(st.state_at(i) == "unmarked" for i in range(3))
        assert st.counts() == (0, 0, 3)

    def test_move_clamps_to_bounds(self):
        st = PickerState(3)
        st.move(-1)
        assert st.cursor == 0
        st.move(2)
        assert st.cursor == 2
        st.move(1)
        assert st.cursor == 2  # clamped at n - 1
        st.move(-5)
        assert st.cursor == 0  # clamped at 0

    def test_toggle_mark_cycle(self):
        st = PickerState(1)
        st.toggle_mark()
        assert st.is_marked(0)
        st.toggle_mark()
        assert st.state_at(0) == "unmarked"
        st.toggle_mark()
        assert st.is_marked(0)

    def test_excluding_an_unmarked_row(self):
        st = PickerState(1)
        st.toggle_exclude()
        assert st.is_excluded(0)
        assert not st.is_marked(0)

    def test_excluding_a_marked_row_unmarks_first(self):
        st = PickerState(1)
        st.toggle_mark()
        st.toggle_exclude()
        # exclude and mark are mutually exclusive; exclude wins and clears mark
        assert st.is_excluded(0)
        assert not st.is_marked(0)

    def test_toggling_mark_on_excluded_clears_exclude(self):
        st = PickerState(1)
        st.toggle_exclude()
        assert st.is_excluded(0)
        st.toggle_mark()
        # Marking an excluded row only clears the exclude — it does not also
        # mark the row. (User must press Space twice to go from "excluded" to
        # "marked".)
        assert not st.is_excluded(0)
        assert not st.is_marked(0)
        st.toggle_mark()
        assert st.is_marked(0)

    def test_un_exclude(self):
        st = PickerState(1)
        st.toggle_exclude()
        st.toggle_exclude()
        assert st.state_at(0) == "unmarked"

    def test_select_all_marks_everything(self):
        st = PickerState(4)
        st.select_all()
        assert st.counts() == (4, 0, 4)
        assert st.confirmed() == [0, 1, 2, 3]

    def test_select_all_again_unmarks(self):
        st = PickerState(3)
        st.select_all()
        st.select_all()
        assert st.counts() == (0, 0, 3)

    def test_select_all_preserves_excluded(self):
        st = PickerState(3)
        st.toggle_exclude()  # cursor at 0
        st.move(1)
        st.select_all()  # 0 excluded, 1 and 2 marked
        assert st.is_excluded(0)
        assert st.is_marked(1)
        assert st.is_marked(2)

    def test_select_all_with_everything_already_marked_unmarks_all(self):
        st = PickerState(3)
        # Mark each row explicitly by walking the cursor.
        st.toggle_mark()
        st.move(1)
        st.toggle_mark()
        st.move(1)
        st.toggle_mark()
        assert st.counts() == (3, 0, 3)
        # all marked; toggle should clear them
        st.select_all()
        assert st.counts() == (0, 0, 3)

    def test_confirmed_returns_only_marked(self):
        st = PickerState(4)
        st.move(1)
        st.toggle_mark()
        st.move(1)
        st.toggle_exclude()
        st.move(1)
        st.toggle_mark()
        # marked: indices 1 and 3 (in display order)
        assert st.confirmed() == [1, 3]

    def test_cursor_does_not_persist_marks(self):
        st = PickerState(3)
        st.move(2)
        st.toggle_mark()
        st.move(0)
        assert st.is_marked(2)
        assert not st.is_marked(0)


def _make_albums_for_tui(n: int) -> list[AlbumMatch]:
    """Albums with varying track counts so the order test is meaningful."""
    out: list[AlbumMatch] = []
    for i in range(n):
        tracks = [
            TrackMatch(
                tidal_id=j,
                title=f"Track {j}",
                artists=[f"Artist {i}"],
                album=f"Album {i}",
                duration_sec=180,
                quality="LOSELESS",
            )
            for j in range(i + 1)
        ]
        out.append(
            AlbumMatch(
                title=f"Album {i}",
                artists=[f"Artist {i}"],
                tracks=tracks,
            )
        )
    return out


class TestSelectAlbumsWithKeys:
    """Drive the picker with a synthetic key sequence. No real terminal."""

    def test_cursor_fallback_on_plain_enter(self):
        albums = _make_albums_for_tui(3)
        # Move down twice then confirm without marking anything. Albums are
        # sorted by track count desc, so display order is
        # [Album 2 (3), Album 1 (2), Album 0 (1)].
        result = _select_albums_with_keys(albums, [KEY_DOWN, KEY_DOWN, KEY_ENTER])
        assert [a.title for a in result] == ["Album 0"]

    def test_space_marks_and_enter_returns_marked(self):
        albums = _make_albums_for_tui(3)
        # sorted: [2, 1, 0]  (track counts 3, 2, 1)
        # Mark the first row, confirm.
        result = _select_albums_with_keys(albums, [KEY_SPACE, KEY_ENTER])
        assert [a.title for a in result] == ["Album 2"]

    def test_multi_select_with_arrows(self):
        albums = _make_albums_for_tui(3)
        # sorted: [2, 1, 0]
        # mark cursor, down, mark, confirm → [2, 1]
        result = _select_albums_with_keys(
            albums,
            [KEY_SPACE, KEY_DOWN, KEY_SPACE, KEY_ENTER],
        )
        assert [a.title for a in result] == ["Album 2", "Album 1"]

    def test_select_all_marks_everything(self):
        albums = _make_albums_for_tui(3)
        result = _select_albums_with_keys(albums, ["a", KEY_ENTER])
        assert [a.title for a in result] == ["Album 2", "Album 1", "Album 0"]

    def test_exclude_keeps_row_visible_but_skipped(self):
        albums = _make_albums_for_tui(3)
        # sorted: [2, 1, 0]. Mark all, then go back to row 1 (Album 2) and
        # exclude it — should drop from confirmed output.
        result = _select_albums_with_keys(
            albums,
            ["a", KEY_BACKSPACE, KEY_ENTER],
        )
        assert [a.title for a in result] == ["Album 1", "Album 0"]

    def test_q_cancels(self):
        albums = _make_albums_for_tui(2)
        result = _select_albums_with_keys(albums, [KEY_SPACE, KEY_QUIT])
        assert result == []

    def test_esc_cancels(self):
        albums = _make_albums_for_tui(2)
        result = _select_albums_with_keys(albums, [KEY_ESC])
        assert result == []

    def test_empty_albums_returns_empty(self):
        assert _select_albums_with_keys([], [KEY_ENTER]) == []


def _make_tracks_for_tui(n: int) -> list[TrackMatch]:
    return [
        TrackMatch(
            tidal_id=i,
            title=f"Title {i}",
            artists=[f"Artist {i}"],
            album=f"Album {i}",
            duration_sec=180,
            quality="LOSELESS",
        )
        for i in range(n)
    ]


class TestSelectTracksWithKeys:
    """Tracks are picked from a sorted list; the cursor fallback applies the
    same way."""

    def test_plain_enter_returns_cursor_row(self):
        tracks = _make_tracks_for_tui(3)
        result = select_tracks.__wrapped__ if hasattr(select_tracks, "__wrapped__") else None  # noqa: E501
        # Use the public dispatcher with key_source via a small helper:
        from monochrome_cli import _run_picker, _label_track, _row_track  # noqa: E401, F401

        it = iter([KEY_DOWN, KEY_ENTER])

        def src():
            try:
                return next(it)
            except StopIteration:
                return None

        outcome = _run_picker(
            title="t",
            headers=[("Artist", "g"), ("Title", "b"), ("Album", "m"), ("Quality", "y")],
            items=tracks,
            row_fn=_row_track,
            label_fn=_label_track,
            key_source=src,
        )
        assert outcome is not None
        _state, indices = outcome
        assert [tracks[i].title for i in indices] == ["Title 1"]


class TestSelectFallback:
    """The public dispatchers must fall back to the legacy prompt when not
    on a TTY (or when --no-tui is passed via force_tui=False).
    """

    def test_select_albums_falls_back_to_prompt(self):
        albums = _make_albums_for_tui(3)
        with patch("monochrome_cli._is_tty", return_value=False), \
             patch("monochrome_cli.Prompt.ask", return_value="2") as mock_prompt:
            result = select_albums(albums)
        assert [a.title for a in result] == ["Album 1"]
        mock_prompt.assert_called()

    def test_select_tracks_falls_back_to_int_prompt(self):
        tracks = _make_tracks_for_tui(2)
        with patch("monochrome_cli._is_tty", return_value=False), \
             patch("monochrome_cli.IntPrompt.ask", return_value=1) as mock_prompt:
            result = select_tracks(tracks)
        assert [t.title for t in result] == ["Title 0"]
        mock_prompt.assert_called()

    def test_select_albums_explicit_no_tui(self):
        albums = _make_albums_for_tui(3)
        with patch("monochrome_cli.Prompt.ask", return_value="all") as mock_prompt:
            result = select_albums(albums, force_tui=False)
        mock_prompt.assert_called()
        assert len(result) == 3

    def test_select_tracks_explicit_no_tui(self):
        tracks = _make_tracks_for_tui(2)
        with patch("monochrome_cli.IntPrompt.ask", return_value=0):
            result = select_tracks(tracks, force_tui=False)
        assert result == []

    def test_select_albums_falls_back_when_tui_unavailable(self):
        """When the dispatcher auto-detects TUI but the raw-mode layer
        reports no real terminal, it must transparently fall through to the
        prompt so a usable selection is always offered."""
        albums = _make_albums_for_tui(3)
        with patch("monochrome_cli._tui_available", return_value=False), \
             patch("monochrome_cli.Prompt.ask", return_value="2") as mock_prompt:
            result = select_albums(albums)  # force_tui=None → auto
        assert [a.title for a in result] == ["Album 1"]
        mock_prompt.assert_called()

    def test_select_tracks_falls_back_when_tui_unavailable(self):
        tracks = _make_tracks_for_tui(2)
        with patch("monochrome_cli._tui_available", return_value=False), \
             patch("monochrome_cli.IntPrompt.ask", return_value=2):
            result = select_tracks(tracks)  # force_tui=None → auto
        assert [t.title for t in result] == ["Title 1"]

    def test_explicit_tui_falls_back_with_notice(self):
        """If the user passes --tui but the TUI is unavailable, the
        dispatcher should warn and fall back to the prompt so the user can
        still get a selection — failing silently would be worse."""
        tracks = _make_tracks_for_tui(2)
        with patch("monochrome_cli._tui_available", return_value=False), \
             patch("monochrome_cli.IntPrompt.ask", return_value=1):
            result = select_tracks(tracks, force_tui=True)
        assert [t.title for t in result] == ["Title 0"]


# ─── Parallelism & prefetch tests ───


class TestSearchParallelism:
    """New parallel fan-out behaviour for search and album resolution."""

    def _track_item(self, tid: int, title: str = "Song") -> dict:
        return {
            "id": tid,
            "title": title,
            "artists": [{"name": "Artist"}],
            "album": {"title": "Album"},
            "duration": 180,
            "audioQuality": "HIGH",
        }

    def test_paginated_search_queries_all_mirrors_in_parallel(self):
        client = MonochromeClient(base_urls=["https://a.com", "https://b.com"])

        def fake_get(url: str, **kwargs):
            resp = MagicMock()
            resp.ok = True
            resp.headers = {"Content-Type": "application/json"}
            if url.startswith("https://a.com"):
                resp.json.return_value = {"data": {"items": [self._track_item(1), self._track_item(2)]}}
            else:
                resp.json.return_value = {"data": {"items": [self._track_item(2), self._track_item(3)]}}
            return resp

        with patch.object(client.session, "get", side_effect=fake_get):
            results = client.search_paginated("test", max_pages=1)

        ids = {t.tidal_id for t in results}
        assert ids == {1, 2, 3}
        assert len(results) == 3

    def test_search_albums_resolves_full_tracks_in_parallel(self):
        client = MonochromeClient(base_urls=["https://mono.com"])
        # Two albums from the initial search.
        tracks = [
            TrackMatch(tidal_id=10, title="A1T1", artists=["Artist"], album="Album 1", album_id=101, duration_sec=180, quality="HIGH"),
            TrackMatch(tidal_id=20, title="A2T1", artists=["Artist"], album="Album 2", album_id=202, duration_sec=180, quality="HIGH"),
        ]
        client.search = MagicMock(return_value=(tracks, "https://mono.com"))

        def fake_get_album_tracks(album_id: int):
            return (
                [TrackMatch(tidal_id=album_id + 1, title=f"Track {album_id}", artists=["Artist"], album=f"Album {album_id}", duration_sec=180, quality="HIGH")],
                "ALBUM",
                ["Artist"],
            )

        with patch.object(client, "get_album_tracks", side_effect=fake_get_album_tracks) as mock_tracks:
            albums = client.search_albums("test")

        assert len(albums) == 2
        # Parallel resolver should have fetched both albums.
        assert mock_tracks.call_count == 2
        assert {a.tracks[0].tidal_id for a in albums} == {102, 203}

    def test_search_discography_resolves_full_tracks_in_parallel_and_filters(self):
        client = MonochromeClient(base_urls=["https://mono.com"])
        tracks = [
            TrackMatch(tidal_id=1, title="Hit", artists=["Daft Punk"], album="Discovery", album_id=11, duration_sec=180, quality="HIGH"),
            TrackMatch(tidal_id=2, title="Feat", artists=["Other"], album="Discovery", album_id=11, duration_sec=180, quality="HIGH"),
        ]
        client.search_paginated = MagicMock(return_value=tracks)

        def fake_get_album_tracks(album_id: int):
            return (
                [
                    TrackMatch(tidal_id=1, title="Hit", artists=["Daft Punk"], album="Discovery", album_id=11, duration_sec=180, quality="HIGH"),
                    TrackMatch(tidal_id=2, title="Feat", artists=["Other"], album="Discovery", album_id=11, duration_sec=180, quality="HIGH"),
                ],
                "ALBUM",
                ["Various Artists"],
            )

        with patch.object(client, "get_album_tracks", side_effect=fake_get_album_tracks) as mock_tracks:
            albums = client.search_discography("Daft Punk", strict=True)

        assert len(albums) == 1
        assert mock_tracks.call_count == 1
        album = albums[0]
        # Strict mode on a VA/compilation album keeps only tracks whose artists match.
        assert [t.tidal_id for t in album.tracks] == [1]


class TestDownloadPrefetch:
    """Per-album stream-URL prefetching and prefetched download_single args."""

    def _track(self, tid: int = 1, title: str = "Song") -> TrackMatch:
        return TrackMatch(
            tidal_id=tid,
            title=title,
            artists=["Artist"],
            album="Album",
            duration_sec=180,
            quality="HIGH",
        )

    def _album(self, n: int) -> AlbumMatch:
        tracks = [self._track(tid=i, title=f"Track {i}") for i in range(n)]
        return AlbumMatch(title="Album", artists=["Artist"], tracks=tracks)

    def _noop_status(self):
        return patch("monochrome_cli.console.status", return_value=MagicMock(__enter__=MagicMock(return_value=MagicMock()), __exit__=MagicMock(return_value=False)))

    def test_download_single_uses_prefetched_stream_url(self, tmp_path):
        client = MagicMock(spec=MonochromeClient)
        client.quality = "HIGH"
        client.get_stream_url = MagicMock(side_effect=RuntimeError("should not be called"))
        track = self._track()
        progress = MagicMock()

        with patch("monochrome_cli.download_file"):
            status = download_single(
                client, track, tmp_path, progress, task_id=0,
                prefetched_stream_url="http://prefetched/stream.flac",
            )

        assert status == "downloaded"
        client.get_stream_url.assert_not_called()

    def test_download_single_prefetched_error_logs_failure(self, tmp_path):
        client = MagicMock(spec=MonochromeClient)
        client.quality = "HIGH"
        track = self._track()
        progress = MagicMock()
        log: list[str] = []

        with patch("monochrome_cli.console.print") as mock_print:
            status = download_single(
                client, track, tmp_path, progress, task_id=0,
                status_log=log,
                prefetched_error=RuntimeError("prefetch failed"),
            )

        assert status == "failed"
        assert len(log) == 1
        assert "prefetch failed" in log[0]
        mock_print.assert_not_called()

    def test_download_album_prefetches_stream_urls(self, tmp_path):
        client = MagicMock(spec=MonochromeClient)
        client.quality = "HIGH"
        album = self._album(2)
        prefetched = {
            0: "http://x/0.flac",
            1: "http://x/1.flac",
        }

        with self._noop_status(), \
             patch("monochrome_cli._prefetch_stream_urls", return_value=prefetched) as mock_prefetch, \
             patch("monochrome_cli.download_file") as mock_download:
            result = download_album(client, album, tmp_path)

        assert result == 2
        mock_prefetch.assert_called_once_with(client, album.tracks, status_log=[])
        assert client.get_stream_url.call_count == 0
        # download_file should have received the prefetched stream URLs.
        urls = {call.args[0] for call in mock_download.call_args_list}
        assert urls == {"http://x/0.flac", "http://x/1.flac"}

    def test_download_album_handles_prefetch_failure(self, tmp_path):
        client = MagicMock(spec=MonochromeClient)
        client.quality = "HIGH"
        album = self._album(2)
        prefetched = {
            0: "http://x/0.flac",
            1: RuntimeError("expired"),
        }

        with self._noop_status(), \
             patch("monochrome_cli._prefetch_stream_urls", return_value=prefetched), \
             patch("monochrome_cli.download_file") as mock_download:
            result = download_album(client, album, tmp_path)

        assert result == 1
        # One success, one failure call.
        assert mock_download.call_count == 1
        failed = [call for call in mock_download.call_args_list if call.kwargs.get("prefetched_error") is not None]
        assert len(failed) == 0  # failures never reach download_file

    def test_download_album_falls_back_without_prefetch(self, tmp_path):
        client = MagicMock(spec=MonochromeClient)
        client.quality = "HIGH"
        client.get_stream_url.return_value = "http://x/fallback.flac"
        album = self._album(1)

        with self._noop_status(), \
             patch("monochrome_cli._prefetch_stream_urls", return_value={}), \
             patch("monochrome_cli.download_file"):
            result = download_album(client, album, tmp_path)

        assert result == 1
        client.get_stream_url.assert_called_once()


# ─── Run if executed directly ───

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
