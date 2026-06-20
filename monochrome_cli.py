#!/usr/bin/env python3
"""
monochrome_cli.py
Search Monochrome API mirrors for tracks, albums, or artist discographies.
Standalone script — no Discord, no player backend.
"""

import argparse
import csv
import json
import os
import select
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import quote, urlsplit

import requests
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

# Cross-platform raw-mode keyboard input. We bind both names on every
# platform (the unused one becomes ``None``) so later references inside the
# picker never hit NameError when the platform-specific module is absent.
if sys.platform == "win32":
    try:
        import msvcrt  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        msvcrt = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]
else:
    import termios
    import tty
    msvcrt = None  # type: ignore[assignment]

console = Console()

DEFAULT_MONOCHROME_MIRRORS = [
    "https://monochrome-api.samidy.com",
    "https://tidal.squid.wtf",
]
DEFAULT_QOBUZ_MIRRORS = [
    "https://qobuz.kennyy.com.br",
    "https://qobuz.squid.wtf",
]
DEFAULT_QUALITY = "HIGH"
STATS_PATH = Path("mirror-stats.json")


def _load_config() -> dict[str, Any]:
    cfg_paths = [
        Path("config.json"),
        Path.home() / ".config" / "monochrome-cli" / "config.json",
    ]
    for p in cfg_paths:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
    return {}


_cfg = _load_config()
DEFAULT_OUTPUT = Path(os.path.expanduser(os.environ.get("MONOCHROME_DL_OUTPUT") or _cfg.get("output_dir") or "downloads"))
DEFAULT_CFG_MONOCHROME_MIRRORS = _cfg.get("monochrome_mirrors")
DEFAULT_CFG_QOBUZ_MIRRORS = _cfg.get("qobuz_mirrors")

# Quality resolution order: -q CLI flag > MONOCHROME_DL_QUALITY env >
# config.json "quality" key > "HIGH".
VALID_QUALITIES = ("LOW", "HIGH", "LOSSLESS", "HI_RES_LOSSLESS")


def _resolve_default_quality() -> str:
    """Pick the default stream quality from env, config, or built-in."""
    raw = os.environ.get("MONOCHROME_DL_QUALITY") or _cfg.get("quality") or "HIGH"
    raw = str(raw).upper().strip()
    if raw not in VALID_QUALITIES:
        console.print(
            f"[yellow]Unknown quality {raw!r}; falling back to HIGH. "
            f"Valid values: {', '.join(VALID_QUALITIES)}[/yellow]"
        )
        return "HIGH"
    return raw


DEFAULT_CFG_QUALITY = _resolve_default_quality()


class MirrorStats:
    def __init__(self, path: Path = STATS_PATH) -> None:
        self.path = path
        self.stats: dict[str, dict[str, int]] = {}
        self._pending = False
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.stats = json.loads(self.path.read_text())
            except Exception:
                self.stats = {}

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.stats))
            tmp.replace(self.path)
        except Exception:
            pass
        self._pending = False

    def record(self, mirror: str, success: bool) -> None:
        s = self.stats.setdefault(mirror, {"ok": 0, "total": 0})
        s["total"] += 1
        if success:
            s["ok"] += 1
        if not self._pending:
            self._pending = True
            self._save()

    def success_rate(self, mirror: str) -> float:
        s = self.stats.get(mirror)
        if s and s["total"] > 0:
            return s["ok"] / s["total"]
        return 1.0


def classify_error(err: Exception) -> tuple[str, str]:
    msg = str(err)
    if "Name or service not known" in msg or "getaddrinfo failed" in msg:
        return "network", "DNS failure"
    if "Connection refused" in msg:
        return "network", "connection refused"
    if "Connection reset" in msg:
        return "network", "connection reset"
    if "timed out" in msg.lower():
        return "network", "timeout"
    if "certificate" in msg.lower() or "ssl" in msg.lower():
        return "network", "TLS/certificate error"
    return "unknown", msg[:120]


def redact_url(url: str) -> str:
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return url.split("?")[0]


def safe_snippet(text: str) -> str:
    import re
    stripped = text.strip()
    if stripped.startswith(("<!DOCTYPE", "<html", "<head", "<body", "<div", "<h1", "<p", "<script")):
        return "[HTML response]"
    clean = re.sub(
        r'["\s]*(?:token|cookie|auth|key|secret|signature|sig)["\s]*:\s*"[^"]*"',
        '"[REDACTED]"',
        text,
        flags=re.IGNORECASE,
    )
    return clean[:200]


def sanitize_filename(name: str, max_bytes: int = 180) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, '_')
    name = name.strip()
    # Truncate by byte length (UTF-8 multi-byte safety)
    encoded = name.encode("utf-8")
    if len(encoded) > max_bytes:
        name = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
    return name


def normalize_artist(name: str) -> str:
    return name.lower().strip()


def artist_matches(query: str, track_artists: list[str]) -> bool:
    q = normalize_artist(query)
    for a in track_artists:
        if normalize_artist(a) == q:
            return True
    return False


class MirrorError(Exception):
    def __init__(self, mirror: str, category: str, detail: str, status: int | None = None, stream_error: str | None = None) -> None:
        self.mirror = mirror
        self.category = category
        self.detail = detail
        self.status = status
        self.stream_error = stream_error
        super().__init__(f"[{category}] {mirror}: {detail}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mirror": self.mirror,
            "category": self.category,
            "detail": self.detail,
            "status": self.status,
            "stream_error": self.stream_error,
        }


class TrackMatch:
    def __init__(
        self,
        tidal_id: int,
        title: str,
        artists: list[str],
        album: str,
        duration_sec: int,
        quality: str,
        isrc: str | None = None,
        album_art_url: str | None = None,
        album_type: str | None = None,
        album_id: int | None = None,
    ) -> None:
        self.tidal_id = tidal_id
        self.title = title
        self.artists = artists
        self.album = album
        self.duration_sec = duration_sec
        self.quality = quality
        self.isrc = isrc
        self.album_art_url = album_art_url
        self.album_type = album_type
        self.album_id = album_id

    def __repr__(self) -> str:
        artists = ", ".join(self.artists) or "Unknown"
        mins, secs = divmod(self.duration_sec, 60)
        return f"{artists} — {self.title} [{self.quality}] {mins}:{secs:02d}"


class AlbumMatch:
    def __init__(self, title: str, artists: list[str], tracks: list[TrackMatch], album_type: str | None = None) -> None:
        self.title = title
        self.artists = artists
        self.tracks = tracks
        self._album_type = album_type

    @property
    def display_artist(self) -> str:
        return ", ".join(self.artists) or "Unknown"

    @property
    def inferred_type(self) -> str:
        if self._album_type:
            t = self._album_type.upper()
            if t == "ALBUM":
                return "Album"
            if t == "EP":
                return "EP"
            if t == "SINGLE":
                return "Single"
            if t == "COMPILATION":
                return "Compilation"
            return t.title()
        types = [t.album_type for t in self.tracks if t.album_type]
        if types:
            return Counter(types).most_common(1)[0][0]
        t = self.title.lower()
        if any(k in t for k in ("single", "edit", "remix", "version")):
            return "Single"
        if "ep" in t.split():
            return "EP"
        return "Album"

    @property
    def quality(self) -> str:
        """Best (highest) quality available across the album's tracks.

        Returns ``"—"`` if the album has no tracks or none carry a quality
        tag. Quality ordering: ``LOW < HIGH < LOSSLESS < HI_RES_LOSSLESS``.
        """
        if not self.tracks:
            return "—"
        rank = {"LOW": 0, "HIGH": 1, "LOSSLESS": 2, "HI_RES_LOSSLESS": 3}
        best = -1
        result = "—"
        for t in self.tracks:
            q = t.quality
            if not q:
                continue
            r = rank.get(q.upper(), -1)
            if r > best:
                best = r
                result = q
        return result

    def __repr__(self) -> str:
        return f"[{self.inferred_type}] {self.display_artist} — {self.title} ({len(self.tracks)} tracks)"


class MonochromeClient:
    def __init__(
        self,
        base_urls: list[str] | None = None,
        quality: str = DEFAULT_QUALITY,
        qobuz_urls: list[str] | None = None,
    ) -> None:
        # Merge defaults with user-provided URLs; defaults first, then extras
        mono = list(dict.fromkeys(DEFAULT_MONOCHROME_MIRRORS + (base_urls or [])))
        self.base_urls = [u.rstrip("/") for u in mono]
        self.quality = quality
        qob = list(dict.fromkeys(DEFAULT_QOBUZ_MIRRORS + (qobuz_urls or [])))
        self.qobuz_urls = [u.rstrip("/") for u in qob]
        self.stats = MirrorStats()
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def _sorted_mirrors(self) -> list[str]:
        return sorted(self.base_urls, key=lambda u: self.stats.success_rate(u), reverse=True)

    def _request_any(self, path: str, preferred: str | None = None) -> tuple[dict[str, Any], str]:
        errors: list[dict[str, Any]] = []
        used_mirror: str = ""

        def try_one(base: str) -> dict[str, Any]:
            nonlocal used_mirror
            url = f"{base}{path}"
            try:
                resp = self.session.get(url, timeout=15)
            except Exception as exc:
                self.stats.record(base, False)
                cat, detail = classify_error(exc)
                raise MirrorError(mirror=redact_url(base), category=cat, detail=detail)

            if not resp.ok:
                self.stats.record(base, False)
                if resp.status_code == 401 and "Token refresh failed" in resp.text:
                    raise MirrorError(
                        mirror=redact_url(base),
                        category="http",
                        status=401,
                        detail="Token refresh failed",
                        stream_error="CREDENTIAL_EXPIRED",
                    )
                ct = resp.headers.get("Content-Type", "").lower()
                if "html" in ct:
                    detail = f"HTTP {resp.status_code} (returned HTML page)"
                else:
                    detail = safe_snippet(resp.text) or f"HTTP {resp.status_code}"
                raise MirrorError(
                    mirror=redact_url(base),
                    category="http",
                    status=resp.status_code,
                    detail=detail,
                )

            try:
                data = resp.json()
            except Exception:
                self.stats.record(base, False)
                ct = resp.headers.get("Content-Type", "").lower()
                if "html" in ct:
                    detail = "invalid JSON (returned HTML page)"
                else:
                    detail = f"invalid JSON: {safe_snippet(resp.text)}"
                raise MirrorError(
                    mirror=redact_url(base),
                    category="parse",
                    detail=detail,
                )

            self.stats.record(base, True)
            used_mirror = base
            return data

        # Try preferred mirror first if given
        bases = self._sorted_mirrors()
        if preferred and preferred in bases:
            bases = [preferred] + [b for b in bases if b != preferred]

        with ThreadPoolExecutor() as ex:
            futures = {ex.submit(try_one, base): base for base in bases}
            for future in as_completed(futures):
                try:
                    data = future.result()
                    return data, used_mirror
                except MirrorError as me:
                    errors.append(me.to_dict())

        console.print(f"[red][monochrome] All {len(errors)} monochrome mirrors failed for {path}[/red]")
        for e in errors:
            status = f" ({e.get('status')})" if e.get("status") else ""
            console.print(f"  [red]{e['mirror']}: [{e['category']}]{status} {e['detail']}[/red]")
        raise RuntimeError(f"All {len(errors)} monochrome mirrors failed for {path}")

    def search(self, query: str, limit: int = 8, offset: int = 0, preferred_mirror: str | None = None) -> tuple[list[TrackMatch], str | None]:
        path = f"/search/?s={quote(query)}&limit={limit}&offset={offset}"
        data, used_mirror = self._request_any(path, preferred=preferred_mirror)
        items = data.get("data", {}).get("items", [])
        if not isinstance(items, list):
            return [], None
        results: list[TrackMatch] = []
        for item in items:
            cover = item.get("album", {}).get("cover") or item.get("cover")
            album_art = None
            if isinstance(cover, str) and cover:
                album_art = f"https://resources.tidal.com/images/{cover.replace('-', '/')}/320x320.jpg"
            results.append(
                TrackMatch(
                    tidal_id=item["id"],
                    title=item.get("title", "Unknown"),
                    artists=[a.get("name", "Unknown") for a in item.get("artists", [])]
                    or ([item.get("artist", {}).get("name")] if item.get("artist", {}).get("name") else []),
                    album=item.get("album", {}).get("title", "Unknown"),
                    duration_sec=item.get("duration", 0),
                    quality=item.get("audioQuality", "UNKNOWN"),
                    isrc=item.get("isrc") if isinstance(item.get("isrc"), str) else None,
                    album_art_url=album_art,
                    album_type=item.get("album", {}).get("type"),
                    album_id=item.get("album", {}).get("id"),
                )
            )
        return results, used_mirror

    def search_paginated(self, query: str, limit: int = 50, max_pages: int = 5) -> list[TrackMatch]:
        all_tracks: list[TrackMatch] = []
        seen_ids: set[int] = set()
        for page in range(max_pages):
            offset = page * limit
            # Query all mirrors in parallel and merge results for maximum coverage
            page_tracks: dict[int, TrackMatch] = {}
            for base in self.base_urls:
                try:
                    resp = self.session.get(
                        f"{base}/search/?s={quote(query)}&limit={limit}&offset={offset}",
                        timeout=15,
                    )
                    if not resp.ok:
                        continue
                    data = resp.json()
                    items = data.get("data", {}).get("items", [])
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        tid = item["id"]
                        if tid in seen_ids or tid in page_tracks:
                            continue
                        cover = item.get("album", {}).get("cover") or item.get("cover")
                        album_art = None
                        if isinstance(cover, str) and cover:
                            album_art = f"https://resources.tidal.com/images/{cover.replace('-', '/')}/320x320.jpg"
                        page_tracks[tid] = TrackMatch(
                            tidal_id=tid,
                            title=item.get("title", "Unknown"),
                            artists=[a.get("name", "Unknown") for a in item.get("artists", [])]
                            or ([item.get("artist", {}).get("name")] if item.get("artist", {}).get("name") else []),
                            album=item.get("album", {}).get("title", "Unknown"),
                            duration_sec=item.get("duration", 0),
                            quality=item.get("audioQuality", "UNKNOWN"),
                            isrc=item.get("isrc") if isinstance(item.get("isrc"), str) else None,
                            album_art_url=album_art,
                            album_type=item.get("album", {}).get("type"),
                            album_id=item.get("album", {}).get("id"),
                        )
                except Exception:
                    continue
            if not page_tracks:
                break
            all_tracks.extend(page_tracks.values())
            seen_ids.update(page_tracks.keys())
        return all_tracks

    def search_albums(self, query: str, limit: int = 16) -> list[AlbumMatch]:
        tracks, _ = self.search(query, limit=limit)
        albums: dict[str, AlbumMatch] = {}
        for t in tracks:
            key = str(t.album_id) if t.album_id else f"{t.album}::{','.join(t.artists)}"
            if key not in albums:
                albums[key] = AlbumMatch(title=t.album, artists=t.artists, tracks=[])
            albums[key].tracks.append(t)

        # Fetch full track listings via /album endpoint for accurate counts
        full_albums: list[AlbumMatch] = []
        for album in albums.values():
            album_id = album.tracks[0].album_id if album.tracks else None
            if album_id:
                try:
                    full_tracks, album_type, album_artists = self.get_album_tracks(album_id)
                    if full_tracks:
                        artists = album_artists if album_artists else album.artists
                        full_albums.append(AlbumMatch(title=album.title, artists=artists, tracks=full_tracks, album_type=album_type))
                        continue
                except Exception as exc:
                    console.print(f"[yellow][album] Failed to fetch full tracks for '{album.title}': {exc}[/yellow]")
            full_albums.append(album)
        return full_albums

    def get_album_tracks(self, album_id: int) -> tuple[list[TrackMatch], str | None, list[str]]:
        data, _ = self._request_any(f"/album/?id={album_id}")
        album_data = data.get("data", {})
        items = album_data.get("items", [])
        album_type = album_data.get("type")
        album_artists: list[str] = []
        if album_data.get("artists"):
            album_artists = [a.get("name", "Unknown") for a in album_data["artists"] if a.get("name")]
        elif album_data.get("artist", {}).get("name"):
            album_artists = [album_data["artist"]["name"]]
        results: list[TrackMatch] = []
        for it in items:
            item = it.get("item", {}) if isinstance(it, dict) else {}
            if not item:
                continue
            cover = item.get("album", {}).get("cover") or item.get("cover")
            album_art = None
            if isinstance(cover, str) and cover:
                album_art = f"https://resources.tidal.com/images/{cover.replace('-', '/')}/320x320.jpg"
            results.append(
                TrackMatch(
                    tidal_id=item["id"],
                    title=item.get("title", "Unknown"),
                    artists=[a.get("name", "Unknown") for a in item.get("artists", [])]
                    or ([item.get("artist", {}).get("name")] if item.get("artist", {}).get("name") else []),
                    album=item.get("album", {}).get("title", "Unknown"),
                    duration_sec=item.get("duration", 0),
                    quality=item.get("audioQuality", "UNKNOWN"),
                    isrc=item.get("isrc") if isinstance(item.get("isrc"), str) else None,
                    album_art_url=album_art,
                    album_type=item.get("album", {}).get("type"),
                    album_id=item.get("album", {}).get("id"),
                )
            )
        return results, album_type, album_artists

    def search_discography(self, artist: str, limit: int = 50, max_pages: int = 5, strict: bool = True) -> list[AlbumMatch]:
        tracks = self.search_paginated(artist, limit=limit, max_pages=max_pages)
        console.print(f"[dim][discography] Raw search returned {len(tracks)} track(s)[/dim]")

        if strict:
            filtered = [t for t in tracks if artist_matches(artist, t.artists)]
        else:
            filtered = tracks
        console.print(f"[dim][discography] After artist filter: {len(filtered)} track(s)[/dim]")

        albums: dict[str, AlbumMatch] = {}
        for t in filtered:
            key = str(t.album_id) if t.album_id else f"{t.album}::{','.join(t.artists)}"
            if key not in albums:
                albums[key] = AlbumMatch(title=t.album, artists=t.artists, tracks=[])
            albums[key].tracks.append(t)

        # Fetch full track listings via /album endpoint for accurate counts
        full_albums: list[AlbumMatch] = []
        for album in albums.values():
            album_id = album.tracks[0].album_id if album.tracks else None
            if album_id:
                try:
                    full_tracks, album_type, album_artists = self.get_album_tracks(album_id)
                    if full_tracks:
                        artists = album_artists if album_artists else album.artists
                        # If album's primary artist doesn't match the searched artist,
                        # it's a compilation/VA album — keep only tracks where the artist appears
                        if strict and not artist_matches(artist, artists):
                            full_tracks = [t for t in full_tracks if artist_matches(artist, t.artists)]
                            if not full_tracks:
                                continue
                        full_albums.append(AlbumMatch(title=album.title, artists=artists, tracks=full_tracks, album_type=album_type))
                        continue
                except Exception as exc:
                    console.print(f"[yellow][album] Failed to fetch full tracks for '{album.title}': {exc}[/yellow]")
            full_albums.append(album)

        console.print(f"[dim][discography] Grouped into {len(full_albums)} album(s)[/dim]")
        return full_albums

    def get_stream_url(self, tidal_id: int, quality: str | None = None, isrc: str | None = None) -> str:
        q = quality or self.quality
        path = f"/track/?id={tidal_id}&quality={q}"

        # Try monochrome mirrors first
        mono_data: dict[str, Any] | None = None
        mono_errors: int = 0
        try:
            mono_data, _ = self._request_any(path)
            url = self._extract_stream_url(mono_data)
            if url:
                return url
        except RuntimeError as exc:
            # All monochrome mirrors failed; extract how many were tried
            msg = str(exc)
            if "mirrors failed" in msg.lower():
                parts = msg.split()
                for i, part in enumerate(parts):
                    if part.isdigit() and i > 0 and parts[i - 1].lower() == "all":
                        mono_errors = int(part)
                        break

        # Fallback to Qobuz when monochrome fails or returns no playable URL
        if isrc and self.qobuz_urls:
            console.print(f"[yellow][fallback] Trying {len(self.qobuz_urls)} Qobuz mirror(s) for ISRC {isrc}...[/yellow]")
            qobuz_url = self._try_qobuz(isrc, q)
            if qobuz_url:
                return qobuz_url
            console.print(f"[red][fallback] All {len(self.qobuz_urls)} Qobuz mirror(s) failed for ISRC {isrc}[/red]")
        elif not self.qobuz_urls:
            console.print(f"[yellow][fallback] No Qobuz mirrors configured — skipping fallback[/yellow]")
        elif not isrc:
            console.print(f"[yellow][fallback] No ISRC available for track {tidal_id} — skipping Qobuz fallback[/yellow]")

        # If monochrome succeeded but returned no stream URL, diagnose and raise
        if mono_data is not None:
            reason = self._classify_missing(mono_data)
            self._log_diag(tidal_id, mono_data, path, reason)
            exc = RuntimeError(f"No playable URL for track {tidal_id}")
            exc.reason = reason
            raise exc

        # All monochrome mirrors failed and no Qobuz fallback succeeded
        total_tried = f"{mono_errors} monochrome + {len(self.qobuz_urls) if self.qobuz_urls else 0} Qobuz" if self.qobuz_urls else f"{mono_errors} monochrome (no Qobuz fallback)"
        raise RuntimeError(f"All mirrors failed for {path} ({total_tried})")

    def _quality_to_qobuz(self, quality: str) -> str:
        q = quality.upper()
        if q == "HI_RES_LOSSLESS":
            return "27"
        if q == "LOSSLESS":
            return "6"
        return "5"

    def _try_qobuz(self, isrc: str, quality: str) -> str | None:
        qobuz_q = self._quality_to_qobuz(quality)
        for base in self.qobuz_urls:
            try:
                search_resp = self.session.get(
                    f"{base}/api/get-music?q={quote(isrc)}&offset=0",
                    timeout=8,
                )
                if not search_resp.ok:
                    continue
                search_json = search_resp.json()
                tracks = search_json.get("data", {}).get("tracks", {}).get("items", [])
                match = None
                for t in tracks:
                    if t.get("isrc", "").lower() == isrc.lower():
                        match = t
                        break
                if not match and tracks:
                    match = tracks[0]
                if not match or not match.get("id"):
                    continue

                stream_resp = self.session.get(
                    f"{base}/api/download-music?track_id={match['id']}&quality={qobuz_q}",
                    timeout=8,
                )
                if not stream_resp.ok:
                    continue
                stream_json = stream_resp.json()
                url = stream_json.get("data", {}).get("url")
                if isinstance(url, str) and url.startswith("http"):
                    console.print(f"[green][qobuz] stream resolved via {redact_url(base)}[/green]")
                    return url
            except Exception as exc:
                console.print(f"[yellow][qobuz] {redact_url(base)} failed for ISRC {isrc}: {exc}[/yellow]")
        return None

    def _extract_stream_url(self, data: dict[str, Any]) -> str | None:
        candidates = [
            data.get("url"),
            data.get("stream_url"),
            data.get("streamUrl"),
        ]
        nested = data.get("data", {})
        if isinstance(nested, dict):
            manifest = nested.get("manifest")
            if isinstance(manifest, str):
                parsed = None
                try:
                    parsed = json.loads(manifest)
                except Exception:
                    try:
                        parsed = json.loads(__import__("base64").b64decode(manifest).decode("utf-8"))
                    except Exception:
                        if manifest.startswith("http"):
                            candidates.append(manifest)
                if isinstance(parsed, dict):
                    urls = parsed.get("urls", [])
                    entry = urls[0] if urls else None
                    url_from_entry = entry if isinstance(entry, str) else (entry.get("url") if isinstance(entry, dict) else None)
                    segment = parsed.get("encryptedMediaUrl") or url_from_entry or parsed.get("url")
                    if isinstance(segment, str):
                        candidates.append(segment)
            candidates.extend([nested.get("url"), nested.get("stream_url"), nested.get("streamUrl")])
        for c in candidates:
            if isinstance(c, str) and c.startswith("http"):
                return c
        return None

    def _classify_missing(self, data: dict[str, Any]) -> str:
        nested = data.get("data", {})
        if isinstance(nested, dict):
            if nested.get("assetPresentation") == "PREVIEW":
                return "PREVIEW_ONLY"
            detail = nested.get("detail") or data.get("detail")
            if isinstance(detail, str):
                lower = detail.lower()
                if any(k in lower for k in ("subscription", "payment", "full-access", "full access")):
                    return "REQUIRES_SUBSCRIPTION"
            if isinstance(nested.get("manifest"), str):
                return "MANIFEST_ONLY"
            if isinstance(detail, str) and detail:
                return "UPSTREAM_ERROR"
        if not data or not isinstance(data, dict) or len(data) == 0:
            return "EMPTY_RESPONSE"
        return "MISSING_STREAM_URL"

    def _log_diag(self, tidal_id: int, data: dict[str, Any], path: str | None = None, reason: str | None = None) -> None:
        top_keys = list(data.keys())
        nested = data.get("data", {})
        nested_keys = list(nested.keys()) if isinstance(nested, dict) else []
        console.print(f"[red][monochrome] Track {tidal_id} — no playable URL found[/red]")
        if path:
            console.print(f"  [dim]endpoint: {path}[/dim]")
        if reason:
            console.print(f"  [dim]failure category: {reason}[/dim]")
        console.print(f"  [dim]top-level keys: {top_keys}[/dim]")
        if nested_keys:
            console.print(f"  [dim]data keys: {nested_keys}[/dim]")
        if isinstance(nested, dict):
            if nested.get("assetPresentation"):
                console.print(f"  [dim]assetPresentation: {nested['assetPresentation']}[/dim]")
            if nested.get("audioQuality"):
                console.print(f"  [dim]audioQuality: {nested['audioQuality']}[/dim]")
            if nested.get("manifestMimeType"):
                console.print(f"  [dim]manifestMimeType: {nested['manifestMimeType']}[/dim]")
            if isinstance(nested.get("manifest"), str):
                console.print("  [dim]manifest present (DASH/encrypted?) — no direct stream URL extracted[/dim]")
            detail = nested.get("detail")
            if detail:
                console.print(f"  [dim]API detail: {str(detail)[:150]}[/dim]")


# Map of internal format names to the on-disk file extension we use for them.
EXT_FOR_FORMAT: dict[str, str] = {
    "flac": ".flac",
    "m4a":  ".m4a",
    "mp3":  ".mp3",
}


def _detect_format_from_content_type(content_type: str | None) -> str | None:
    """Return the audio format implied by an HTTP ``Content-Type`` header.

    Returns one of ``"flac"``, ``"m4a"``, ``"mp3"``, or ``None`` when the header
    is missing or does not point at a known audio container.
    """
    if not content_type:
        return None
    ct = content_type.lower().split(";", 1)[0].strip()
    if ct in {"audio/flac", "audio/x-flac", "application/flac", "flac"}:
        return "flac"
    if ct in {"audio/mp4", "audio/m4a", "audio/x-m4a", "audio/mp4a-latm",
              "audio/aac", "audio/x-aac"}:
        return "m4a"
    if ct in {"audio/mpeg", "audio/mp3", "audio/mpeg3", "audio/x-mpeg", "audio/x-mp3"}:
        return "mp3"
    # Looser fallbacks: anything mentioning these substrings is good enough.
    if "flac" in ct:
        return "flac"
    if "mp4" in ct or "m4a" in ct or "aac" in ct:
        return "m4a"
    if "mpeg" in ct or "mp3" in ct:
        return "mp3"
    return None


def _detect_format_from_bytes(data: bytes) -> str | None:
    """Sniff the leading bytes of an audio file.

    Recognises native FLAC (``fLaC`` magic), ISO BMFF (any ``ftyp`` box —
    treated as ``m4a`` for music downloads), and MP3 (an ``ID3`` tag or the
    11-bit-all-ones frame sync). Returns ``None`` for unrecognised data.
    """
    if len(data) >= 4 and data[:4] == b"fLaC":
        return "flac"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return "m4a"
    if len(data) >= 3 and data[:3] == b"ID3":
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    return None


def _existing_audio(base: Path) -> Path | None:
    """Return an existing audio file at ``base`` under any supported extension.

    ``base`` is a *stem* (no extension); we check for ``base.flac``,
    ``base.m4a``, ``base.mp3`` and return the first hit. This is used by the
    downloader to skip tracks that have already been saved — regardless of
    which container the mirror served last time.
    """
    for ext in EXT_FOR_FORMAT.values():
        try:
            p = base.with_suffix(ext)
        except OSError:
            continue
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


def download_file(url: str, base: Path, progress: Progress, task_id: int, chunk_size: int = 8192) -> Path:
    """Stream ``url`` to ``base.<ext>`` where ``<ext>`` is auto-detected.

    The container is determined from the response's ``Content-Type`` header
    and verified with a magic-byte sniff of the saved file. The download is
    written atomically to a sibling ``.tmp`` file and renamed on success, so
    a misnamed file is never left on disk.

    Returns the final on-disk path (which may differ from ``base`` if a
    different container was served). Raises on any I/O or HTTP error; partial
    temp files are cleaned up.
    """
    base.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    head_ct = _detect_format_from_content_type(resp.headers.get("Content-Type", ""))
    total = int(resp.headers.get("content-length", 0))
    if total:
        progress.update(task_id, total=total)

    tmp: Path | None = None
    head = bytearray()
    file_obj = None
    try:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            if len(head) < 16:
                head.extend(chunk[: 16 - len(head)])
            if file_obj is None:
                detected = _detect_format_from_bytes(bytes(head)) or head_ct or "flac"
                if detected not in EXT_FOR_FORMAT:
                    detected = "flac"
                ext = EXT_FOR_FORMAT[detected]
                tmp = base.with_suffix(ext + ".tmp")
                file_obj = open(tmp, "wb")
            file_obj.write(chunk)
            progress.update(task_id, advance=len(chunk))
    finally:
        if file_obj is not None:
            try:
                file_obj.close()
            except OSError:
                pass
        try:
            resp.close()
        except Exception:
            pass

    if tmp is None:
        raise RuntimeError(f"empty response body for {url}")

    try:
        with open(tmp, "rb") as fh:
            first = fh.read(16)
        detected = _detect_format_from_bytes(first) or head_ct or "flac"
        if detected not in EXT_FOR_FORMAT:
            detected = "flac"
        final_ext = EXT_FOR_FORMAT[detected]
        final_path = base.with_suffix(final_ext)

        # A file already exists at the final path — drop the new download and
        # report the existing one (caller treats it as a skip).
        if final_path.exists() and final_path != tmp:
            tmp.unlink(missing_ok=True)
            return final_path

        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(final_path)
        return final_path
    except BaseException:
        # Any failure after writing the temp file: clean it up.
        try:
            if tmp is not None and Path(tmp).exists():
                Path(tmp).unlink()
        except OSError:
            pass
        raise


def fix_extensions(root: Path) -> tuple[int, int]:
    """Walk ``root`` and rename files whose extension does not match their
    actual container.

    Sniffs the first 16 bytes of every regular file under ``root`` and, if a
    known audio format is detected, renames the file to the matching extension
    (``.flac``/``.m4a``/``.mp3``). Files that are already correctly named,
    whose content is not a recognised audio format, or whose target path is
    already occupied, are left alone. Returns ``(renamed, scanned)`` counts.
    """
    renamed = 0
    scanned = 0
    for path in sorted(root.rglob("*")):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        scanned += 1
        try:
            with open(path, "rb") as f:
                head = f.read(16)
        except OSError:
            continue
        detected = _detect_format_from_bytes(head)
        if not detected:
            continue
        expected_ext = EXT_FOR_FORMAT[detected]
        if path.suffix.lower() == expected_ext.lower():
            continue
        target = path.with_suffix(expected_ext)
        if target.exists():
            console.print(f"[yellow][skip] {path}: target already exists ({target.name})[/yellow]")
            continue
        try:
            path.rename(target)
        except OSError as exc:
            console.print(f"[red][skip] {path}: {exc}[/red]")
            continue
        renamed += 1
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        console.print(f"[dim]{rel}[/dim] → [green]{target.name}[/green]")
    return renamed, scanned


def pick_track(tracks: list[TrackMatch]) -> TrackMatch | None:
    if not tracks:
        console.print("[yellow]No tracks found.[/yellow]")
        return None
    table = Table(title=f"Found {len(tracks)} track(s)")
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("Artist", style="green")
    table.add_column("Title", style="blue")
    table.add_column("Album", style="magenta")
    table.add_column("Quality", style="yellow")
    for i, t in enumerate(tracks, 1):
        table.add_row(str(i), ", ".join(t.artists), t.title, t.album, t.quality)
    console.print(table)
    while True:
        choice = IntPrompt.ask("Pick a number (0 to quit)", default=0)
        if choice == 0:
            return None
        if 1 <= choice <= len(tracks):
            return tracks[choice - 1]
        console.print("[red]Invalid choice.[/red]")


def _parse_album_selection(raw: str, total: int) -> list[int] | None:
    """Parse a user selection string into a sorted, de-duplicated list of 1-based
    album indices.

    Accepted forms (mixed freely, whitespace or commas as separators):
      - ``0`` / ``""`` / ``"q"`` / ``"quit"`` → ``None`` (cancel)
      - ``"all"`` / ``"*"``                  → all indices ``[1..total]``
      - ``"3"``                              → ``[3]``
      - ``"1,3,5"`` or ``"1 3 5"``           → ``[1, 3, 5]``
      - ``"1-3"``                            → ``[1, 2, 3]``

    Returns ``None`` to signal cancellation, or raises ``ValueError`` for any
    token that cannot be parsed or that is out of range.
    """
    if total <= 0:
        return None

    text = (raw or "").strip().lower()
    if text in ("", "0", "q", "quit", "cancel"):
        return None
    if text in ("all", "*", "a"):
        return list(range(1, total + 1))

    # Normalize separators: commas and whitespace both split tokens.
    text = text.replace(",", " ")
    tokens = [t for t in text.split() if t]

    indices: set[int] = set()
    for tok in tokens:
        if "-" in tok:
            head, _, tail = tok.partition("-")
            if not head or not tail:
                raise ValueError(f"invalid range: {tok!r}")
            try:
                start = int(head)
                end = int(tail)
            except ValueError as exc:
                raise ValueError(f"not a number: {tok!r}") from exc
            if start > end:
                start, end = end, start
            if start < 1 or end > total:
                raise ValueError(f"out of range: {tok!r} (valid: 1-{total})")
            indices.update(range(start, end + 1))
        else:
            try:
                n = int(tok)
            except ValueError as exc:
                raise ValueError(f"not a number: {tok!r}") from exc
            if n < 1 or n > total:
                raise ValueError(f"out of range: {n} (valid: 1-{total})")
            indices.add(n)

    if not indices:
        return None
    return sorted(indices)


def pick_albums(albums: list[AlbumMatch]) -> list[AlbumMatch]:
    """Interactively let the user choose one or more albums from ``albums``.

    Returns the selected albums in display order, or an empty list if the user
    cancelled. Supports comma-separated numbers, ranges (``1-3``), ``all``,
    and a bare ``0``/empty input to cancel.
    """
    if not albums:
        console.print("[yellow]No albums found.[/yellow]")
        return []
    albums = sorted(albums, key=lambda a: len(a.tracks), reverse=True)
    table = Table(title=f"Found {len(albums)} album(s)")
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("Type", style="magenta")
    table.add_column("Album", style="blue")
    table.add_column("Tracks", style="yellow")
    table.add_column("Artist", style="green")
    table.add_column("Quality", style="cyan", no_wrap=True)
    for i, a in enumerate(albums, 1):
        table.add_row(
            str(i),
            a.inferred_type,
            a.title,
            str(len(a.tracks)),
            a.display_artist,
            a.quality,
        )
    console.print(table)

    help_text = (
        "Pick one or more (e.g. [bold]1,3,5[/bold] or [bold]1-3[/bold] or [bold]all[/bold]; "
        "[bold]0[/bold] to quit)"
    )
    while True:
        raw = Prompt.ask(help_text, default="").strip()
        try:
            indices = _parse_album_selection(raw, len(albums))
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            continue
        if indices is None:
            return []
        return [albums[i - 1] for i in indices]


def pick_album(albums: list[AlbumMatch]) -> AlbumMatch | None:
    """Backwards-compatible single-album picker. Returns ``None`` on cancel."""
    selected = pick_albums(albums)
    return selected[0] if selected else None


def _note(log: list[str] | None, message: str) -> None:
    """Append ``message`` to ``log`` if provided, else print to ``console``.

    Used by ``download_single`` to defer per-track status messages until after
    the live progress region closes — avoids interleaving prints with the
    Live display and producing a scrolling wall of text.
    """
    if log is None:
        console.print(message)
    else:
        log.append(message)


def download_single(
    client: MonochromeClient,
    track: TrackMatch,
    output_dir: Path,
    progress: Progress,
    task_id: int,
    status_log: list[str] | None = None,
) -> str:
    try:
        stream_url = client.get_stream_url(track.tidal_id, quality=client.quality, isrc=track.isrc)
    except Exception as exc:
        _note(status_log, f"[red][skip] {track.title}: failed to get stream URL: {exc}[/red]")
        return "failed"

    raw_name = f"{', '.join(track.artists)} - {track.title}"
    safe_name = sanitize_filename(raw_name)
    base = output_dir / safe_name
    try:
        if _existing_audio(base) is not None:
            _note(status_log, f"[yellow][skip] {track.title}: already exists[/yellow]")
            return "skipped"
    except OSError:
        # Path too long — try progressively shorter names
        for short_len in (120, 80, 50, 30):
            short_name = sanitize_filename(raw_name, max_bytes=short_len)
            short_base = output_dir / short_name
            try:
                if _existing_audio(short_base) is not None:
                    _note(status_log, f"[yellow][skip] {track.title}: already exists[/yellow]")
                    return "skipped"
                base = short_base
                break
            except OSError:
                continue
        else:
            _note(status_log, f"[red][skip] {track.title}: path too long[/red]")
            return "failed"

    try:
        download_file(stream_url, base, progress, task_id)
        progress.update(task_id, description=f"[green]✓ {track.title}")
        return "downloaded"
    except KeyboardInterrupt:
        progress.update(task_id, description=f"[red]✗ {track.title}")
        _note(status_log, f"[yellow]Interrupted: {track.title}[/yellow]")
        raise
    except Exception as exc:
        progress.update(task_id, description=f"[red]✗ {track.title}")
        _note(status_log, f"[red][skip] {track.title}: download failed: {exc}[/red]")
        return "failed"


def _format_counters(downloaded: int, skipped: int, failed: int) -> str:
    """Render the inline counter column used in the album overall progress bar."""
    return (
        f"  [green]✓{downloaded}[/green]  "
        f"[yellow]⊘{skipped}[/yellow]  "
        f"[red]✗{failed}[/red]"
    )


def _print_notes_table(notes: list[str], title: str = "Notes") -> None:
    """Render collected per-track status messages as a table, or skip if empty."""
    if not notes:
        return
    table = Table(title=title, show_header=False, box=None, padding=(0, 1))
    table.add_column("#", style="dim", justify="right")
    table.add_column("Detail")
    for i, msg in enumerate(notes, 1):
        # Strip rich markup wrappers for the table cell — let the table render the row plainly.
        table.add_row(str(i), msg)
    console.print(table)


def download_album(
    client: MonochromeClient,
    album: AlbumMatch,
    base_dir: Path,
    artist_folder: str | None = None,
    album_index: tuple[int, int] | None = None,
) -> int:
    artist_dir = sanitize_filename(artist_folder) if artist_folder else sanitize_filename(album.display_artist)
    album_dir = sanitize_filename(album.title)
    out = base_dir / artist_dir / album_dir
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Directory path too long — try shorter names
        for short_len in (120, 80, 50):
            artist_dir = sanitize_filename(artist_folder, max_bytes=short_len) if artist_folder else sanitize_filename(album.display_artist, max_bytes=short_len)
            album_dir = sanitize_filename(album.title, max_bytes=short_len)
            out = base_dir / artist_dir / album_dir
            try:
                out.mkdir(parents=True, exist_ok=True)
                break
            except OSError:
                continue
        else:
            console.print(f"[red][skip] Album path too long: {album.title}[/red]")
            return 0

    title = f"[bold cyan]Downloading {album.inferred_type}[/bold cyan]"
    if album_index is not None:
        i, n = album_index
        title = f"[bold cyan]Album {i}/{n} — {album.inferred_type}[/bold cyan]"

    console.print(Panel(
        f"[bold]{album.title}[/bold] by {album.display_artist}\n"
        f"[dim]{len(album.tracks)} track(s) → {out}[/dim]",
        title=title,
        border_style="cyan",
    ))

    total = len(album.tracks)
    downloaded = skipped = failed = 0
    notes: list[str] = []
    overall_progress = Progress(
        TextColumn("[bold cyan]Overall [/bold cyan]"),
        BarColumn(bar_width=24),
        MofNCompleteColumn(),
        TextColumn("{task.description}"),
        console=console,
    )
    track_progress = Progress(
        TextColumn("[cyan]Track   [/cyan]"),
        TextColumn("{task.description}"),
        BarColumn(bar_width=24),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )

    interrupted = False
    try:
        with Live(
            Group(overall_progress, track_progress),
            console=console,
            refresh_per_second=10,
            transient=False,
        ):
            overall_task = overall_progress.add_task(
                _format_counters(0, 0, 0), total=total
            )
            track_task = track_progress.add_task("", total=None)
            for idx, track in enumerate(album.tracks, 1):
                track_progress.update(
                    track_task,
                    description=f"[{idx}/{total}] {track.title}",
                    total=None,
                )
                status = download_single(
                    client, track, out, track_progress, track_task, status_log=notes
                )
                if status == "downloaded":
                    downloaded += 1
                    track_progress.update(
                        track_task,
                        description=f"[green]✓ [{idx}/{total}] {track.title}[/green]",
                    )
                elif status == "skipped":
                    skipped += 1
                    track_progress.update(
                        track_task,
                        description=f"[yellow]⊘ [{idx}/{total}] {track.title}[/yellow]",
                    )
                else:
                    failed += 1
                    track_progress.update(
                        track_task,
                        description=f"[red]✗ [{idx}/{total}] {track.title}[/red]",
                    )
                overall_progress.update(
                    overall_task,
                    advance=1,
                    description=_format_counters(downloaded, skipped, failed),
                )
    except KeyboardInterrupt:
        interrupted = True
        notes.append("[yellow]Interrupted by user.[/yellow]")

    # Per-album summary table removed — the "Overall" and "Track" progress
    # bars already render ✓N / ⊘N / ✗N counts for the user. Per-track
    # diagnostic notes are still printed so the user can see *which* tracks
    # were skipped or failed.
    _print_notes_table(notes, title="Per-track notes")
    console.print()
    return downloaded if not interrupted else downloaded


def download_albums(
    client: MonochromeClient,
    selected: list[AlbumMatch],
    base_dir: Path,
    *,
    artist_folder: str | None = None,
    summary_title: str = "Multi-Album Summary",
) -> int:
    """Download a sequence of pre-selected albums in order with a final
    summary table. Single-album selections skip the summary and use the
    regular per-album progress display.

    Returns the number of tracks successfully downloaded.
    """
    if not selected:
        return 0
    if len(selected) == 1:
        return download_album(
            client,
            selected[0],
            base_dir,
            artist_folder=artist_folder,
            album_index=(1, 1),
        )
    total_downloaded = 0
    total_tracks = sum(len(a.tracks) for a in selected)
    try:
        for i, album in enumerate(selected, 1):
            total_downloaded += download_album(
                client,
                album,
                base_dir,
                artist_folder=artist_folder,
                album_index=(i, len(selected)),
            )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")

    summary = Table(
        title=f"{summary_title}: {len(selected)} album(s) selected",
        show_header=True,
    )
    summary.add_column("Status", style="bold")
    summary.add_column("Count", justify="right")
    summary.add_row(
        f"[green]Downloaded[/green] ({len(selected)} albums, {total_tracks} tracks)",
        str(total_downloaded),
    )
    console.print(summary)
    console.print()
    return total_downloaded


def download_discography(
    client: MonochromeClient,
    albums: list[AlbumMatch],
    base_dir: Path,
    artist_query: str,
    *,
    force_tui: bool | None = None,
) -> int:
    """Run a discography search result through the album picker (TUI by
    default) and download the selection. Returns 0 on empty / cancelled
    selection; otherwise the number of tracks downloaded.
    """
    selected = select_albums(albums, force_tui=force_tui)
    if not selected:
        return 0
    return download_albums(
        client,
        selected,
        base_dir,
        artist_folder=artist_query,
        summary_title="Discography Summary",
    )


def _format_csv_counters(downloaded: int, skipped: int, failed: int, missing: int) -> str:
    return (
        f"  [green]✓{downloaded}[/green]  "
        f"[yellow]⊘{skipped}[/yellow]  "
        f"[red]✗{failed}[/red]  "
        f"[magenta]?{missing}[/magenta]"
    )


def download_from_csv(client: MonochromeClient, csv_path: Path, output_dir: Path) -> int:
    if not csv_path.exists():
        console.print(f"[red]CSV file not found: {csv_path}[/red]")
        return 0

    out = output_dir / sanitize_filename(csv_path.stem)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError:
        out = output_dir / sanitize_filename(csv_path.stem, max_bytes=80)
        out.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]Downloading playlist from {csv_path.name} into {out}[/bold]")

    rows: list[dict[str, str]] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if not rows:
        console.print("[yellow]CSV file is empty.[/yellow]")
        return 0

    downloaded = skipped = failed = missing = 0
    total = len(rows)
    notes: list[str] = []
    overall_progress = Progress(
        TextColumn("[bold cyan]Overall [/bold cyan]"),
        BarColumn(bar_width=24),
        MofNCompleteColumn(),
        TextColumn("{task.description}"),
        console=console,
    )
    track_progress = Progress(
        TextColumn("[cyan]Track   [/cyan]"),
        TextColumn("{task.description}"),
        BarColumn(bar_width=24),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )

    interrupted = False
    try:
        with Live(
            Group(overall_progress, track_progress),
            console=console,
            refresh_per_second=10,
            transient=False,
        ):
            overall_task = overall_progress.add_task(
                _format_csv_counters(0, 0, 0, 0), total=total
            )
            track_task = track_progress.add_task("", total=None)
            for idx, row in enumerate(rows, 1):
                track_name = row.get("Track Name", "").strip()
                artists_raw = row.get("Artist Name(s)", "").strip()
                if not track_name or not artists_raw:
                    missing += 1
                    notes.append("[yellow][skip] Missing track/artist in CSV row[/yellow]")
                    overall_progress.update(
                        overall_task,
                        advance=1,
                        description=_format_csv_counters(downloaded, skipped, failed, missing),
                    )
                    continue

                query = f"{artists_raw} - {track_name}"
                try:
                    tracks, _ = client.search(query, limit=8)
                except Exception as exc:
                    failed += 1
                    notes.append(f"[yellow][skip] Search failed for '{query}': {exc}[/yellow]")
                    overall_progress.update(
                        overall_task,
                        advance=1,
                        description=_format_csv_counters(downloaded, skipped, failed, missing),
                    )
                    continue

                if not tracks:
                    failed += 1
                    notes.append(f"[red][fail] No results for '{query}'[/red]")
                    overall_progress.update(
                        overall_task,
                        advance=1,
                        description=_format_csv_counters(downloaded, skipped, failed, missing),
                    )
                    continue

                # Pick first result
                track = tracks[0]
                track_progress.update(
                    track_task,
                    description=f"[{idx}/{total}] {track.artists[0] if track.artists else '?'} - {track.title}",
                    total=None,
                )
                status = download_single(
                    client, track, out, track_progress, track_task, status_log=notes
                )
                if status == "downloaded":
                    downloaded += 1
                    track_progress.update(
                        track_task,
                        description=f"[green]✓ [{idx}/{total}] {track.title}[/green]",
                    )
                elif status == "skipped":
                    skipped += 1
                    track_progress.update(
                        track_task,
                        description=f"[yellow]⊘ [{idx}/{total}] {track.title}[/yellow]",
                    )
                else:
                    failed += 1
                    track_progress.update(
                        track_task,
                        description=f"[red]✗ [{idx}/{total}] {track.title}[/red]",
                    )

                overall_progress.update(
                    overall_task,
                    advance=1,
                    description=_format_csv_counters(downloaded, skipped, failed, missing),
                )
    except KeyboardInterrupt:
        interrupted = True
        notes.append("[yellow]Interrupted by user.[/yellow]")

    summary = Table(title=f"Playlist Summary: {csv_path.name}")
    summary.add_column("Status", style="bold")
    summary.add_column("Count", justify="right")
    summary.add_row("[green]Downloaded[/green]", str(downloaded))
    if skipped:
        summary.add_row("[yellow]Skipped (already exists)[/yellow]", str(skipped))
    if failed:
        summary.add_row("[red]Failed[/red]", str(failed))
    if missing:
        summary.add_row("[dim]Missing data (CSV)[/dim]", str(missing))
    summary.add_row("[bold]Total processed[/bold]", str(total))
    console.print(summary)
    _print_notes_table(notes, title="Per-row notes")
    console.print()
    return downloaded if not interrupted else downloaded


def _check_mirror(session: requests.Session, url: str, path: str, timeout: int = 8) -> dict[str, Any]:
    full = f"{url.rstrip('/')}{path}"
    start = time.time()
    try:
        resp = session.get(full, timeout=timeout)
        latency = (time.time() - start) * 1000
        if resp.ok:
            try:
                resp.json()
                return {"url": url, "status": "ok", "latency_ms": latency, "http_status": resp.status_code}
            except Exception:
                return {"url": url, "status": "unexpected content", "latency_ms": latency, "http_status": resp.status_code}
        return {"url": url, "status": f"HTTP {resp.status_code}", "latency_ms": latency, "http_status": resp.status_code}
    except requests.exceptions.Timeout:
        return {"url": url, "status": "timeout", "latency_ms": None, "http_status": None}
    except Exception as exc:
        cat, detail = classify_error(exc)
        return {"url": url, "status": detail, "latency_ms": None, "http_status": None}


def main() -> int:
    try:
        return _main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        return 130


def _main() -> int:
    parser = argparse.ArgumentParser(description="Search Monochrome and download tracks, albums, or discographies.")
    parser.add_argument("query", nargs="?", help="Search query")
    parser.add_argument("-a", "--album", action="store_true", help="Search for albums and bulk download")
    parser.add_argument("-d", "--discography", action="store_true", help="Search for artist discography and bulk download all albums")
    parser.add_argument("--no-strict", action="store_true", help="In discography mode, include tracks from other artists that match the search query")
    parser.add_argument("-n", "--limit", type=int, default=50, help="Tracks per search page (default 50)")
    parser.add_argument("--pages", type=int, default=5, help="Max search pages for discography (default 5)")
    parser.add_argument(
        "-q", "--quality",
        default=DEFAULT_CFG_QUALITY,
        choices=VALID_QUALITIES,
        help=(
            f"Stream quality (one of {', '.join(VALID_QUALITIES)}). "
            f"Default: {DEFAULT_CFG_QUALITY} "
            f"(overridable via MONOCHROME_DL_QUALITY env or config.json `quality` key)."
        ),
    )
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="Output directory (default downloads/)")
    parser.add_argument("--mirrors", nargs="+", default=DEFAULT_CFG_MONOCHROME_MIRRORS, help="Monochrome mirror URLs (override config)")
    parser.add_argument("--qobuz-mirrors", nargs="+", default=DEFAULT_CFG_QOBUZ_MIRRORS, help="Qobuz mirror URLs (override config)")
    parser.add_argument("--status", action="store_true", help="Check mirror availability and exit")
    parser.add_argument("--csv", type=Path, default=None, help="Path to a CSV playlist file for bulk download")
    parser.add_argument("--fix-extensions", type=Path, default=None, metavar="DIR",
                        help="Rename files under DIR whose extension does not match their actual audio container (FLAC/M4A/MP3) and exit")
    parser.add_argument(
        "--tui",
        dest="tui",
        action="store_true",
        default=None,
        help="Force the interactive TUI picker (arrow keys, space, a, backspace, q)",
    )
    parser.add_argument(
        "--no-tui",
        dest="tui",
        action="store_false",
        default=None,
        help="Disable the TUI picker; use the legacy text prompts (handy for piping / CI)",
    )
    args = parser.parse_args()
    args.output = Path(os.path.expanduser(str(args.output)))
    tui_mode = args.tui if args.tui is not None else _is_tty()

    if args.fix_extensions is not None:
        root = Path(os.path.expanduser(str(args.fix_extensions)))
        if not root.is_dir():
            console.print(f"[red]Not a directory: {root}[/red]")
            return 1
        renamed, scanned = fix_extensions(root)
        console.print(f"[bold green]Scanned {scanned} file(s); renamed {renamed}.[/bold green]")
        return 0

    client = MonochromeClient(
        base_urls=args.mirrors,
        quality=args.quality,
        qobuz_urls=args.qobuz_mirrors,
    )

    if args.status:
        table = Table(title="Mirror Status")
        table.add_column("Type", style="cyan")
        table.add_column("Mirror", style="blue")
        table.add_column("Status", style="green")
        table.add_column("Latency", style="yellow", justify="right")

        for url in client.base_urls:
            r = _check_mirror(client.session, url, "/search/?s=test&limit=1")
            status_style = "green" if r["status"] == "ok" else "red"
            latency_str = f"{r['latency_ms']:.0f} ms" if r["latency_ms"] is not None else "—"
            table.add_row("Monochrome", r["url"], f"[{status_style}]{r['status']}[/{status_style}]", latency_str)

        for url in client.qobuz_urls:
            r = _check_mirror(client.session, url, "/api/get-music?q=test&offset=0")
            status_style = "green" if r["status"] == "ok" else "red"
            latency_str = f"{r['latency_ms']:.0f} ms" if r["latency_ms"] is not None else "—"
            table.add_row("Qobuz", r["url"], f"[{status_style}]{r['status']}[/{status_style}]", latency_str)

        console.print(table)
        return 0

    if args.csv:
        download_from_csv(client, args.csv, args.output)
        return 0

    query = args.query
    if not query:
        query = console.input("[bold cyan]Search query: [/bold cyan]").strip()
    if not query:
        console.print("[red]Empty query.[/red]")
        return 1

    if args.discography:
        console.print(f"[bold]Searching discography for: {query}[/bold]")
        try:
            albums = client.search_discography(query, limit=args.limit, max_pages=args.pages, strict=not args.no_strict)
        except Exception as exc:
            console.print(f"[red]Search failed: {exc}[/red]")
            return 1

        if not albums:
            console.print("[yellow]No albums found for that artist.[/yellow]")
            return 0

        download_discography(client, albums, args.output, query, force_tui=tui_mode)
    elif args.album:
        console.print(f"[bold]Searching albums for: {query}[/bold]")
        try:
            albums = client.search_albums(query, limit=args.limit)
        except Exception as exc:
            console.print(f"[red]Search failed: {exc}[/red]")
            return 1

        selected = select_albums(albums, force_tui=tui_mode)
        if not selected:
            return 0
        download_albums(client, selected, args.output)
    else:
        console.print(f"[bold]Searching tracks for: {query}[/bold]")
        try:
            tracks, _ = client.search(query, limit=args.limit)
        except Exception as exc:
            console.print(f"[red]Search failed: {exc}[/red]")
            return 1

        track_list = select_tracks(tracks, force_tui=tui_mode)
        track = track_list[0] if track_list else None
        if not track:
            return 0

        artist_dir = sanitize_filename(", ".join(track.artists) or "Unknown")
        out = args.output / artist_dir
        out.mkdir(parents=True, exist_ok=True)
        try:
            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task_id = progress.add_task(
                    f"{', '.join(track.artists) or 'Unknown'} - {track.title}",
                    total=None,
                )
                status = download_single(client, track, out, progress, task_id)
                if status == "skipped":
                    console.print("[yellow]Track already exists — skipped.[/yellow]")
                elif status == "failed":
                    console.print("[red]Track download failed.[/red]")
                else:
                    console.print("[green]Track downloaded successfully.[/green]")
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted by user.[/yellow]")

    return 0


# ──────────────────────────────────────────────────────────────────────────────
# TUI picker
#
# Interactive selection for search results: arrow keys move a cursor, Space
# toggles a per-row "marked for download" state, Backspace toggles a per-row
# "excluded" state, `a` toggles mark-all, Enter confirms, `q` cancels.
#
# Renders inside a `rich.live.Live` so the table redraws in place. Keyboard
# input is read directly from the terminal in cbreak mode; terminal state is
# always restored on exit (including on KeyboardInterrupt / EOF).
# ──────────────────────────────────────────────────────────────────────────────


_T = TypeVar("_T")


# Canonical key names produced by _read_one_key / _parse_key_bytes.
KEY_UP = "UP"
KEY_DOWN = "DOWN"
KEY_LEFT = "LEFT"
KEY_RIGHT = "RIGHT"
KEY_ENTER = "ENTER"
KEY_SPACE = "SPACE"
KEY_BACKSPACE = "BACKSPACE"
KEY_QUIT = "q"
KEY_ALL = "a"
KEY_ESC = "ESC"
KEY_OTHER = "OTHER"
KEY_CTRL_C = "CTRL_C"
KEY_CTRL_D = "CTRL_D"
KEY_HOME = "HOME"
KEY_END = "END"

# 1-byte keystrokes that map to a canonical key name.
_SINGLE_KEY_MAP: dict[bytes, str] = {
    b"\r": KEY_ENTER,
    b"\n": KEY_ENTER,
    b" ": KEY_SPACE,
    b"\x7f": KEY_BACKSPACE,
    b"\x08": KEY_BACKSPACE,
    b"\x03": KEY_CTRL_C,
    b"\x04": KEY_CTRL_D,
    b"q": KEY_QUIT,
    b"Q": KEY_QUIT,
    b"a": KEY_ALL,
    b"A": KEY_ALL,
}

# Trailing byte of an ESC [ X (or ESC O X) arrow / navigation sequence.
_TRAILING_KEY_MAP: dict[bytes, str] = {
    b"A": KEY_UP,
    b"B": KEY_DOWN,
    b"C": KEY_RIGHT,
    b"D": KEY_LEFT,
    b"H": KEY_HOME,
    b"F": KEY_END,
}

# Windows arrow keys: msvcrt.getch returns b"\x00" or b"\xe0" followed by a
# second byte that identifies the actual key.
_WIN_SCAN_MAP: dict[bytes, str] = {
    b"H": KEY_UP,
    b"P": KEY_DOWN,
    b"K": KEY_LEFT,
    b"M": KEY_RIGHT,
    b"G": KEY_HOME,
    b"O": KEY_END,
}


def _parse_key_bytes(buf: bytes) -> str:
    """Map a 1-byte keystroke (or 3-byte CSI sequence) to a canonical name.

    Pure function — testable without a real terminal.
    """
    if not buf:
        return KEY_OTHER
    if len(buf) == 1:
        return _SINGLE_KEY_MAP.get(buf, KEY_OTHER)
    if len(buf) == 3 and buf[0:2] == b"\x1b[":
        return _TRAILING_KEY_MAP.get(buf[2:3], KEY_OTHER)
    return KEY_OTHER


class PickerState:
    """Pure state model for the picker. No I/O.

    Each row is in one of three states: unmarked, marked (download), or
    excluded (skip-on-confirm). The cursor highlights exactly one row.
    """

    UNMARKED = "unmarked"
    MARKED = "marked"
    EXCLUDED = "excluded"

    def __init__(self, n: int) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self.n = n
        self.cursor = 0
        self._states: list[str] = [self.UNMARKED] * n

    # ── queries ──────────────────────────────────────────────────────────
    def state_at(self, i: int) -> str:
        return self._states[i]

    def is_marked(self, i: int) -> bool:
        return self._states[i] == self.MARKED

    def is_excluded(self, i: int) -> bool:
        return self._states[i] == self.EXCLUDED

    def counts(self) -> tuple[int, int, int]:
        m = sum(1 for s in self._states if s == self.MARKED)
        e = sum(1 for s in self._states if s == self.EXCLUDED)
        return m, e, self.n

    # ── transitions ──────────────────────────────────────────────────────
    def move(self, delta: int) -> None:
        if self.n == 0:
            return
        self.cursor = max(0, min(self.n - 1, self.cursor + delta))

    def toggle_mark(self) -> None:
        """Toggle the mark state on the cursor row. Excluded → unmarked."""
        if self.n == 0:
            return
        i = self.cursor
        cur = self._states[i]
        if cur == self.MARKED:
            self._states[i] = self.UNMARKED
        elif cur == self.EXCLUDED:
            self._states[i] = self.UNMARKED
        else:
            self._states[i] = self.MARKED

    def toggle_exclude(self) -> None:
        """Toggle the exclude state on the cursor row. Excluded ⊥ marked."""
        if self.n == 0:
            return
        i = self.cursor
        if self._states[i] == self.EXCLUDED:
            self._states[i] = self.UNMARKED
        else:
            self._states[i] = self.EXCLUDED

    def select_all(self) -> None:
        """Toggle mark-all. If every non-excluded row is currently marked,
        clear them; otherwise mark them all. Excluded rows are never marked.
        """
        if self.n == 0:
            return
        non_excluded = [i for i in range(self.n) if self._states[i] != self.EXCLUDED]
        all_marked = bool(non_excluded) and all(
            self._states[i] == self.MARKED for i in non_excluded
        )
        for i in non_excluded:
            self._states[i] = self.UNMARKED if all_marked else self.MARKED

    # ── output ───────────────────────────────────────────────────────────
    def confirmed(self) -> list[int]:
        """Indices of marked rows, in display order."""
        return [i for i in range(self.n) if self._states[i] == self.MARKED]


class _TerminalRaw:
    """Context manager that puts stdin into cbreak (Unix) or no-op (Windows,
    msvcrt handles its own state). Always restores the prior termios state
    on exit, even on exceptions.
    """

    def __init__(self) -> None:
        self._is_win = sys.platform == "win32"
        self._fd: int | None = None
        self._old: list[Any] | None = None

    def __enter__(self) -> "_TerminalRaw":
        # Windows: msvcrt manages its own terminal state — nothing to set up
        # here. We still attempt to verify the stdio handles are usable in
        # ``is_real`` below.
        if self._is_win:
            try:
                self._fd = sys.stdin.fileno()
            except (AttributeError, ValueError, OSError):
                return self
            return self
        # Non-Windows: enter cbreak mode. Bail out (no-op) only if the
        # termios/tty module is genuinely missing — which on a stock Linux
        # or macOS interpreter is impossible.
        if termios is None or tty is None:
            return self
        try:
            self._fd = sys.stdin.fileno()
        except (AttributeError, ValueError, OSError):
            # Not a real terminal (e.g. tests, CI, piped input).
            return self
        try:
            self._old = termios.tcgetattr(self._fd)
        except termios.error:
            self._old = None
            return self
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._is_win or self._fd is None or self._old is None:
            return
        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
        except termios.error:
            pass

    @property
    def is_real(self) -> bool:
        """True if reading from a real terminal (and a keypress will be
        consumed from stdin). False when running under tests or pipes.
        """
        if self._is_win:
            return self._fd is not None
        return self._fd is not None and self._old is not None

    def read(self) -> bytes:
        """Read one byte from the terminal. Caller is responsible for
        buffering ESC sequences (this returns a single byte only).
        """
        if self._is_win:
            assert msvcrt is not None
            return msvcrt.getch()
        assert self._fd is not None
        return os.read(self._fd, 1)


def _read_one_key(raw: _TerminalRaw) -> str | None:
    """Read a single key from ``raw`` and return its canonical name, or
    ``None`` on EOF. Cross-platform; handles ESC sequences and Windows scan
    codes.
    """
    try:
        ch1 = raw.read()
    except OSError:
        return None
    if not ch1:
        return None

    # Windows function / arrow keys: b"\\x00" or b"\\xe0" + scan byte.
    if sys.platform == "win32" and ch1 in (b"\x00", b"\xe0"):
        try:
            ch2 = raw.read()
        except OSError:
            return None
        return _WIN_SCAN_MAP.get(ch2, KEY_OTHER)

    # ESC: possibly the start of a CSI sequence, possibly a bare Escape.
    if ch1 == b"\x1b" and not sys.platform == "win32":
        # Peek with a short timeout to disambiguate bare Escape vs sequence.
        try:
            rlist, _, _ = select.select([raw._fd], [], [], 0.05)  # type: ignore[arg-type]
        except (ValueError, OSError):
            return KEY_ESC
        if not rlist:
            return KEY_ESC
        try:
            ch2 = raw.read()
        except OSError:
            return KEY_ESC
        if ch2 not in (b"[", b"O"):
            return KEY_ESC
        try:
            rlist, _, _ = select.select([raw._fd], [], [], 0.05)  # type: ignore[arg-type]
        except (ValueError, OSError):
            return KEY_ESC
        if not rlist:
            return KEY_ESC
        try:
            ch3 = raw.read()
        except OSError:
            return KEY_ESC
        return _parse_key_bytes(b"\x1b" + ch2 + ch3)

    return _parse_key_bytes(ch1)


def _is_tty() -> bool:
    """True if both stdin and stdout are attached to a terminal."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


# Type aliases for the picker loop.
_RowFn = Callable[[_T], list[str]]
_LabelFn = Callable[[int, PickerState], str]
_KeySource = Callable[[], str | None]


def _render_picker(
    title: str,
    headers: list[tuple[str, str]],
    items: list[_T],
    state: PickerState,
    row_fn: _RowFn,
    label_fn: _LabelFn,
) -> Group:
    """Build the rich renderable for the current picker state."""
    table = Table(
        title=title,
        show_header=True,
        header_style="bold",
        expand=True,
        show_lines=False,
    )
    table.add_column("", style="cyan", no_wrap=True, width=3)
    for name, style in headers:
        table.add_column(name, style=style, overflow="ellipsis")
    if not items:
        group = Group(
            table,
            Text("No results to display.", style="yellow"),
        )
        return group
    for i, item in enumerate(items):
        cells: list[Text] = []
        if i == state.cursor:
            row_style = "reverse"
        elif state.is_excluded(i):
            row_style = "dim"
        else:
            row_style = ""
        cells.append(Text(label_fn(i, state), style=row_style))
        for raw in row_fn(item):
            cells.append(Text(str(raw), style=row_style))
        table.add_row(*cells)

    m, e, n = state.counts()
    footer = Text.assemble(
        (
            "↑/↓ move · space mark · ⌫ exclude · a all · enter confirm · q quit\n",
            "dim",
        ),
        (f"{m} marked · {e} excluded · {n} total", "bold"),
    )
    return Group(table, footer)


def _run_picker(
    title: str,
    headers: list[tuple[str, str]],
    items: list[_T],
    row_fn: _RowFn,
    label_fn: _LabelFn,
    *,
    key_source: _KeySource | None = None,
) -> tuple[PickerState, list[int]] | None:
    """Run the interactive picker. Returns ``(state, marked_indices)`` on
    confirm, or ``None`` on cancel / Ctrl+C / EOF.

    When ``key_source`` is provided, the real terminal is not touched and the
    picker is driven by the supplied callable (used by tests). When it is
    ``None``, stdin is put into cbreak mode for the duration of the loop and
    restored before returning.
    """
    if not items:
        return PickerState(0), []
    state = PickerState(len(items))

    def _render() -> Group:
        return _render_picker(title, headers, items, state, row_fn, label_fn)

    def _dispatch(key: str) -> bool:
        """Apply ``key`` to ``state``. Returns True if the loop should exit."""
        if key == KEY_UP:
            state.move(-1)
        elif key == KEY_DOWN:
            state.move(1)
        elif key == KEY_SPACE:
            state.toggle_mark()
        elif key == KEY_BACKSPACE:
            state.toggle_exclude()
        elif key == KEY_ALL:
            state.select_all()
        elif key == KEY_ENTER:
            return True
        elif key in (KEY_QUIT, KEY_ESC, KEY_CTRL_C, KEY_CTRL_D):
            state._cancelled = True  # type: ignore[attr-defined]
            return True
        return False

    if key_source is not None:
        # ``screen=True`` puts the picker in the terminal's alternate screen
        # buffer so the table is rendered into a private area. On close the
        # alt-screen is torn down and the main screen — with any preceding
        # output like the "Searching …" line — reappears underneath the
        # download progress, with no visible erase/flicker.
        with Live(
            _render(),
            console=console,
            refresh_per_second=12,
            transient=False,
            screen=True,
        ):
            while True:
                key = key_source()
                if key is None:
                    state._cancelled = True  # type: ignore[attr-defined]
                    return None
                if _dispatch(key):
                    break
        return state, _resolve_confirm(state)

    with _TerminalRaw() as raw:
        if not raw.is_real:
            # No real terminal available; fall back to a no-op key source so
            # the caller can decide what to do (typically: use the prompt
            # path). Returning a "cancelled" state is the safest signal.
            return None
        try:
            with Live(
                _render(),
                console=console,
                refresh_per_second=12,
                transient=False,
                screen=True,
            ) as live:
                while True:
                    key = _read_one_key(raw)
                    if key is None:
                        return None
                    if _dispatch(key):
                        break
                    live.update(_render())
        except KeyboardInterrupt:
            state._cancelled = True  # type: ignore[attr-defined]
            return None
    return state, _resolve_confirm(state)


def _resolve_confirm(state: PickerState) -> list[int]:
    """Pick the indices to return on confirm. If nothing is explicitly
    marked, fall back to the cursor row — the natural "download the one I'm
    looking at" UX. Cancellation always wins.
    """
    if getattr(state, "_cancelled", False):
        return []
    marked = state.confirmed()
    if marked:
        return marked
    if state.n == 0:
        return []
    return [state.cursor]


# ── Public dispatchers ─────────────────────────────────────────────────────


def _label_track(i: int, state: PickerState) -> str:
    if state.is_marked(i):
        return "[x]"
    if state.is_excluded(i):
        return "[-]"
    return "[ ]"


def _row_track(t: TrackMatch) -> list[str]:
    return [
        ", ".join(t.artists) or "Unknown",
        t.title,
        t.album,
        t.quality,
    ]


def _label_album(i: int, state: PickerState) -> str:
    if state.is_marked(i):
        return "[x]"
    if state.is_excluded(i):
        return "[-]"
    return "[ ]"


def _row_album(a: AlbumMatch) -> list[str]:
    return [
        a.inferred_type,
        a.title,
        str(len(a.tracks)),
        a.display_artist,
        a.quality,
    ]


def select_tracks(
    tracks: list[TrackMatch],
    *,
    force_tui: bool | None = None,
) -> list[TrackMatch]:
    """Pick tracks from the search result.

    In TUI mode (default on a TTY) returns the marked tracks, or
    ``[tracks[cursor]]`` if nothing was marked. In fallback mode uses
    ``pick_track`` and returns a single-element list (or empty on cancel).

    When ``force_tui`` is ``None`` (auto-detect) and the TUI path is
    unavailable — e.g. the terminal is not actually a TTY at the raw-mode
    layer even though ``isatty()`` reported one — the dispatcher transparently
    falls back to the legacy prompt so a usable selection is always
    presented.
    """
    if not tracks:
        console.print("[yellow]No tracks found.[/yellow]")
        return []
    if force_tui is not False and _tui_available():
        result = _run_picker(
            title=f"Select tracks ({len(tracks)})",
            headers=[
                ("Artist", "green"),
                ("Title", "blue"),
                ("Album", "magenta"),
                ("Quality", "yellow"),
            ],
            items=tracks,
            row_fn=_row_track,
            label_fn=_label_track,
        )
        if result is not None:
            _state, indices = result
            return [tracks[i] for i in indices]
        if force_tui is True:
            # User explicitly asked for the TUI; don't fall back.
            return []
        # Auto-detect: TUI was not actually usable. Fall through to the
        # prompt path so the user still gets to pick something.
        console.print("[dim]TUI unavailable; using text prompt.[/dim]")
    chosen = pick_track(tracks)
    return [chosen] if chosen is not None else []


def select_albums(
    albums: list[AlbumMatch],
    *,
    force_tui: bool | None = None,
) -> list[AlbumMatch]:
    """Pick albums from the search result.

    In TUI mode returns all marked albums in display order (or
    ``[albums[cursor]]`` if nothing was marked). In fallback mode uses
    ``pick_albums`` (string parser).

    Falls back to the legacy prompt if TUI was auto-detected but turned out
    to be unavailable at the raw-mode layer (see ``select_tracks``).
    """
    if not albums:
        console.print("[yellow]No albums found.[/yellow]")
        return []
    albums = sorted(albums, key=lambda a: len(a.tracks), reverse=True)
    if force_tui is not False and _tui_available():
        result = _run_picker(
            title=f"Select albums ({len(albums)})",
            headers=[
                ("Type", "magenta"),
                ("Album", "blue"),
                ("Tracks", "yellow"),
                ("Artist", "green"),
                ("Quality", "cyan"),
            ],
            items=albums,
            row_fn=_row_album,
            label_fn=_label_album,
        )
        if result is not None:
            _state, indices = result
            return [albums[i] for i in indices]
        if force_tui is True:
            return []
        console.print("[dim]TUI unavailable; using text prompt.[/dim]")
    return pick_albums(albums)


def _tui_available() -> bool:
    """True if a real TTY is attached AND raw-mode can be entered.

    Uses the same checks as :func:`_is_tty` plus a probe of the cbreak
    context manager so the dispatcher never silently fails in environments
    where ``isatty()`` lies (wrapper scripts, detached tmux panes,
    CI sandboxes, …).
    """
    if not _is_tty():
        return False
    with _TerminalRaw() as raw:
        return raw.is_real


# ── Test helper ────────────────────────────────────────────────────────────


def _select_albums_with_keys(
    albums: list[AlbumMatch],
    keys: list[str],
) -> list[AlbumMatch]:
    """Drive :func:`select_albums` with a fixed key sequence. No real
    terminal is touched. Used by the test suite.
    """
    if not albums:
        return []
    albums = sorted(albums, key=lambda a: len(a.tracks), reverse=True)
    it = iter(keys)

    def src() -> str | None:
        try:
            return next(it)
        except StopIteration:
            return None

    result = _run_picker(
        title=f"Select albums ({len(albums)})",
        headers=[
            ("Type", "magenta"),
            ("Album", "blue"),
            ("Tracks", "yellow"),
            ("Artist", "green"),
            ("Quality", "cyan"),
        ],
        items=albums,
        row_fn=_row_album,
        label_fn=_label_album,
        key_source=src,
    )
    if result is None:
        return []
    _state, indices = result
    return [albums[i] for i in indices]


# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    sys.exit(main())
