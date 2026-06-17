# monochrome-cli

> [!IMPORTANT]
> This repository does not contain any copyrighted material, or code to illegaly download music. Downloads are provided by the Qobuz API and should only be initiated by the API token owner. The author is **not responsible for the usage of this repository nor endorses it**, nor is the author responsible for any copies, forks, re-uploads made by other users, or anything else related to [monochrome-cli](https://github.com/kraibse/monochrome-cli) or [Qobuz-DL](https://github.com/QobuzDL/Qobuz-DL). Any live demo found online of this project is not associated with the authors of this repo. This is the author's only account and repository.

A standalone Python CLI for searching and downloading music from Monochrome API mirrors.


## Installation

Requires Python 3.9+ and the following packages:

```bash
pip install requests rich
```

Or using the requirements file:

```bash
pip install -r requirements.txt
```

## Usage

### Basic Commands

```bash
# Search and download a single track
python monochrome_cli.py "artist - song title"

# Search and download an album
python monochrome_cli.py -a "artist - album title"

# In an interactive terminal, results open a TUI picker (see below).
# In the legacy text prompt (piped / CI / --no-tui), you can pick multiple
# albums at once, e.g.:
#   1,3,5      -> albums #1, #3, and #5
#   1-3        -> albums #1, #2, and #3
#   all        -> every album in the list
#   0          -> cancel
python monochrome_cli.py -a "artist name"

# Search and download a full discography
python monochrome_cli.py -d "artist name"

# Non-strict discography (includes tracks from other artists that match the query)
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
| `--tui` | Force the interactive TUI picker (default on a TTY) |
| `--no-tui` | Disable the TUI picker; use the legacy text prompts (handy for piping / CI) |

### Examples

```bash
# Download with specific quality
python monochrome_cli.py -q LOSSLESS "Pink Floyd - Time"

# Save to a specific folder
python monochrome_cli.py -o ~/Music "Daft Punk - Get Lucky"

# Use custom mirrors
python monochrome_cli.py --mirrors https://mirror1.com https://mirror2.com "search query"

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

### Live Progress UI

Album, discography, and CSV downloads use a stacked live progress display that stays compact (no scrolling) and shows aggregate counters as tracks complete. The display re-renders in place via `rich.live.Live`; per-track status messages (skips, failures, stream-URL errors) are collected during the download and printed as a single notes table once the live region closes.

```
┌──────────────────────────────────────────────────────────────┐
│  Album 2/5 — Album                                           │
│  AIR by Pizza Hotline                                        │
│  8 track(s) → downloads/Pizza Hotline/AIR                    │
└──────────────────────────────────────────────────────────────┘

Overall  [████████░░░░░░░░░░] 3/8   ✓3  ⊘1  ✗0
Track   [3/8] Intravenus  [██████████] 4.2/8.2 MB  12.4 MB/s  0:00:02
```

After the download finishes:

```
┏━━━━━━━━━━━━━━━━┳━━━━━━━┓
┃ Status         ┃ Count  ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━┩
│ Downloaded     │     3  │
│ Skipped        │     1  │
│ Failed         │     0  │
└────────────────┴───────┘

Per-track notes
  1. [yellow][skip] Track 4: already exists[/yellow]
  2. [red][skip] Track 5: failed to get stream URL: mirror 503[/red]
```

Discography runs each album in sequence with its own live region; the album panel header is `Album 1/5 — Album`, `Album 2/5 — Album`, etc. CSV playlists get the same layout with an extra `?{missing}` counter for rows that had no track/artist data.

### TUI Picker

When you run the CLI in an interactive terminal (and stdin/stdout are both TTYs), the search-result list for single-track and album modes is shown as a keyboard-driven picker. There are no numbers to type — the highlighted row is your cursor, and you can mark many rows at once.

```
┏━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━┓
┃   ┃ Artist   ┃ Title                ┃ Album       ┃ Quality ┃
┡━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━┩
│[x]│ Pink Fl… │ Time                 ┃ The Dark S… ┃ LOSELES…│   <- cursor row (reverse)
│[ ]│ Pink Fl… │ Money                ┃ The Dark S… ┃ LOSELES…│
│[-]│ Daft Punk│ Get Lucky            ┃ Random Acc… ┃ HIGH    │   <- excluded (dim)
└───┴──────────┴──────────────────────┴─────────────┴─────────┘
↑/↓ move · space mark · ⌫ exclude · a all · enter confirm · q quit
3 marked · 1 excluded · 12 total
```

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move the cursor (clamped at the top/bottom). |
| `Space` | Toggle "mark for download" on the cursor row. Press again to unmark. On an excluded row, the first `Space` only re-includes it. |
| `Backspace` | Toggle "exclude" on the cursor row — visible but ignored on confirm. Press `Backspace` again to un-exclude. |
| `a` | Toggle mark-all. Marks every non-excluded row; press again to clear. |
| `Enter` | Confirm. If you marked rows, those are returned. If nothing is marked, the row under the cursor is used. |
| `q` / `Esc` / `Ctrl-C` / `Ctrl-D` | Cancel. Nothing is downloaded. |

The TUI is auto-detected from the TTY. Force it explicitly with `--tui`, or force the legacy prompts with `--no-tui` (recommended for pipes, CI, and any environment where stdin/stdout are not attached to a real terminal).

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
| `monochrome_mirrors` | list of strings | Custom Monochrome API mirror URLs |
| `qobuz_mirrors` | list of strings | Custom Qobuz API mirror URLs |

### Priority

1. **CLI flags** (`--mirrors`, `--qobuz-mirrors`, `-o`) override everything
2. **Config file** values override built-in defaults
3. **Built-in defaults** are used as fallback

### Environment Variable

You can also set the output directory via environment variable:

```bash
export MONOCHROME_DL_OUTPUT=~/Music
```

This overrides the config file but is overridden by the `-o` CLI flag.

## How It Works

- **Search:** Queries all configured Monochrome mirrors in parallel and merges results for maximum coverage.
- **Mirror Selection:** Automatically tracks mirror reliability in `mirror-stats.json` and prioritizes the most successful mirrors.
- **Downloads:** Saves tracks as `.flac` files, organized by artist and album when downloading albums or discographies.
- **Fallback:** If a track's ISRC is available and Monochrome fails, the tool automatically tries Qobuz mirrors as a backup source.

## Notes

- Downloaded files are saved as `.flac`.
- Existing files are automatically skipped to avoid re-downloading duplicates.
- **Bulk mode summaries:** After album, discography, or CSV downloads, a summary table shows how many tracks were downloaded, skipped, or failed.
- The tool tracks mirror success rates locally in `mirror-stats.json` to improve future reliability.
