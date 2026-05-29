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
    MirrorError,
    MirrorStats,
    MonochromeClient,
    TrackMatch,
    artist_matches,
    classify_error,
    normalize_artist,
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
