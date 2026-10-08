"""music-findr: browse Spotify and pull music into your library with spotdl.

Files land relative to the working directory (/music in the container),
exactly like `spotdl web --web-use-output-dir`.
"""

import logging
import os
import queue
import re
import threading
import time
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


HOME_CHART = "37i9dQZEVXbMDoHDwVN2tF"  # Top 50 - Global
HOME_NEW = "37i9dQZF1DX4JAvHpjipBk"  # New Music Friday
HOME_PLAYLISTS = [
    "37i9dQZF1DXcBWIGoYBM5M",  # Today's Top Hits
    "37i9dQZF1DX0XUsuxWHRQd",  # RapCaviar
    "37i9dQZF1DWXRqgorJj26U",  # Rock Classics
    "37i9dQZF1DX4UtSsGT1Sbe",  # All Out 80s
    "37i9dQZF1DX1lVhptIYRda",  # Hot Country
    "37i9dQZF1DX10zKzsJ2jva",  # Viva Latino
    "37i9dQZF1DX4SBhb3fqCJd",  # Are & Be
    "37i9dQZF1DX4dyzvuaRJ0n",  # mint
    "37i9dQZF1DWWMOmoXKqHTD",  # Songs to Sing in the Car
    "37i9dQZF1DX4sWSpwq3LiO",  # Peaceful Piano
]
_home: dict = {"at": 0.0, "data": None}


def playlist_cover(playlist_id: str):
    try:
        info = sp(PublicPlaylist, playlist_id).get_playlist_info(limit=1)
        return playlist_card(info["data"]["playlistV2"])
    except Exception:  # editorial playlists come and go by region
        return None


@app.get("/api/home")
def home():
    """Charts, new releases and big playlists, cached for an hour."""
    if _home["data"] and time.time() - _home["at"] < 3600:
        return _home["data"]
    with ThreadPoolExecutor(6) as pool:
        chart = pool.submit(playlist, HOME_CHART)
        fresh = pool.submit(playlist, HOME_NEW)
        lists = [p for p in pool.map(playlist_cover, HOME_PLAYLISTS) if p]
        albums = {}
        for t in fresh.result()["tracks"]:
            if t["album"] and t["album"]["id"] not in albums:
                albums[t["album"]["id"]] = {**t["album"], "image": t["image"], "artists": t["artists"]}
        _home["data"] = {
            "chart": chart.result()["tracks"][:10],
            "new": list(albums.values())[:18],
            "playlists": lists,
        }
        _home["at"] = time.time()
    return _home["data"]


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
        "step": 0,  # which release of `steps` is being worked on
        "steps": 0,
        "current": 0,
        "stop": False,
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
            job["stop"] = True  # the worker stops after the current batch
            return {"ok": True, "stopping": True}
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


def run(job, downloader, resolve, batch=20):
    """Resolve and download a job one release at a time, in small batches,
    so songs arrive (and show up) early and a stop request lands quickly."""
    with lock:
        if job["status"] != "queued":
            return
        job["status"] = "resolving"
    active["job"] = job
    try:
        urls = spotify_urls(job)
        job["steps"] = len(urls)
        for step, url in enumerate(urls, 1):
            if job["stop"]:
                break
            job["step"], job["status"] = step, "resolving"
            try:
                songs = [s for s in resolve([url]) if s.url not in job["songs"]]
            except Exception:  # one unreadable release shouldn't sink a discography
                if len(urls) == 1:
                    raise
                log.exception("skipping %s", url)
                continue
            new = {
                s.url: {
                    "name": s.name,
                    "artists": s.artists,
                    "status": "queued",
                    "progress": 0,
                    "message": "",
                }
                for s in songs
            }
            job["songs"] = {**job["songs"], **new}  # swap, so readers never see a half-built dict
            job["current"] = len(new)  # the newest `current` songs are this release's
            if songs and not job["title"]:
                job["title"] = songs[0].display_name
            job["status"] = "downloading"
            for i in range(0, len(songs), batch):
                if job["stop"]:
                    break
                for song, path in downloader.download_multiple_songs(songs[i : i + batch]):
                    entry = job["songs"][song.url]
                    if entry["status"] not in ("done", "skipped"):
                        entry["status"] = "done" if path else "failed"
        if job["stop"]:
            job["status"] = "stopped"
        elif not job["songs"]:
            raise LookupError("Spotify returned no songs for this")
        else:
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
