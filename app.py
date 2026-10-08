"""music-findr: browse Spotify and pull music into your library with spotdl.

Files land relative to the working directory (/music in the container),
exactly like `spotdl web --web-use-output-dir`.
"""

import base64
import hashlib
import itertools
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from mutagen import File as AudioFile
from mutagen.flac import Picture
from spotapi import Artist, PublicAlbum, PublicPlaylist, Song
from spotapi.client import BaseClient
from spotapi.exceptions import BaseClientError, RequestError
from spotapi.http.request import TLSClient

# spotdl's modules import each other, and loading them from two threads at once
# deadlocks (it once killed the download worker at startup), so all of it loads
# here, before any thread starts.
from spotdl.download.downloader import Downloader
from spotdl.types.song import Song as SpotdlSong
from spotdl.utils.formatter import create_file_name, sanitize_string
from spotdl.utils.metadata import get_file_metadata
from spotdl.utils.search import parse_query
from spotdl.utils.spotify import SpotifyClient

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


@app.middleware("http")
async def same_site_only(request: Request, call_next):
    """Changes need a header other websites can't send without a CORS preflight
    (which this app never grants), so a page you visit can't drive it."""
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "music-findr":
        return JSONResponse({"detail": "Missing X-Requested-With: music-findr"}, status_code=403)
    return await call_next(request)


@app.exception_handler(Exception)
def upstream_error(_: Request, exc: Exception):
    log.exception("request failed")
    return JSONResponse({"detail": f"Spotify lookup failed: {why(exc)}"}, status_code=502)


def why(exc: Exception) -> str:
    """The message, plus the connection detail spotapi keeps to itself."""
    detail = str(getattr(exc, "error", None) or "")
    if "429" in detail:
        return "Spotify is rate-limiting this server (429 Too Many Requests); it usually clears up within an hour"
    return f"{exc} ({detail[:200]})" if detail else str(exc)


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


# spotapi starts a fresh session (tokens + query hashes, ~2s) for every object it
# creates, and spotdl's free client creates one per track. Share a single session:
# album lookups drop from ~25s to ~7s. It gets its own connection: spotapi's default
# one is shared by every object, and each new object re-points its login at itself.
# ponytail: patches spotapi internals; drop if spotapi starts reusing sessions itself


def _fresh_session():
    return BaseClient(client=TLSClient("chrome120", "", auto_retries=3))


_session = _fresh_session()


def spotify(call):
    """Run a Spotify lookup. If the connection is broken, start a fresh session
    and try once more, so one bad connection can't take everything down."""
    global _session
    try:
        return call()
    except RequestError as exc:
        log.warning("Spotify connection failed (%s); starting a fresh session", why(exc))
        _session = _fresh_session()
        return call()


def _share_session(cls):
    init = cls.__init__

    def patched(self, *args, **kwargs):
        init(self, *args, **kwargs)
        self.base = _session

    cls.__init__ = patched


for _cls in (Song, Artist, PublicAlbum, PublicPlaylist):
    _share_session(_cls)


def discography(artist_id: str, section: str):
    return spotify(
        lambda: [
            r
            for page in Artist().paginate_artist_discography(artist_id, section=section)
            for item in page
            for r in each(release, item["releases"]["items"])
        ]
    )


@app.get("/api/search")
def search(q: str):
    if m := LINK.search(q):
        return {"link": {"type": m[1], "id": m[2]}}
    s = spotify(lambda: Song().query_songs(q, limit=12))["data"]["searchV2"]
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
        a = spotify(lambda: Artist().get_artist(artist_id))["data"]["artistUnion"]
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
    a = spotify(lambda: PublicAlbum(album_id).get_album_info(limit=500))["data"]["albumUnion"]
    info = release(a, 640)
    return {
        **info,
        "label": a.get("label"),
        "total": a["tracksV2"].get("totalCount"),
        "tracks": each(lambda i: track(i["track"], info["image"]), a["tracksV2"]["items"]),
    }


@app.get("/api/playlist/{playlist_id}")
def playlist(playlist_id: str):
    p = spotify(lambda: PublicPlaylist(playlist_id).get_playlist_info(limit=500))["data"]["playlistV2"]
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
_home_lock = threading.Lock()


def playlist_cover(playlist_id: str):
    try:
        info = spotify(lambda: PublicPlaylist(playlist_id).get_playlist_info(limit=1))
        return playlist_card(info["data"]["playlistV2"])
    except Exception:  # editorial playlists come and go by region
        return None


@app.get("/api/home")
def home():
    """Charts, new releases and big playlists, cached for an hour."""
    with _home_lock:
        if _home["data"] and time.time() - _home["at"] < 3600:
            return _home["data"]
        try:
            return _build_home()
        except Exception:
            if _home["data"]:  # Spotify hiccup: last hour's page beats an error
                log.exception("home refresh failed; serving the old one")
                return _home["data"]
            raise


def _build_home():
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
# Jobs run one at a time (songs inside a job download THREADS at once) and are
# saved to STATE, so a restart picks up whatever was waiting or running.

STATE = Path(".music-findr/jobs.json")  # inside /music; hidden, so the library skips it
RUNNING = ("queued", "waiting", "resolving", "downloading")
KEEP = 300  # finished downloads kept in the history
jobs: list = []
pending: queue.PriorityQueue = queue.PriorityQueue()
_order = itertools.count()


def submit(job):
    """Queue a job. Discographies can take hours, so songs, albums and playlists
    you add go ahead of them; otherwise first come, first served."""
    pending.put((job["kind"] == "artist", next(_order), job))
lock = threading.Lock()
_saving = threading.Lock()
active: dict = {}  # {"job": job} while downloading, for the progress callback
SONG_STATUS = {"Done": "done", "Skipped": "skipped", "Error": "failed"}
FINISHED = ("done", "skipped", "duplicate", "failed")
library = threading.Lock()  # a download and an organize never touch /music at once


class DownloadRequest(BaseModel):
    kind: str
    id: str
    title: Optional[str] = None
    subtitle: Optional[str] = None
    image: Optional[str] = None


def write_json(path: Path, data):
    with _saving:
        try:
            path.parent.mkdir(exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(path)
        except OSError:
            log.exception("couldn't save %s", path)


def save():
    """Write the queue to disk so a restart can carry on with it."""
    with lock:
        snapshot = json.loads(json.dumps(jobs))
    write_json(STATE, snapshot)


def reset(job):
    job.update(status="queued", step=0, steps=0, current=0, stop=False, error=None, songs={})


def restore():
    """Bring back the saved queue; anything that was waiting or running starts again."""
    try:
        saved = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return
    kept = []
    for job in reversed(saved):  # oldest first, the order they were added
        try:
            if job["status"] in RUNNING:
                drop_partial_files(job)
                reset(job)
                submit(job)
            kept.insert(0, job)
        except Exception:  # a damaged entry shouldn't stop the app starting
            log.exception("skipping a saved download I can't read: %s", job)
    with lock:
        jobs[:0] = kept
    log.info("Restored %d downloads, %d to resume", len(kept), pending.qsize())


def drop_partial_files(job):
    """Delete files this job was still writing when the server stopped. spotdl
    would skip them as already there, and they'd stay broken for good."""
    for song in job["songs"].values():
        path = Path(song["path"]) if song.get("path") else None
        if (
            song["status"] not in FINISHED
            and path
            and path.is_file()
            and path.stat().st_mtime >= job.get("started", float("inf"))
            and not read_tags(path).get("name")  # spotdl tags a file once it's complete
        ):
            log.info("Removing %s: the restart cut it off mid-download", path)
            try:
                path.unlink()
            except OSError:
                log.exception("couldn't remove %s", path)


def find(job_id: str):
    job = next((j for j in jobs if j["job"] == job_id), None)
    if job is None:
        raise HTTPException(404, "No such download")
    return job


def view(job):
    songs = list(job["songs"].values())
    return {
        **{k: v for k, v in job.items() if k != "songs"},
        "songs": songs,
        "total": len(songs),
        "finished": sum(s["status"] in FINISHED for s in songs),
        "failed": sum(s["status"] == "failed" for s in songs),
    }


@app.post("/api/downloads")
def enqueue(req: DownloadRequest):
    return queue_download(**req.model_dump())


def queue_download(kind, id, title=None, subtitle=None, image=None):
    if kind not in ("track", "album", "artist", "playlist"):
        raise HTTPException(422, "kind must be track, album, artist or playlist")
    if not re.fullmatch(r"[A-Za-z0-9]+", id):
        raise HTTPException(422, "id must be a Spotify id")
    if kind == "playlist":
        remember(id, title, subtitle, image)
    job = {
        "kind": kind,
        "id": id,
        "title": title,
        "subtitle": subtitle,
        "image": image,
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
        for old in [j for j in jobs if j["status"] not in RUNNING][KEEP:]:
            jobs.remove(old)  # newest first, so these are the oldest finished ones
    submit(job)
    save()
    return view(job)


@app.get("/api/downloads")
def downloads():
    with lock:
        return [view(j) for j in jobs]


@app.delete("/api/downloads/{job_id}")
def remove(job_id: str):
    with lock:
        job = find(job_id)
        if job["status"] in ("resolving", "downloading"):
            job["stop"] = True  # the worker stops after the current batch
            return {"ok": True, "stopping": True}
        job["status"] = "removed"  # the worker skips it if it was still queued
        jobs.remove(job)
    save()
    return {"ok": True}


@app.post("/api/downloads/{job_id}/retry")
def retry(job_id: str):
    """Run a finished, failed or stopped download again; songs already saved are skipped."""
    with lock:
        job = find(job_id)
        if job["status"] in RUNNING:
            raise HTTPException(409, "That download is already queued")
        reset(job)
    submit(job)
    save()
    return view(job)


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


def target(song):
    """Where spotdl will write a song, so a restart can clean up after it."""
    try:
        return str(create_file_name(song, SETTINGS["output"], SETTINGS["format"]))
    except Exception:
        return None


REMASTER = re.compile(r"\s*(?:-\s*|\(|\[)\s*(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?\s*[)\]]?\s*$", re.I)


def plain_title(name: str) -> str:
    """Title without remaster markers: "Airbag - Remastered 2017" is still Airbag
    (but "Airbag - Live" is a different recording)."""
    return REMASTER.sub("", name).strip().casefold()


def library_songs():
    """What's already in the folder: by Spotify track id (spotdl tags it), and by
    (title, main artist) -> [(seconds, file)] for other releases of the same song."""
    by_id, by_name = {}, {}
    for rel, meta in scan(Path.cwd()):
        if not meta.get("name"):
            continue
        url = meta.get("url") or ""
        if "open.spotify.com/track/" in url:
            by_id[url.rsplit("/", 1)[-1]] = str(rel)
        key = (plain_title(meta["name"]), ((meta.get("artists") or [""])[0]).casefold())
        by_name.setdefault(key, []).append(((meta.get("ms") or 0) / 1000, str(rel)))
    return by_id, by_name


def copy_we_have(song, by_id, by_name):
    if song.url.rsplit("/", 1)[-1] in by_id:
        return by_id[song.url.rsplit("/", 1)[-1]]
    key = (plain_title(song.name), (song.artists[0] if song.artists else "").casefold())
    # file lengths come from the YouTube audio, a few seconds (remasters: up to ~15)
    # off Spotify's; title and artist already match, this only rules out a namesake
    return next((path for secs, path in by_name.get(key, []) if abs(secs - song.duration) <= 20), None)


def run(job, downloader, resolve, batch=None):
    """Resolve and download a job one release at a time, in small batches,
    so songs arrive (and show up) early and a stop request lands quickly."""
    batch = batch or SETTINGS["threads"] * 2
    with lock:
        if job["status"] != "queued":
            return
        job["status"], job["started"], job["error"] = "resolving", time.time(), None
    active["job"] = job
    save()
    seen = {}  # (title, main artist) -> durations already in this job

    def repeat(song):
        """The same recording on another release (single + album, deluxe...).
        Spotify gives each release its own track id, so match title, artist
        and length instead."""
        key = (plain_title(song.name), song.artists[0].casefold() if song.artists else "")
        if any(abs(d - song.duration) <= 3 for d in seen.get(key, [])):
            return True
        seen.setdefault(key, []).append(song.duration)
        return False

    def fetch(batch_songs):
        for song, path in downloader.download_multiple_songs(batch_songs):
            entry = job["songs"][song.url]
            if entry["status"] not in ("done", "skipped"):
                entry["status"] = "done" if path else "failed"

    try:
        urls = spotify_urls(job)
        job["steps"] = len(urls)
        for step, url in enumerate(urls, 1):
            if job["stop"]:
                break
            job["step"], job["status"] = step, "resolving"
            try:
                found = [s for s in resolve([url]) if s.url not in job["songs"]]
            except (RequestError, BaseClientError):
                raise  # Spotify itself is unreachable or limiting us: wait, don't skip the rest
            except Exception:  # one unreadable release shouldn't sink a discography
                if len(urls) == 1:
                    raise
                log.exception("skipping %s", url)
                continue
            songs, repeats = [], set()
            for s in found:
                repeats.add(s.url) if repeat(s) else songs.append(s)
            # a playlist links the copy you already have, whatever release it came from
            mine = {}
            if job["kind"] == "playlist":
                index = library_songs()
                mine = {s.url: p for s in songs if (p := copy_we_have(s, *index))}
                songs = [s for s in songs if s.url not in mine]
            new = {
                s.url: {
                    "name": s.name,
                    "artists": s.artists,
                    "status": "duplicate" if s.url in repeats else "skipped" if s.url in mine else "queued",
                    "progress": 0,
                    "message": "Using the copy you have" if s.url in mine else "",
                    "path": mine.get(s.url) or target(s),
                    "pos": getattr(s, "list_position", None),  # order within a playlist
                    "secs": getattr(s, "duration", None),
                }
                for s in found
            }
            job["songs"] = {**job["songs"], **new}  # swap, so readers never see a half-built dict
            job["current"] = len(new)  # the newest `current` songs are this release's
            if found and not job["title"]:
                job["title"] = (job["kind"] == "playlist" and getattr(found[0], "list_name", None)) or found[0].display_name
            job["status"] = "downloading"
            save()
            for i in range(0, len(songs), batch):
                if job["stop"]:
                    break
                fetch(songs[i : i + batch])
                save()
            missed = [s for s in songs if job["songs"][s.url]["status"] == "failed"]
            if missed and not job["stop"]:
                fetch(missed)  # YouTube lookups fail now and then; one retry recovers most
        if job["stop"]:
            job["status"] = "stopped"
        elif not job["songs"]:
            raise LookupError("Spotify returned no songs for this")
        else:
            job["status"] = "done"
        if job["kind"] == "playlist":
            try:
                publish(job)
            except Exception:  # the songs are saved either way
                log.exception("couldn't update the playlist for %s", job["title"])
    except (RequestError, BaseClientError) as exc:  # Spotify unreachable or limiting: work() waits
        log.warning("download %s waiting: %s", job["job"], why(exc))
        job["status"], job["error"] = "waiting", f"Can't reach Spotify: {why(exc)}"
    except Exception as exc:  # keep the worker alive; show the reason in the UI
        log.exception("download %s failed", job["job"])
        job["status"], job["error"] = "failed", why(exc)
    finally:
        active.clear()
        save()


def work(job, downloader, resolve, sleep=time.sleep):
    """Run a job. While Spotify can't be reached, keep it waiting and try again,
    backing off to every 30 min, instead of failing it and everything after it."""
    wait = 60
    while True:
        with library:
            run(job, downloader, resolve)
        if job["status"] != "waiting":
            return
        job["error"] += f". Trying again in {wait // 60} min."
        save()
        sleep(wait)
        with lock:
            if job["status"] != "waiting":  # removed while it waited
                return
            job["status"] = "queued"
        wait = min(wait * 2, 1800)


def serve_one(job, downloader, resolve):
    """Run one job; whatever goes wrong, the worker carries on with the next."""
    try:
        work(job, downloader, resolve)
    except Exception as exc:
        log.exception("download %s crashed", job.get("job"))
        job["status"], job["error"] = "failed", f"Crashed: {type(exc).__name__}: {exc}"
        save()


def worker():
    while True:  # if setup fails (no ffmpeg, no network...), say why and try again
        try:
            if SpotifyClient._instance is None:
                SpotifyClient.init(client_id="", client_secret="", no_cache=True)
            downloader = Downloader(SETTINGS)
            break
        except Exception:
            log.exception("the downloader couldn't start; trying again in a minute")
            time.sleep(60)
    downloader.progress_handler.update_callback = on_progress
    resolve = lambda urls: spotify(lambda: parse_query(urls, threads=SETTINGS["threads"]))  # noqa: E731
    log.info("Saving to %s as %s", Path.cwd(), SETTINGS["output"])
    while True:
        serve_one(pending.get()[2], downloader, resolve)


# --- Organize: move existing files to where OUTPUT says they belong ---------

AUDIO = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav"}
_tags: dict = {}  # path -> (mtime, tags), so later scans only read changed files
_scanning = threading.Lock()  # one scan at a time; the next one reuses its cache


def read_tags(path: Path):
    try:
        meta = get_file_metadata(path) or {}
        length = AudioFile(path).info.length
    except Exception:  # unreadable, or not really audio
        return {}
    meta["has_art"] = bool(meta.pop("album_art", None))
    meta.pop("lyrics", None)  # can be large; nothing here shows them
    meta["ms"] = int(length * 1000)
    return meta


def scan(root: Path):
    """Every audio file under root (skipping hidden/NAS system folders) with its tags."""
    with _scanning:
        return _scan(root)


def _scan(root: Path):
    found = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.suffix.lower() not in AUDIO or any(p.startswith((".", "@")) for p in rel.parts):
            continue
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        cached = _tags.get(path)
        if not cached or cached[0] != mtime:
            cached = _tags[path] = (mtime, read_tags(path))
        found.append((rel, cached[1]))
    seen = {root / rel for rel, _ in found}
    for gone in [p for p in _tags if p.is_relative_to(root) and p not in seen]:
        del _tags[gone]  # moved or deleted since the last scan
    return found


def plan_library(root: Path):
    """Work out each file's OUTPUT path from its tags, using spotdl's own
    naming so a later download of the same song finds it and skips."""
    plan = {"moves": [], "conflicts": [], "untagged": [], "in_place": 0}
    claimed = set()
    for rel, meta in scan(root):
        if not (meta.get("name") and meta.get("artists") and meta.get("album_name")):
            plan["untagged"].append(str(rel))
            continue
        fields = {**meta, "album_artist": meta.get("album_artist") or meta["artists"][0]}
        try:
            target = create_file_name(SpotdlSong.from_missing_data(**fields), SETTINGS["output"], rel.suffix[1:].lower())
        except Exception:
            target = None
        # tags like ".." would point outside the folder; ".x"/"@x" would hide it from the library
        if not target or target.is_absolute() or any(p.startswith((".", "@")) for p in target.parts):
            plan["untagged"].append(str(rel))
        elif target == rel:
            plan["in_place"] += 1
        elif target in claimed or (root / target).exists():
            plan["conflicts"].append({"from": str(rel), "to": str(target)})
        else:
            claimed.add(target)
            plan["moves"].append({"from": str(rel), "to": str(target)})
    # fingerprint of the exact moves, so "Move" only ever does what the preview showed
    plan["plan"] = hashlib.sha1(json.dumps(plan["moves"]).encode()).hexdigest()[:16]
    return plan


def move_files(root: Path, moves):
    """Move files (and same-name .lrc lyrics) without ever overwriting, then
    remove folders the moves left empty."""
    moved, emptied, inside = 0, set(), root.resolve()
    for m in moves:
        src, dst = root / m["from"], root / m["to"]
        if dst.exists() or not src.exists() or inside not in dst.resolve().parents:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        moved += 1
        lrc, lrc_dst = src.with_suffix(".lrc"), dst.with_suffix(".lrc")
        if lrc.exists() and not lrc_dst.exists():
            lrc.rename(lrc_dst)
        emptied.add(src.parent)
    for folder in sorted(emptied, key=lambda d: len(d.parts), reverse=True):
        while folder != root:
            try:
                folder.rmdir()  # only succeeds when empty
            except OSError:
                break
            folder = folder.parent
    return moved


@app.post("/api/organize")
def organize(apply: bool = False, plan: Optional[str] = None):
    """Preview (default) or apply the moves; applying needs the preview's `plan`."""
    if not library.acquire(blocking=False):
        raise HTTPException(409, "Downloads are running; organize when they finish")
    try:
        root = Path.cwd()
        found = plan_library(root)
        if apply and plan != found["plan"]:
            raise HTTPException(409, "Your folder changed since you checked it. Check again before moving.")
        found["moved"] = move_files(root, found["moves"]) if apply else 0
    finally:
        library.release()
    if found["moved"]:  # playlists may link files that just moved
        busy = busy_ids()
        with saved_lock:
            relink = [dict(p) for p in saved.values() if p["id"] not in busy]
        for p in relink:
            queue_download("playlist", p["id"], p["name"], p["owner"] and f"By {p['owner']}", p["image"])
    return {
        **{k: v[:300] if isinstance(v, list) else v for k, v in found.items()},
        "counts": {k: len(v) for k, v in found.items() if isinstance(v, list)},
        "applied": apply,
    }


@app.get("/api/library")
def library_index():
    """Everything in the music folder, one entry per song, for the Library page."""
    songs = []
    for rel, meta in scan(Path.cwd()):
        if not meta:
            continue  # unreadable, e.g. half-written by an interrupted download
        url = meta.get("url") or ""
        songs.append(
            {
                "path": str(rel),
                "id": url.rsplit("/", 1)[-1] if "open.spotify.com/track/" in url else None,
                "name": meta.get("name") or rel.stem,
                "artists": meta.get("artists") or [],
                "album": meta.get("album_name") or "",
                "album_artist": meta.get("album_artist")
                or (meta.get("artists") or ["Unknown artist"])[0],
                "year": meta.get("year"),
                "track": _int(meta.get("track_number")),
                "disc": _int(meta.get("disc_number")) or 1,
                "ms": meta.get("ms"),
                "art": meta.get("has_art", False),
            }
        )
    # songs not where OUTPUT puts them; a discography download would fetch them again
    return {"songs": songs, "unorganized": len(plan_library(Path.cwd())["moves"])}


@app.get("/api/library/artist")
def library_artist(name: str, track: Optional[str] = None):
    """The Spotify artist behind a name in the music folder: read off one of
    their songs when we know its Spotify id, else an exact-name search."""
    wanted = name.casefold()
    if track and re.fullmatch(r"[A-Za-z0-9]+", track):
        t = spotify(lambda: Song().get_track_info(track))["data"]["trackUnion"]
        credits = (t.get("firstArtist") or {}).get("items", []) + (t.get("otherArtists") or {}).get("items", [])
        for a in credits:
            if a["profile"]["name"].casefold() == wanted:
                return {"id": sid(a["uri"]), "name": a["profile"]["name"]}
    hits = spotify(lambda: Song().query_songs(name, limit=5))["data"]["searchV2"]["artists"]["items"]
    for a in each(lambda i: artist_card(i["data"]), hits):
        if a["name"].casefold() == wanted:
            return {"id": a["id"], "name": a["name"]}
    raise HTTPException(404, f"Couldn't find {name} on Spotify")


def _int(value):
    try:
        return int(str(value).split("/")[0])
    except (TypeError, ValueError):
        return None


@app.get("/api/library/cover")
def library_cover(path: str):
    """The cover art embedded in a song file."""
    root = Path.cwd().resolve()
    file = (root / path).resolve()
    if root not in file.parents or file.suffix.lower() not in AUDIO or not file.is_file():
        raise HTTPException(404, "No such song")
    try:
        audio, data = AudioFile(file), None
    except Exception:
        audio = None
    if audio is None:
        raise HTTPException(404, "Not a readable song")
    if hasattr(audio, "pictures") and audio.pictures:  # flac
        data = audio.pictures[0].data
    elif audio.tags is not None:
        if hasattr(audio.tags, "getall") and audio.tags.getall("APIC"):  # mp3
            data = audio.tags.getall("APIC")[0].data
        elif audio.tags.get("covr"):  # m4a
            data = bytes(audio.tags["covr"][0])
        elif audio.tags.get("metadata_block_picture"):  # ogg / opus
            data = Picture(base64.b64decode(audio.tags["metadata_block_picture"][0])).data
    if not data:
        raise HTTPException(404, "No cover in this file")
    kind = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    return Response(data, media_type=kind, headers={"Cache-Control": "max-age=86400"})


# --- Playlists: kept in sync, and pointed at the files (nothing is copied) ---

SAVED = Path(".music-findr/playlists.json")
PLAYLIST_DIR = Path("Playlists")  # .m3u8 files that Jellyfin, Navidrome and players pick up
SYNC_HOURS = float(os.environ.get("SYNC_HOURS", "24"))
JELLYFIN = {
    "url": os.environ.get("JELLYFIN_URL", "").rstrip("/"),
    "key": os.environ.get("JELLYFIN_API_KEY", ""),
    "user": os.environ.get("JELLYFIN_USER", ""),
}
saved: dict = {}  # playlist id -> what the Playlists page shows
saved_lock = threading.RLock()  # requests, the worker, auto-sync and Jellyfin pushes all touch it


def load_saved():
    try:
        stored = json.loads(SAVED.read_text())
    except (OSError, ValueError):
        return
    with saved_lock:
        saved.update(stored)
        for p in saved.values():
            p["owner"] = (p.get("owner") or "").removeprefix("By ") or None  # older versions kept "By "
        cut_off = [dict(p) for p in saved.values() if "Waiting for Jellyfin" in (p.get("jellyfin") or "")]
    busy = busy_ids()
    for p in cut_off:  # a restart stopped its Jellyfin push
        with lock:
            last = next((j for j in jobs if j["kind"] == "playlist" and j["id"] == p["id"]
                         and j["status"] in ("done", "stopped") and j["songs"]), None)
        if last:  # its songs are on disk already: just redo the Jellyfin part, now
            publish(last)
        elif p["id"] not in busy:
            queue_download("playlist", p["id"], p["name"], p["owner"] and f"By {p['owner']}", p["image"])


def store_saved():
    with saved_lock:
        snapshot = json.loads(json.dumps(saved))
    write_json(SAVED, snapshot)


def remember(pid, name=None, owner=None, image=None):
    owner = (owner or "").removeprefix("By ") or None  # the UI passes "By Chase"
    with saved_lock:
        entry = saved.setdefault(pid, {"id": pid, "name": None, "owner": None, "image": None, "synced": 0, "songs": 0})
        entry.update({k: v for k, v in {"name": name, "owner": owner, "image": image}.items() if v})
    store_saved()


def busy_ids():
    with lock:
        return {j["id"] for j in jobs if j["status"] in RUNNING}


def due(now):
    """Saved playlists last synced (or tried) more than SYNC_HOURS ago."""
    if SYNC_HOURS <= 0:
        return []
    busy = busy_ids()
    with saved_lock:
        return [dict(p) for p in saved.values()
                if p["id"] not in busy and now - max(p["synced"], p.get("tried", 0)) >= SYNC_HOURS * 3600]


def sync_due(now):
    """Queue the playlists that are due; returns how many."""
    playlists = due(now)
    for p in playlists:
        with saved_lock:
            saved[p["id"]]["tried"] = now  # a failing sync waits a full interval too
        log.info("Syncing playlist %s", p["name"])
        queue_download("playlist", p["id"], p["name"], p["owner"] and f"By {p['owner']}", p["image"])
    return len(playlists)


def auto_sync():
    while True:
        time.sleep(3600)
        try:
            sync_due(time.time())
        except Exception:  # never let one bad hour stop syncing for good
            log.exception("playlist auto-sync failed")


def publish(job, background=True):
    """After a playlist download, make the playlist: in Jellyfin when it's set up,
    otherwise as a .m3u8 file. Either way it points at the files already there."""
    songs = sorted(
        (s for s in job["songs"].values() if s["status"] in ("done", "skipped") and s.get("path")),
        key=lambda s: s.get("pos") or 0,
    )
    with saved_lock:
        entry = saved.get(job["id"])
        if entry is None:
            return  # stopped syncing in the meantime
        entry.update(name=entry["name"] or job["title"] or "Playlist", songs=len(songs), synced=time.time())
        if JELLYFIN["url"] and JELLYFIN["key"]:
            if entry.get("file"):  # Jellyfin would import the file too, as a duplicate
                Path(entry.pop("file")).unlink(missing_ok=True)
            entry["jellyfin"] = "Waiting for Jellyfin to pick up new songs…"
        else:
            new = str(write_m3u(entry["name"], songs))
            if entry.get("file") and entry["file"] != new:  # renamed on Spotify
                Path(entry["file"]).unlink(missing_ok=True)
            entry["file"] = new
    store_saved()
    if JELLYFIN["url"] and JELLYFIN["key"]:
        push = partial(push_to_jellyfin, entry, [s["path"] for s in songs], settle=10 if background else 0)
        threading.Thread(target=push, daemon=True).start() if background else push()


def write_m3u(name, songs):
    PLAYLIST_DIR.mkdir(exist_ok=True)
    path = PLAYLIST_DIR / f"{sanitize_string(name).lstrip('.') or 'Playlist'}.m3u8"
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for s in songs:
        lines += [f"#EXTINF:{s.get('secs') or -1},{', '.join(s['artists'])} - {s['name']}",
                  os.path.relpath(s["path"], PLAYLIST_DIR)]  # relative, so any mount point works
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def jellyfin(method, path, **kwargs):
    auth = f'MediaBrowser Client="music-findr", Device="music-findr", DeviceId="music-findr", Version="1", Token="{JELLYFIN["key"]}"'
    r = requests.request(method, JELLYFIN["url"] + path, headers={"Authorization": auth}, timeout=60, **kwargs)
    r.raise_for_status()
    return r.json() if r.content else None


def push_to_jellyfin(entry, paths, settle=10):
    """Make the playlist in Jellyfin right away from the songs it already knows,
    then, if some are new downloads, have it scan and top the playlist up."""
    try:
        users = jellyfin("GET", "/Users")
        wanted = JELLYFIN["user"].casefold()
        user = next((u for u in users if u["Name"].casefold() == wanted), None) if wanted else next(
            (u for u in users if u.get("Policy", {}).get("IsAdministrator")), users[0])
        if user is None:
            raise LookupError(f"there's no Jellyfin user called {JELLYFIN['user']}")
        ids = _jellyfin_ids(user, paths)
        _jellyfin_playlist(entry, user, ids)
        if len(ids) < len(paths):
            with saved_lock:
                entry["jellyfin"] = (f"In Jellyfin: {len(ids)} of {len(paths)} songs. "
                                     "Waiting for Jellyfin to scan the new ones…")
            store_saved()
            jellyfin("POST", "/Library/Refresh")
            for _ in range(180):  # wait for the scan, up to 30 min
                time.sleep(settle)
                tasks = jellyfin("GET", "/ScheduledTasks")
                if all(t.get("State") == "Idle" for t in tasks if t.get("Key") == "RefreshLibrary"):
                    break
            ids = _jellyfin_ids(user, paths)
            _jellyfin_playlist(entry, user, ids)
        with saved_lock:
            entry["jellyfin"] = f"In Jellyfin: {len(ids)} of {len(paths)} songs"
    except Exception as exc:
        log.exception("Jellyfin playlist update failed")
        with saved_lock:
            entry["jellyfin"] = f"Couldn't update Jellyfin: {exc}"
    store_saved()


def _jellyfin_ids(user, paths):
    """Jellyfin's ids for our files, in playlist order, matched by path (Jellyfin
    may mount the folder elsewhere, e.g. /data/music)."""
    items = jellyfin("GET", "/Items", params={"Recursive": "true", "IncludeItemTypes": "Audio",
                                              "Fields": "Path", "userId": user["Id"]})["Items"]
    by_name = {}
    for p in paths:
        by_name.setdefault(p.rsplit("/", 1)[-1], []).append(p)
    found = {}
    for item in items:
        path = (item.get("Path") or "").replace("\\", "/")
        for p in by_name.get(path.rsplit("/", 1)[-1], []):
            if path.endswith("/" + p):
                found[p] = item["Id"]
    return [found[p] for p in paths if p in found]


def _jellyfin_playlist(entry, user, ids):
    """Create the playlist, or replace its songs if it's already there."""
    pid = entry.get("jellyfin_id")
    if pid:
        try:
            current = jellyfin("GET", f"/Playlists/{pid}/Items", params={"userId": user["Id"]})["Items"]
        except requests.HTTPError:
            pid = None  # deleted in Jellyfin; make it again
    if pid:
        for chunk in _chunks([i["PlaylistItemId"] for i in current]):
            jellyfin("DELETE", f"/Playlists/{pid}/Items", params={"entryIds": ",".join(chunk)})
        for chunk in _chunks(ids):
            jellyfin("POST", f"/Playlists/{pid}/Items", params={"ids": ",".join(chunk), "userId": user["Id"]})
    else:
        made = jellyfin("POST", "/Playlists", json={"Name": entry["name"], "Ids": ids,
                                                    "UserId": user["Id"], "MediaType": "Audio"})
        pid = made["Id"]
    with saved_lock:
        entry["jellyfin_id"] = pid
    store_saved()


def _chunks(items, size=50):
    return [items[i : i + size] for i in range(0, len(items), size)]


@app.get("/api/playlists")
def playlists():
    with saved_lock:
        listed = json.loads(json.dumps(list(saved.values())))
    return {"playlists": listed, "jellyfin": bool(JELLYFIN["url"] and JELLYFIN["key"]), "sync_hours": SYNC_HOURS}


@app.post("/api/playlists/{pid}/sync")
def sync_playlist(pid: str):
    with saved_lock:
        entry = dict(saved.get(pid) or {})
    if not entry:
        raise HTTPException(404, "That playlist isn't saved")
    if pid in busy_ids():
        raise HTTPException(409, "That playlist is already syncing")
    return queue_download("playlist", pid, entry["name"], entry["owner"] and f"By {entry['owner']}", entry["image"])


@app.delete("/api/playlists/{pid}")
def forget_playlist(pid: str):
    with saved_lock:
        saved.pop(pid, None)
    store_saved()
    return {"ok": True}


@app.get("/api/playlists/export")
def export_playlists():
    lines = [f"# music-findr playlists, {time.strftime('%Y-%m-%d')}",
             "# One Spotify link per line (playlists, albums, artists or songs). Lines starting with # are ignored.", ""]
    with saved_lock:
        for p in saved.values():
            lines += [f"# {p['name']}", f"https://open.spotify.com/playlist/{p['id']}"]
    return Response("\n".join(lines) + "\n", media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="music-findr-playlists.txt"'})


class ImportRequest(BaseModel):
    text: str


@app.post("/api/import")
def import_links(req: ImportRequest):
    """Queue every Spotify link in the text; playlists are saved for syncing."""
    queued, ignored, busy = 0, [], busy_ids()
    for line in req.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = LINK.search(line)
        if not m:
            ignored.append(line[:200])
            continue
        kind, pid = m[1], m[2]
        if pid in busy:
            continue
        card = playlist_cover(pid) if kind == "playlist" else None
        owner = card and card["owner"] and f"By {card['owner']}"
        queue_download(kind, pid, card and card["name"], owner, card and card["image"])
        busy.add(pid)
        queued += 1
    return {"queued": queued, "ignored": ignored}


@app.on_event("startup")
def start_worker():
    restore()
    load_saved()
    threading.Thread(target=worker, daemon=True, name="downloader").start()
    threading.Thread(target=auto_sync, daemon=True, name="playlist-sync").start()


app.mount(
    "/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui"
)
