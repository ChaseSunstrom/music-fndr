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

Songs that are already in the folder are skipped, so downloading an album twice is safe.

**Discography** downloads every album and single. Compilations are left out because they repeat songs you already have.

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
