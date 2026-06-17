# monochrome-cli

> [!IMPORTANT]
> This repository does not contain any copyrighted material, or code to illegaly download music. Downloads are provided by the Qobuz API and should only be initiated by the API token owner. The author is **not responsible for the usage of this repository nor endorses it**, nor is the author responsible for any copies, forks, re-uploads made by other users, or anything else related to [monochrome-cli](https://github.com/kraibse/monochrome-cli) or [Qobuz-DL](https://github.com/QobuzDL/Qobuz-DL). Any live demo found online of this project is not associated with the authors of this repo. This is the author's only account and repository.

A standalone Python CLI for searching and downloading music from Monochrome API mirrors.

## Installation

Requires Python 3.9+.

```bash
pip install -r requirements.txt
```

## Usage

### General search

Download a single track matching a free-form query. Multiple results are presented for selection.

```bash
# Basic search
python monochrome_cli.py "artist - song title"

# Specific quality
python monochrome_cli.py -q LOSSLESS "Pink Floyd - Time"

# Save to a custom folder
python monochrome_cli.py -o ~/Music "Daft Punk - Get Lucky"

# Use extra mirrors (merged with the configured defaults)
python monochrome_cli.py --mirrors https://mirror1.com https://mirror2.com "search query"
```

### Album search

Search for an album and download one or more matching releases. Album results are listed for selection; numbers like `1,3,5` or `1-3`, or the keyword `all`, download multiple at once. Use `0` to cancel.

```bash
# Search and download an album
python monochrome_cli.py -a "artist - album title"

# Multi-select examples shown by the prompt:
#   1,3,5      -> albums #1, #3, and #5
#   1-3        -> albums #1, #2, and #3
#   all        -> every album in the list
#   0          -> cancel
python monochrome_cli.py -a "artist name"
```

### Discography search

List every album for an artist and download one or more. Album results are presented for selection, so you can pick a single release, a subset, or the full set.

```bash
# Full discography (tracks limited to the artist)
python monochrome_cli.py -d "artist name"

# Non-strict discography: also include tracks from other artists that match
python monochrome_cli.py -d --no-strict "artist name"
```

### CLI Options

| Flag | Description |
|------|-------------|
| `-a`, `--album` | Search for albums and bulk download (supports multi-select) |
| `-d`, `--discography` | Search for artist discography and download all albums |
| `--no-strict` | In discography mode, include tracks from other artists matching the query |
| `-n`, `--limit` | Tracks per search page (default: 50) |
| `--pages` | Max search pages for discography (default: 5) |
| `-q`, `--quality` | Stream quality: `LOW`, `HIGH`, `LOSSLESS`, `HI_RES_LOSSLESS` (default: `HIGH`) |
| `-o`, `--output` | Output directory (default: `downloads/`) |
| `--mirrors` | Additional Monochrome mirror URLs (merged with defaults) |
| `--qobuz-mirrors` | Additional Qobuz mirror URLs (merged with defaults) |
| `--status` | Check availability of all configured mirrors and exit |
| `--csv` | Path to a CSV playlist file for bulk download |
| `--fix-extensions DIR` | Walk DIR and rename files whose extension does not match their actual audio container (FLAC / M4A / MP3) |

### Examples

```bash
# Check mirror status before downloading
python monochrome_cli.py --status

# Bulk download from a CSV playlist
python monochrome_cli.py --csv playlist.csv

# Rename misnamed files (e.g. .flac files that are actually M4A) in a folder
python monochrome_cli.py --fix-extensions ~/Music
```

### CSV Playlists

You can bulk-download tracks from a CSV playlist export (e.g., from Spotify):

```bash
python monochrome_cli.py --csv my_playlist.csv
```

The tool reads `Track Name` and `Artist Name(s)` columns, searches each track, and downloads the first match. All tracks are saved into a folder named after the CSV file (without extension) inside the output directory.

**Supported CSV columns:**
- `Track Name`
- `Artist Name(s)`

**Progress tracking:** During CSV downloads, an overall progress bar shows:
- Processed count / total tracks
- Downloaded, skipped, failed, and missing counts
- Estimated time remaining

**Summary:** After completion, a summary table shows:
- Downloaded tracks
- Skipped (already existed)
- Failed downloads
- Missing data (CSV rows with empty fields)

Rows missing either column are skipped. If a track can't be found, it's logged and the script continues with the next row.

### Format Detection & Fix-Extensions

Some mirrors (especially at `LOW` quality, but occasionally at `HIGH`) return **M4A (ISO BMFF)** audio even when the file was requested as FLAC. Previously the CLI would save those bytes as `.flac`, which made tools like `file(1)` report `ISO Media, MP4 Base Media v1` for a `.flac` file.

The downloader now auto-detects the real container from the response's `Content-Type` header and a magic-byte sniff of the saved bytes, and writes the file with the matching extension. Recognised formats:

| Format | Extensions | Magic bytes | Common Content-Types |
|--------|------------|-------------|----------------------|
| FLAC   | `.flac`    | `fLaC` (offset 0)            | `audio/flac`, `audio/x-flac` |
| M4A    | `.m4a`     | `ftyp` box (offset 4)        | `audio/mp4`, `audio/m4a`, `audio/aac` |
| MP3    | `.mp3`     | `ID3` tag or `0xFFEx` sync   | `audio/mpeg`, `audio/mp3` |

The file is written to `Artist - Title.<ext>.tmp` and atomically renamed once the format is confirmed — a misnamed file is never left on disk.

**Migrating an existing library.** If you already have a directory of `.flac` files that are actually M4A, run the migration walker to rename them in place:

```bash
python monochrome_cli.py --fix-extensions ~/Music
```

It scans every regular file under the directory, sniffs the first 16 bytes, and renames mismatches (e.g. `Pizza Hotline - AIR.flac` → `Pizza Hotline - AIR.m4a`). It skips non-audio files, already-correctly-named files, and any case where the target path is already occupied. The exit status is 0 either way; a summary of scanned / renamed counts is printed at the end.

## Configuration

Configuration is loaded from JSON files in the following priority:

1. **Local config:** `./config.json` (in the working directory)
2. **Global config:** `~/.config/monochrome-cli/config.json`

If no config file is found, built-in defaults are used.

### Config File Format

Create `config.json` (or `~/.config/monochrome-cli/config.json`):

 ```json
{
  "output_dir": "~/Music",
  "quality": "LOSSLESS",
  "monochrome_mirrors": [
    "https://extra-monochrome-mirror-1.com",
    "https://extra-monochrome-mirror-2.com"
  ],
  "qobuz_mirrors": [
    "https://extra-qobuz-mirror-1.com",
    "https://extra-qobuz-mirror-2.com"
  ]
}
```

### Config Options

| Key | Type | Description |
|-----|------|-------------|
| `output_dir` | string | Default download directory |
| `quality` | string | Default stream quality: `LOW`, `HIGH`, `LOSSLESS`, or `HI_RES_LOSSLESS` (default: `HIGH`) |
| `monochrome_mirrors` | list of strings | Custom Monochrome API mirror URLs |
| `qobuz_mirrors` | list of strings | Custom Qobuz API mirror URLs |

### Priority

1. **CLI flags** (`--mirrors`, `--qobuz-mirrors`, `-o`, `-q`) override everything
2. **Config file** values override built-in defaults
3. **Built-in defaults** are used as fallback

### Environment Variables

The output directory and default quality can be set via environment variables:

```bash
export MONOCHROME_DL_OUTPUT=~/Music
export MONOCHROME_DL_QUALITY=LOSSLESS
```

These override the config file but are overridden by the `-o` and `-q` CLI flags, respectively.

## How It Works

- **Search:** Queries all configured Monochrome mirrors in parallel and merges results for maximum coverage.
- **Mirror Selection:** Automatically tracks mirror reliability in `mirror-stats.json` and prioritizes the most successful mirrors.
- **Downloads:** Saves tracks as `.flac` files, organized by artist and album when downloading albums or discographies.
- **Fallback:** If a track's ISRC is available and Monochrome fails, the tool automatically tries Qobuz mirrors as a backup source.
