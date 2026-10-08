# music-findr

A web UI for [spotDL](https://github.com/spotDL/spotify-downloader). Search Spotify for artists, albums, songs and playlists (or paste a Spotify link), open them, and download a song, an album, a playlist or an artist's whole discography straight into your music folder. Live progress shows in the Downloads panel.

No Spotify account or API keys needed. spotDL finds each song on YouTube Music and tags the file (artist, album, track number, cover art, lyrics).

## Run it

```yaml
services:
  music-findr:
    build: .            # this repo
    image: music-findr:latest
    container_name: music-findr
    network_mode: host
    environment:
      - PORT=8800
    volumes:
      - /data/music:/music
    restart: unless-stopped
```

```sh
docker compose up -d --build
```

Then open `http://<server>:8800`. It uses the same port as `spotdl web`, so stop your old spotdl container first, or set `PORT`.

## Settings

All optional. Set them under `environment:`.

| Variable  | Default | What it does |
|-----------|---------|--------------|
| `OUTPUT`  | `{album-artist}/{album}/{track-number} - {title}.{output-ext}` | Where each file goes inside `/music`. Uses [spotDL's template variables](https://spotdl.readthedocs.io/en/latest/usage/#output). For spotDL's flat layout, use `{artists} - {title}.{output-ext}`. |
| `FORMAT`  | `mp3` | `mp3`, `flac`, `ogg`, `opus`, `m4a` or `wav` |
| `BITRATE` | `128k` | For example `320k`, or `disable` to keep the source quality |
| `THREADS` | `4` | How many songs download at the same time |
| `PORT`    | `8800` | The port the web UI listens on |

## How downloads behave

- **Already have it:** songs already in the folder are skipped, so downloading an album twice is safe.
- **Discography** downloads every album and single, one release at a time. Compilations are left out. When the same recording turns up on several releases (single and album, clean and explicit, deluxe), it's saved once.
- **Couldn't download:** each song that fails gets one automatic retry. **Retry** on a finished download runs it again, and only what's missing downloads.
- **Restarts:** the queue is saved in `/music/.music-findr/`. After a restart or crash, anything that was waiting or running starts again, and songs that were cut off mid-write are cleaned up first.
- **Stop** ends a running download after the songs in progress.

## Your library

**Library** shows what's in your music folder by artist and album, read from the files' tags. Spotify pages mark songs you already have.

- **Organize your music folder** moves existing files into the `OUTPUT` layout (Artist/Album folders by default) using their tags. It shows every move before doing anything, and **Move** applies exactly that preview. It never overwrites or deletes your files: files with missing tags, and files that would land on an existing file, stay where they are. Folders left empty are removed. Keep `FORMAT` the same as your existing files (spotDL's default is mp3) so they're recognised. Run this once if you're coming from spotDL's flat layout; otherwise songs you already have won't be recognised.
- **Get missing songs**, on each artist or on every artist at once, finds the artist on Spotify and queues their discography. Songs you have come back as "Already there", so a finished download tells you what was missing.

## Rate limits

Spotify lookups use the same anonymous access as the Spotify web player, so there's no API key to get banned. Audio comes from YouTube, which can slow down or block heavy use. If many songs show "Couldn't download", lower `THREADS` and use **Retry** later.

## Updating

YouTube changes often, and the fix is usually a newer yt-dlp. Rebuild to pick it up:

```sh
docker compose build --pull --no-cache && docker compose up -d
```

## Development

```sh
uv venv -p 3.12 .venv && VIRTUAL_ENV=.venv uv pip install "spotdl~=4.5.2" "httpx<0.28"
mkdir -p music && cd music && ../.venv/bin/uvicorn --app-dir .. app:app --reload --port 8800
../.venv/bin/python ../test_app.py   # self-check (calls Spotify for real)
```

`app.py` is the whole backend. `static/index.html` is the whole UI, with no build step.
