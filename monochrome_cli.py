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
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Confirm, IntPrompt
from rich.table import Table

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

        console.print(f"[red][monochrome] All {len(errors)} mirrors failed for {path}[/red]")
        for e in errors:
            status = f" ({e.get('status')})" if e.get("status") else ""
            console.print(f"  [red]{e['mirror']}: [{e['category']}]{status} {e['detail']}[/red]")
        raise RuntimeError(f"All Monochrome mirrors failed for {path}")

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

        if isrc and self.qobuz_urls:
            qobuz_url = self._try_qobuz(isrc, q)
            if qobuz_url:
                return qobuz_url

        path = f"/track/?id={tidal_id}&quality={q}"
        data, _ = self._request_any(path)
        url = self._extract_stream_url(data)
        if url:
            return url

        reason = self._classify_missing(data)
        self._log_diag(tidal_id, data, path, reason)
        exc = RuntimeError(f"No playable URL for track {tidal_id}")
        exc.reason = reason
        raise exc

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


def download_file(url: str, dest: Path, progress: Progress, task_id: int, chunk_size: int = 8192) -> None:
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    if total:
        progress.update(task_id, total=total)
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if chunk:
                f.write(chunk)
                progress.update(task_id, advance=len(chunk))


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


def pick_album(albums: list[AlbumMatch]) -> AlbumMatch | None:
    if not albums:
        console.print("[yellow]No albums found.[/yellow]")
        return None
    albums = sorted(albums, key=lambda a: len(a.tracks), reverse=True)
    table = Table(title=f"Found {len(albums)} album(s)")
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("Type", style="magenta")
    table.add_column("Album", style="blue")
    table.add_column("Tracks", style="yellow")
    table.add_column("Artist", style="green")
    for i, a in enumerate(albums, 1):
        table.add_row(str(i), a.inferred_type, a.title, str(len(a.tracks)), a.display_artist)
    console.print(table)
    while True:
        choice = IntPrompt.ask("Pick a number (0 to quit)", default=0)
        if choice == 0:
            return None
        if 1 <= choice <= len(albums):
            return albums[choice - 1]
        console.print("[red]Invalid choice.[/red]")


def download_single(client: MonochromeClient, track: TrackMatch, output_dir: Path, progress: Progress) -> str:
    try:
        stream_url = client.get_stream_url(track.tidal_id, quality=client.quality, isrc=track.isrc)
    except Exception as exc:
        console.print(f"[red][skip] {track.title}: failed to get stream URL: {exc}[/red]")
        return "failed"

    safe_name = sanitize_filename(f"{', '.join(track.artists)} - {track.title}")
    dest = output_dir / f"{safe_name}.flac"
    try:
        if dest.exists():
            console.print(f"[yellow][skip] {track.title}: already exists[/yellow]")
            return "skipped"
    except OSError:
        # Path too long — try progressively shorter names
        for short_len in (120, 80, 50, 30):
            safe_name = sanitize_filename(f"{', '.join(track.artists)} - {track.title}", max_bytes=short_len)
            dest = output_dir / f"{safe_name}.flac"
            try:
                if dest.exists():
                    console.print(f"[yellow][skip] {track.title}: already exists[/yellow]")
                    return "skipped"
                break
            except OSError:
                continue
        else:
            console.print(f"[red][skip] {track.title}: path too long[/red]")
            return "failed"

    task_id = progress.add_task(f"[cyan]{track.title}", start=True)
    try:
        download_file(stream_url, dest, progress, task_id)
        progress.update(task_id, description=f"[green]✓ {track.title}")
        return "downloaded"
    except KeyboardInterrupt:
        progress.update(task_id, description=f"[red]✗ {track.title}")
        console.print(f"[yellow]Interrupted: {track.title}[/yellow]")
        raise
    except Exception as exc:
        progress.update(task_id, description=f"[red]✗ {track.title}")
        console.print(f"[red][skip] {track.title}: download failed: {exc}[/red]")
        return "failed"


def download_album(client: MonochromeClient, album: AlbumMatch, base_dir: Path, artist_folder: str | None = None) -> int:
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

    console.print(Panel(
        f"[bold]{album.title}[/bold] by {album.display_artist}\n"
        f"[dim]{len(album.tracks)} track(s) → {out}[/dim]",
        title=f"[bold cyan]Downloading {album.inferred_type}[/bold cyan]",
        border_style="cyan",
    ))

    downloaded = skipped = failed = 0
    try:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            for track in album.tracks:
                status = download_single(client, track, out, progress)
                if status == "downloaded":
                    downloaded += 1
                elif status == "skipped":
                    skipped += 1
                else:
                    failed += 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")

    summary = Table(title=f"Album Summary: {album.title} ({len(album.tracks)} tracks)", show_header=True)
    summary.add_column("Status", style="bold")
    summary.add_column("Count", justify="right")
    summary.add_row("[green]Downloaded[/green]", str(downloaded))
    if skipped:
        summary.add_row("[yellow]Skipped[/yellow]", str(skipped))
    if failed:
        summary.add_row("[red]Failed[/red]", str(failed))
    console.print(summary)
    console.print()
    return downloaded


def download_discography(client: MonochromeClient, albums: list[AlbumMatch], base_dir: Path, artist_query: str) -> int:
    albums = sorted(albums, key=lambda a: len(a.tracks), reverse=True)
    total_tracks = sum(len(a.tracks) for a in albums)

    table = Table(title=f"Discography for {artist_query}")
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("Type", style="magenta")
    table.add_column("Album", style="blue")
    table.add_column("Tracks", style="yellow")
    table.add_column("Artist", style="green")
    for i, a in enumerate(albums, 1):
        table.add_row(str(i), a.inferred_type, a.title, str(len(a.tracks)), a.display_artist)
    console.print(table)

    if not Confirm.ask(f"Download all {len(albums)} album(s)?"):
        return 0

    total_downloaded = 0
    try:
        for album in albums:
            total_downloaded += download_album(client, album, base_dir, artist_folder=artist_query)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")

    console.print(f"[bold green]Discography complete: {total_downloaded}/{total_tracks} tracks saved across {len(albums)} album(s)[/bold green]")
    return total_downloaded


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

    def _overall_desc() -> str:
        return f"[bold cyan]Overall: {downloaded + skipped + failed + missing}/{total} | Down: {downloaded} | Skip: {skipped} | Fail: {failed} | Miss: {missing}[/bold cyan]"

    try:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            overall_task = progress.add_task(_overall_desc(), total=total)
            for row in rows:
                track_name = row.get("Track Name", "").strip()
                artists_raw = row.get("Artist Name(s)", "").strip()
                if not track_name or not artists_raw:
                    missing += 1
                    console.print("[yellow][skip] Missing track/artist in CSV row[/yellow]")
                    progress.update(overall_task, advance=1, description=_overall_desc())
                    continue

                query = f"{artists_raw} - {track_name}"
                try:
                    tracks, _ = client.search(query, limit=8)
                except Exception as exc:
                    failed += 1
                    console.print(f"[yellow][skip] Search failed for '{query}': {exc}[/yellow]")
                    progress.update(overall_task, advance=1, description=_overall_desc())
                    continue

                if not tracks:
                    failed += 1
                    console.print(f"[red][fail] No results for '{query}'[/red]")
                    progress.update(overall_task, advance=1, description=_overall_desc())
                    continue

                # Pick first result
                track = tracks[0]
                status = download_single(client, track, out, progress)
                if status == "downloaded":
                    downloaded += 1
                elif status == "skipped":
                    skipped += 1
                else:
                    failed += 1

                progress.update(overall_task, advance=1, description=_overall_desc())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")

    # Final summary
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
    return downloaded


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
    parser.add_argument("-q", "--quality", default=DEFAULT_QUALITY, help="Stream quality (default HIGH)")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="Output directory (default downloads/)")
    parser.add_argument("--mirrors", nargs="+", default=DEFAULT_CFG_MONOCHROME_MIRRORS, help="Monochrome mirror URLs (override config)")
    parser.add_argument("--qobuz-mirrors", nargs="+", default=DEFAULT_CFG_QOBUZ_MIRRORS, help="Qobuz mirror URLs (override config)")
    parser.add_argument("--status", action="store_true", help="Check mirror availability and exit")
    parser.add_argument("--csv", type=Path, default=None, help="Path to a CSV playlist file for bulk download")
    args = parser.parse_args()
    args.output = Path(os.path.expanduser(str(args.output)))

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

        download_discography(client, albums, args.output, query)
    elif args.album:
        console.print(f"[bold]Searching albums for: {query}[/bold]")
        try:
            albums = client.search_albums(query, limit=args.limit)
        except Exception as exc:
            console.print(f"[red]Search failed: {exc}[/red]")
            return 1

        album = pick_album(albums)
        if not album:
            return 0

        download_album(client, album, args.output)
    else:
        console.print(f"[bold]Searching tracks for: {query}[/bold]")
        try:
            tracks, _ = client.search(query, limit=args.limit)
        except Exception as exc:
            console.print(f"[red]Search failed: {exc}[/red]")
            return 1

        track = pick_track(tracks)
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
                status = download_single(client, track, out, progress)
                if status == "skipped":
                    console.print("[yellow]Track already exists — skipped.[/yellow]")
                elif status == "failed":
                    console.print("[red]Track download failed.[/red]")
                else:
                    console.print("[green]Track downloaded successfully.[/green]")
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted by user.[/yellow]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
