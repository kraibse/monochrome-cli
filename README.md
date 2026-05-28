# monochrome-cli

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

# Search and download a full discography
python monochrome_cli.py -d "artist name"

# Non-strict discography (includes tracks from other artists that match the query)
python monochrome_cli.py -d --no-strict "artist name"
```

### CLI Options

| Flag | Description |
|------|-------------|
| `-a`, `--album` | Search for albums and bulk download |
| `-d`, `--discography` | Search for artist discography and download all albums |
| `--no-strict` | In discography mode, include tracks from other artists matching the query |
| `-n`, `--limit` | Tracks per search page (default: 50) |
| `--pages` | Max search pages for discography (default: 5) |
| `-q`, `--quality` | Stream quality: `LOW`, `HIGH`, `LOSSLESS`, `HI_RES_LOSSLESS` (default: `HIGH`) |
| `-o`, `--output` | Output directory (default: `downloads/`) |
| `--mirrors` | Override Monochrome mirror URLs |
| `--qobuz-mirrors` | Override Qobuz mirror URLs |

### Examples

```bash
# Download with specific quality
python monochrome_cli.py -q LOSSLESS "Pink Floyd - Time"

# Save to a specific folder
python monochrome_cli.py -o ~/Music "Daft Punk - Get Lucky"

# Use custom mirrors
python monochrome_cli.py --mirrors https://mirror1.com https://mirror2.com "search query"
```

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
- Duplicate filenames are automatically numbered (e.g., `song_1.flac`).
- The tool tracks mirror success rates locally in `mirror-stats.json` to improve future reliability.
