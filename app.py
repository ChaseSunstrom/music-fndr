"""music-findr: browse Spotify and pull music into your library with spotdl.

Files land relative to the working directory (/music in the container),
exactly like `spotdl web --web-use-output-dir`.
"""

import logging
import os
import queue
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from spotapi import Artist, PublicAlbum, PublicPlaylist, Song

SETTINGS = {
    "output": os.environ.get(
        "OUTPUT", "{album-artist}/{album}/{track-number} - {title}.{output-ext}"
    ),
    "format": os.environ.get("FORMAT", "mp3"),
    "bitrate": os.environ.get("BITRATE", "128k"),
    "threads": int(os.environ.get("THREADS", "4")),
    "simple_tui": True,
}
LINK = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?|spotify:)"
    r"(track|album|artist|playlist)[/:]([A-Za-z0-9]+)"
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("music-findr")
app = FastAPI(title="music-findr")


@app.exception_handler(Exception)
def upstream_error(_: Request, exc: Exception):
    log.exception("request failed")
    return JSONResponse({"detail": f"Spotify lookup failed: {exc}"}, status_code=502)


# --- Spotify (partner API via spotapi, no credentials) -> small JSON ---------


def sid(uri: str) -> str:
    return uri.rsplit(":", 1)[-1]


def image(sources, want=300):
    """Smallest image at least `want` px wide, else the largest there is."""
    sources = [s for s in sources or [] if s.get("url")]
    if not sources:
        return None
    size = lambda s: s.get("width") or s.get("maxWidth") or 0  # noqa: E731
    big = sorted((s for s in sources if size(s) >= want), key=size)
    return (big[0] if big else max(sources, key=size))["url"]


def color(art) -> Optional[str]:
    return (((art or {}).get("extractedColors") or {}).get("colorDark") or {}).get("hex")


def each(fn, items):
    out = []
    for item in items or []:
        try:
            out.append(fn(item))
        except (KeyError, TypeError, AttributeError):
            pass  # unavailable / region-locked entries come back as stubs
    return out


def people(obj):
    return [
        {"id": sid(a["uri"]), "name": a["profile"]["name"]}
        for a in (obj or {}).get("items", [])
    ]


def track(t, cover=None):
    album = t.get("albumOfTrack") or {}
    return {
        "id": sid(t["uri"]),
        "name": t["name"],
        "artists": people(t.get("artists")),
        "album": {"id": sid(album["uri"]), "name": album.get("name")}
        if album.get("uri")
        else None,
        "image": image((album.get("coverArt") or {}).get("sources")) or cover,
        "ms": (t.get("duration") or t.get("trackDuration") or {}).get(
            "totalMilliseconds"
        ),
        "explicit": (t.get("contentRating") or {}).get("label") == "EXPLICIT",
    }


def release(a, size=300):
    date = a.get("date") or {}
    return {
        "id": sid(a["uri"]),
        "name": a["name"],
        "type": (a.get("type") or "album").lower(),
        "artists": people(a.get("artists")),
        "year": date.get("year") or (date.get("isoString") or "")[:4] or None,
        "image": image((a.get("coverArt") or {}).get("sources"), size),
        "color": color(a.get("coverArt")),
    }


def artist_card(a):
    avatar = (a.get("visuals") or {}).get("avatarImage") or {}
    return {
        "id": sid(a["uri"]),
        "name": a["profile"]["name"],
        "image": image(avatar.get("sources")),
    }


def playlist_card(p, size=300):
    art = ((p.get("images") or {}).get("items") or [{}])[0]
    return {
        "id": sid(p["uri"]),
        "name": p["name"],
        "owner": ((p.get("ownerV2") or {}).get("data") or {}).get("name"),
        "image": image(art.get("sources"), size),
        "color": color(art),
    }


_base = Song().base  # shared, so tokens and query hashes are fetched once


def sp(cls, *args):
    """A spotapi object on the shared base (~0.6s a lookup instead of ~2.5s)."""
    obj = cls(*args)
    obj.base = _base
    return obj


def discography(artist_id: str, section: str):
    return [
        r
        for page in sp(Artist).paginate_artist_discography(artist_id, section=section)
        for item in page
        for r in each(release, item["releases"]["items"])
    ]


@app.get("/api/search")
def search(q: str):
    if m := LINK.search(q):
        return {"link": {"type": m[1], "id": m[2]}}
    s = sp(Song).query_songs(q, limit=12)["data"]["searchV2"]
    return {
        "tracks": each(lambda i: track(i["item"]["data"]), s["tracksV2"]["items"]),
        "artists": each(lambda i: artist_card(i["data"]), s["artists"]["items"]),
        "albums": each(lambda i: release(i["data"]), s["albumsV2"]["items"]),
        "playlists": each(lambda i: playlist_card(i["data"]), s["playlists"]["items"]),
    }


@app.get("/api/artist/{artist_id}")
def artist(artist_id: str):
    with ThreadPoolExecutor(4) as pool:
        sections = {
            s: pool.submit(discography, artist_id, s)
            for s in ("albums", "singles", "compilations")
        }
        a = sp(Artist).get_artist(artist_id)["data"]["artistUnion"]
        avatar = (a.get("visuals") or {}).get("avatarImage") or {}
        header = (a.get("headerImage") or {}).get("data") or {}
        return {
            "id": artist_id,
            "name": a["profile"]["name"],
            "image": image(avatar.get("sources"), 640),
            "header": image(header.get("sources"), 1600),
            "color": color(avatar),
            "listeners": (a.get("stats") or {}).get("monthlyListeners"),
            "top": each(lambda i: track(i["track"]), a["discography"]["topTracks"]["items"]),
            **{s: f.result() for s, f in sections.items()},
        }


@app.get("/api/album/{album_id}")
def album(album_id: str):
    # ponytail: one page of up to 500 tracks; box sets beyond that show truncated
    # (the download itself resolves every track through spotdl)
    a = sp(PublicAlbum, album_id).get_album_info(limit=500)["data"]["albumUnion"]
    info = release(a, 640)
    return {
        **info,
        "label": a.get("label"),
        "total": a["tracksV2"].get("totalCount"),
        "tracks": each(lambda i: track(i["track"], info["image"]), a["tracksV2"]["items"]),
    }


@app.get("/api/playlist/{playlist_id}")
def playlist(playlist_id: str):
    p = sp(PublicPlaylist, playlist_id).get_playlist_info(limit=500)["data"]["playlistV2"]
    return {
        **playlist_card(p, 640),
        "description": p.get("description"),
        "total": p["content"].get("totalCount"),
        "tracks": each(lambda i: track(i["itemV2"]["data"]), p["content"]["items"]),
    }


# --- Downloads: one worker thread owning a spotdl Downloader -----------------
# ponytail: jobs live in memory (history clears on restart) and run one at a
# time; songs inside a job download THREADS at once.

jobs: list = []
pending: queue.Queue = queue.Queue()
lock = threading.Lock()
active: dict = {}  # {"job": job} while downloading, for the progress callback
SONG_STATUS = {"Done": "done", "Skipped": "skipped", "Error": "failed"}


class DownloadRequest(BaseModel):
    kind: str
    id: str
    title: Optional[str] = None
    subtitle: Optional[str] = None
    image: Optional[str] = None


def view(job):
    songs = list(job["songs"].values())
    return {
        **{k: v for k, v in job.items() if k != "songs"},
        "songs": songs,
        "total": len(songs),
        "finished": sum(s["status"] in ("done", "skipped", "failed") for s in songs),
        "failed": sum(s["status"] == "failed" for s in songs),
    }


@app.post("/api/downloads")
def enqueue(req: DownloadRequest):
    if req.kind not in ("track", "album", "artist", "playlist"):
        raise HTTPException(422, "kind must be track, album, artist or playlist")
    if not re.fullmatch(r"[A-Za-z0-9]+", req.id):
        raise HTTPException(422, "id must be a Spotify id")
    job = {
        **req.model_dump(),
        "job": uuid.uuid4().hex[:10],
        "status": "queued",
        "error": None,
        "songs": {},
    }
    with lock:
        jobs.insert(0, job)
    pending.put(job)
    return view(job)


@app.get("/api/downloads")
def downloads():
    with lock:
        return [view(j) for j in jobs]


@app.delete("/api/downloads/{job_id}")
def remove(job_id: str):
    with lock:
        job = next((j for j in jobs if j["job"] == job_id), None)
        if job is None:
            raise HTTPException(404, "No such download")
        if job["status"] in ("resolving", "downloading"):
            raise HTTPException(409, "That download is running; wait for it to finish")
        job["status"] = "removed"  # the worker skips it if it was still queued
        jobs.remove(job)
    return {"ok": True}


def spotify_urls(job):
    if job["kind"] == "artist":
        return [
            f"https://open.spotify.com/album/{r['id']}"
            for section in ("albums", "singles")
            for r in discography(job["id"], section)
        ]
    return [f"https://open.spotify.com/{job['kind']}/{job['id']}"]


def on_progress(tracker, message):
    song = active.get("job", {}).get("songs", {}).get(tracker.song.url)
    if song is not None:
        song.update(
            progress=tracker.progress,
            message=message,
            status=SONG_STATUS.get(message, "downloading"),
        )


def run(job, downloader, resolve):
    """Resolve a job's songs, then download them, recording status as we go."""
    with lock:
        if job["status"] != "queued":
            return
        job["status"] = "resolving"
    try:
        songs = resolve(spotify_urls(job))
        if not songs:
            raise LookupError("Spotify returned no songs for this")
        job["songs"] = {
            s.url: {
                "name": s.name,
                "artists": s.artists,
                "status": "queued",
                "progress": 0,
                "message": "",
            }
            for s in songs
        }
        job["title"] = job["title"] or songs[0].display_name
        job["status"] = "downloading"
        active["job"] = job
        for song, path in downloader.download_multiple_songs(songs):
            entry = job["songs"][song.url]
            if entry["status"] not in ("done", "skipped"):
                entry["status"] = "done" if path else "failed"
        job["status"] = "done"
    except Exception as exc:  # keep the worker alive; show the reason in the UI
        log.exception("download %s failed", job["job"])
        job["status"], job["error"] = "failed", str(exc)
    finally:
        active.clear()


def worker():
    # Imported here so the API (and its tests) load without ffmpeg/YouTube setup.
    from spotdl.download.downloader import Downloader
    from spotdl.utils.search import parse_query
    from spotdl.utils.spotify import SpotifyClient

    SpotifyClient.init(client_id="", client_secret="", no_cache=True)
    downloader = Downloader(SETTINGS)
    downloader.progress_handler.update_callback = on_progress
    resolve = lambda urls: parse_query(urls, threads=SETTINGS["threads"])  # noqa: E731
    log.info("Saving to %s as %s", Path.cwd(), SETTINGS["output"])
    while True:
        run(pending.get(), downloader, resolve)


@app.on_event("startup")
def start_worker():
    threading.Thread(target=worker, daemon=True, name="downloader").start()


app.mount(
    "/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui"
)
