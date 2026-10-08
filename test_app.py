"""Self-check: python test_app.py (hits Spotify live for the browse endpoints)."""

from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

import json
import tempfile
import time
from pathlib import Path

import app

app.STATE = Path(tempfile.mkdtemp()) / "jobs.json"  # keep the real queue file out of it
app.SAVED = app.STATE.parent / "playlists.json"
client = TestClient(app.app)
client.headers["X-Requested-With"] = "music-findr"  # what the UI sends on every change


def check_spotdl_loads_before_threads():
    """spotdl's modules import each other; loading them from two threads at once
    deadlocks (it killed the download worker on startup). They must all load
    when the app loads, before any thread starts."""
    import subprocess
    import sys

    code = ("import sys, app; mods = ['spotdl.download.downloader', 'spotdl.utils.search', 'spotdl.utils.spotify', "
            "'spotdl.utils.metadata', 'spotdl.utils.formatter', 'spotdl.types.song', 'mutagen.flac']; "
            "missing = [m for m in mods if m not in sys.modules]; assert not missing, missing")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]


def check_worker_survives_a_crash():
    job = client.post("/api/downloads", json={"kind": "track", "id": "boom"}).json()
    raw = next(j for j in app.jobs if j["job"] == job["job"])
    real_work = app.work
    app.work = lambda *a, **k: 1 / 0
    try:
        app.serve_one(raw, None, None)  # must not raise: the worker thread lives on
    finally:
        app.work = real_work
    assert raw["status"] == "failed" and "ZeroDivisionError" in raw["error"], raw
    with app.lock:
        app.jobs.remove(raw)


def check_discographies_wait_their_turn():
    """Songs, albums and playlists you add run before queued discographies (which
    can take hours), in the order you added them."""
    while not app.pending.empty():
        app.pending.get_nowait()
    for kind, sid_ in [("artist", "a1"), ("artist", "a2"), ("playlist", "p1"), ("album", "b1")]:
        client.post("/api/downloads", json={"kind": kind, "id": sid_})
    order = [app.pending.get_nowait()[2]["id"] for _ in range(4)]
    assert order == ["p1", "b1", "a1", "a2"], order
    with app.lock:
        app.jobs.clear()


def check_links():
    for q, want in [
        ("https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE?si=x", ("album", "6dVIqQ8qmQ5GBnJ9shOYGE")),
        ("https://open.spotify.com/intl-de/artist/4Z8W4fKeB5YxbusRsdQVPb", ("artist", "4Z8W4fKeB5YxbusRsdQVPb")),
        ("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", ("playlist", "37i9dQZF1DXcBWIGoYBM5M")),
    ]:
        link = client.get("/api/search", params={"q": q}).json()["link"]
        assert (link["type"], link["id"]) == want, link


def check_browse():
    s = client.get("/api/search", params={"q": "radiohead"}).json()
    for kind in ("tracks", "artists", "albums", "playlists"):
        assert s[kind] and s[kind][0]["id"] and s[kind][0]["name"], kind
    a = client.get("/api/artist/4Z8W4fKeB5YxbusRsdQVPb").json()
    assert a["name"] == "Radiohead" and a["top"] and len(a["albums"]) > 10, a.keys()
    al = client.get("/api/album/6dVIqQ8qmQ5GBnJ9shOYGE").json()
    assert al["name"] == "OK Computer" and len(al["tracks"]) == al["total"] == 12
    p = client.get("/api/playlist/37i9dQZF1DXcBWIGoYBM5M").json()
    assert p["name"] and p["tracks"] and p["tracks"][0]["artists"]


def song(url, name, duration=200, artist="A"):
    return NS(url=url, name=name, artists=[artist], duration=duration, display_name=f"{artist} - {name}")


def check_cross_site_guard():
    import os

    here = os.getcwd()
    os.chdir(tempfile.mkdtemp())  # if the guard ever breaks, organize runs on an empty folder
    try:
        stranger = TestClient(app.app)  # e.g. a form or fetch on some other website
        assert stranger.post("/api/organize", params={"apply": True}).status_code == 403
        assert stranger.post("/api/downloads", json={"kind": "track", "id": "abc"}).status_code == 403
        assert stranger.get("/api/downloads").status_code == 200  # reading is fine
    finally:
        os.chdir(here)


def check_jobs():
    songs = [song(f"u{i}", f"Song {i}") for i in range(3)]
    outcome = {"u0": "x.mp3", "u1": "y.mp3", "u2": None}  # u2 fails, and fails its retry
    calls = []

    def download_multiple_songs(batch):
        calls.append([s.url for s in batch])
        for s in batch:
            if s.url == "u0":
                app.on_progress(NS(song=s, progress=100), "Skipped")
            if s.url == "u1":
                app.on_progress(NS(song=s, progress=60), "Downloading")
        return [(s, outcome[s.url]) for s in batch]

    job = client.post("/api/downloads", json={"kind": "track", "id": "abc"}).json()
    queued = client.post("/api/downloads", json={"kind": "album", "id": "def"}).json()
    assert client.post("/api/downloads", json={"kind": "show", "id": "x"}).status_code == 422
    assert client.delete(f"/api/downloads/{queued['job']}").status_code == 200

    raw = next(j for j in app.jobs if j["job"] == job["job"])
    app.run(raw, NS(download_multiple_songs=download_multiple_songs), lambda urls: songs)
    assert calls == [["u0", "u1", "u2"], ["u2"]], calls  # one retry for the failure
    done = client.get("/api/downloads").json()
    assert len(done) == 1 and done[0]["title"] == "A - Song 0", done
    assert [s["status"] for s in done[0]["songs"]] == ["skipped", "done", "failed"]
    assert (done[0]["status"], done[0]["finished"], done[0]["failed"]) == ("done", 3, 1)

    raw["status"], raw["songs"] = "queued", {}
    app.run(raw, None, lambda urls: [])  # nothing resolved -> failed with a reason
    assert raw["status"] == "failed" and raw["error"]

    # a discography goes release by release, saves each recording once,
    # and a stop request lands between batches
    real_urls = app.spotify_urls
    app.spotify_urls = lambda job: ["r1", "r2", "r3"]
    try:
        disco = client.post("/api/downloads", json={"kind": "artist", "id": "art", "title": "A"}).json()
        raw = next(j for j in app.jobs if j["job"] == disco["job"])
        per_release = {
            "r1": [song("a1", "Hit"), song("a2", "Intro", 60), song("a3", "Deep Cut")],
            # deluxe edition: same Hit (1s longer), a different Intro, one new song
            "r2": [song("b1", "HIT", 201), song("b2", "Intro", 95), song("b3", "B-Side")],
            "r3": [song("c1", "Never Reached")],
        }
        seen = []

        def fake_download(batch):
            seen.append([s.url for s in batch])
            if raw["step"] == 2:  # user hits Stop while release 2 downloads
                assert client.delete(f"/api/downloads/{raw['job']}").json()["stopping"]
            return [(s, "f.mp3") for s in batch]

        app.run(raw, NS(download_multiple_songs=fake_download), lambda urls: per_release[urls[0]], batch=2)
        assert seen == [["a1", "a2"], ["a3"], ["b2", "b3"]], seen
        status = {url: s["status"] for url, s in raw["songs"].items()}
        assert status["b1"] == "duplicate" and status["b2"] == status["b3"] == "done", status
        assert (raw["status"], raw["step"], raw["steps"], len(raw["songs"])) == ("stopped", 2, 3, 6)
    finally:
        app.spotify_urls = real_urls


def check_organize_and_library():
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import APIC, ID3

    root = Path(tempfile.mkdtemp())
    silence = root / "silence.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "1", "-q:a", "9", str(silence)], check=True)
    png = b"\x89PNG\r\n\x1a\nfake"

    def tagged(rel, title, track, cover=False, album_artist="Radiohead"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(silence, path)
        tags = EasyID3()
        tags.update({"title": title, "artist": "Radiohead", "albumartist": album_artist,
                     "album": "OK Computer", "tracknumber": f"{track}/12"})
        tags.save(path)
        if cover:
            id3 = ID3(path)
            id3.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover", data=png))
            id3.save()

    tagged("Radiohead - Airbag.mp3", "Airbag", 1, cover=True)  # flat spotdl layout
    (root / "Radiohead - Airbag.lrc").write_text("[00:01.00]lyrics")
    tagged("old/Radiohead - Lucky.mp3", "Lucky", 11)  # its folder empties out
    tagged("Radiohead/OK Computer/03 - Subterranean Homesick Alien.mp3", "Subterranean Homesick Alien", 3)
    tagged("dupe/Radiohead - Airbag.mp3", "Airbag", 1)  # wants the same spot as the first
    tagged("sneaky.mp3", "Sneaky", 1, album_artist="..")  # would plan ../OK Computer/...
    silence.rename(root / "untagged.mp3")
    here = os.getcwd()
    os.chdir(root)
    try:
        assert client.get("/api/library").json()["unorganized"] == 2
        preview = client.post("/api/organize").json()
        assert preview["counts"] == {"moves": 2, "conflicts": 1, "untagged": 2}, preview
        assert "sneaky.mp3" in preview["untagged"]  # never planned outside the folder
        assert preview["in_place"] == 1 and not preview["applied"]
        assert (root / "Radiohead - Airbag.mp3").exists()  # a preview moves nothing

        # apply only does what was previewed: a changed folder needs a new check
        stale = client.post("/api/organize", params={"apply": True, "plan": "not-the-plan"})
        assert stale.status_code == 409 and (root / "Radiohead - Airbag.mp3").exists()
        app.remember("plx", "Linked playlist")  # its paths change when files move
        done = client.post("/api/organize", params={"apply": True, "plan": preview["plan"]}).json()
        assert any(j["kind"] == "playlist" and j["id"] == "plx" for j in app.jobs)  # re-synced to the new paths
        app.forget_playlist("plx")
        assert done["moved"] == 2 and not (root.parent / "OK Computer").exists()
        album = root / "Radiohead" / "OK Computer"
        assert sorted(p.name for p in album.iterdir()) == [
            "01 - Airbag.lrc", "01 - Airbag.mp3", "03 - Subterranean Homesick Alien.mp3", "11 - Lucky.mp3"]
        assert not (root / "old").exists()
        assert (root / "dupe" / "Radiohead - Airbag.mp3").exists() and (root / "untagged.mp3").exists()
        again = client.post("/api/organize").json()
        assert again["counts"]["moves"] == 0 and again["in_place"] == 3

        (root / "broken.mp3").write_bytes(b"not audio")  # e.g. cut off mid-download
        lib = client.get("/api/library").json()
        songs = lib["songs"]
        assert len(songs) == 6 and "broken.mp3" not in {s["path"] for s in songs}, songs
        assert lib["unorganized"] == 0
        airbag = next(s for s in songs if s["path"] == "Radiohead/OK Computer/01 - Airbag.mp3")
        assert (airbag["track"], airbag["album"], airbag["art"]) == (1, "OK Computer", True), airbag
        cover = client.get("/api/library/cover", params={"path": airbag["path"]})
        assert cover.content == png and cover.headers["content-type"] == "image/png"
        assert client.get("/api/library/cover", params={"path": "untagged.mp3"}).status_code == 404
        assert client.get("/api/library/cover", params={"path": "broken.mp3"}).status_code == 404
        assert client.get("/api/library/cover", params={"path": "../../etc/passwd"}).status_code == 404

        app.library.acquire()  # a download is running
        assert client.post("/api/organize", params={"apply": True, "plan": again["plan"]}).status_code == 409
        app.library.release()
    finally:
        os.chdir(here)
        shutil.rmtree(root)


def check_library_artist():
    by_song = client.get("/api/library/artist", params={"name": "radiohead", "track": "7c378mlmubSu7NGkLFa4sN"}).json()
    by_name = client.get("/api/library/artist", params={"name": "Radiohead"}).json()
    assert by_song["id"] == by_name["id"] == "4Z8W4fKeB5YxbusRsdQVPb", (by_song, by_name)
    assert client.get("/api/library/artist", params={"name": "zzqx no such artist qq"}).status_code == 404


def check_restart():
    import os
    import time

    folder = app.STATE.parent
    cut = folder / "cut.mp3"  # being written when the server stopped
    cut.write_bytes(b"half an mp3")
    finished = folder / "finished.mp3"  # done before the stop, so left alone
    finished.write_bytes(b"x")
    older = folder / "older.mp3"  # predates the job, so not ours to touch
    older.write_bytes(b"x")
    os.utime(older, (0, 0))

    def entry(status, path):
        return {"name": "n", "artists": [], "status": status, "progress": 0, "message": "", "path": str(path)}

    base = {"kind": "album", "id": "a", "title": "A", "subtitle": None, "image": None, "error": None,
            "step": 1, "steps": 1, "current": 3, "stop": False, "started": time.time() - 60}
    app.STATE.write_text(json.dumps([
        {**base, "job": "j1", "status": "downloading", "songs": {
            "u1": entry("downloading", cut), "u2": entry("done", finished), "u3": entry("queued", older)}},
        {"job": "garbled"},  # a damaged entry is skipped, not fatal
        {**base, "job": "j0", "status": "done", "songs": {}},
    ]))
    with app.lock:
        app.jobs.clear()
    while not app.pending.empty():
        app.pending.get_nowait()

    app.restore()
    assert not cut.exists() and finished.exists() and older.exists()
    assert [j["job"] for j in app.jobs] == ["j1", "j0"], [j["job"] for j in app.jobs]
    assert (app.jobs[0]["status"], app.jobs[0]["songs"]) == ("queued", {})
    assert app.pending.get_nowait()[2] is app.jobs[0] and app.pending.empty()

    assert client.post("/api/downloads/j0/retry").json()["status"] == "queued"
    assert client.post("/api/downloads/j1/retry").status_code == 409
    client.post("/api/downloads", json={"kind": "track", "id": "zzz"})
    assert json.loads(app.STATE.read_text())[0]["id"] == "zzz"


def check_reconnect():
    """A lookup on a broken connection reconnects with a fresh session and tries once more."""
    from spotapi.exceptions import RequestError

    broken = RequestError("Failed to complete request.", error="curl: (35) TLS connect error")
    real_query, sessions = app.Song.query_songs, []

    def flaky(self, *args, **kwargs):
        sessions.append(self.base)
        if len(sessions) == 1:
            raise broken
        return real_query(self, *args, **kwargs)

    app.Song.query_songs = flaky
    try:
        r = client.get("/api/search", params={"q": "radiohead"})
        assert r.status_code == 200 and r.json()["albums"], r.text
        assert sessions[0] is not sessions[1]  # the retry ran on a fresh session
    finally:
        app.Song.query_songs = real_query



def check_spotify_waits():
    """Spotify unreachable or limiting us for a whole job: the queue waits and
    tries again (instead of failing every queued job in a few seconds)."""
    from spotapi.exceptions import BaseClientError, RequestError

    broken = RequestError("Failed to complete request.", error="curl: (35) TLS connect error")
    limited = BaseClientError("Could not get session", error="Status Code: 429, Response: <!DOCTYPE html>...")
    real_urls, tries, naps = app.spotify_urls, [], []

    def down_twice(job):
        tries.append(1)
        if len(tries) <= 2:
            raise broken if len(tries) == 1 else limited
        return ["r1"]

    app.spotify_urls = down_twice
    try:
        job = client.post("/api/downloads", json={"kind": "artist", "id": "out", "title": "Outage"}).json()
        raw = next(j for j in app.jobs if j["job"] == job["job"])

        def nap(seconds):
            naps.append(seconds)
            detail = "curl: (35)" if len(naps) == 1 else "rate-limiting"
            assert raw["status"] == "waiting" and detail in raw["error"] and "<" not in raw["error"], raw["error"]

        one = [song("o1", "Only Song")]
        app.work(raw, NS(download_multiple_songs=lambda b: [(s, "f.mp3") for s in b]), lambda urls: one, sleep=nap)
        assert raw["status"] == "done" and naps == [60, 120] and raw["error"] is None, (raw["status"], naps)

        # removing a waiting job stops the waiting
        tries.clear()
        again = client.post("/api/downloads", json={"kind": "artist", "id": "out2", "title": "Gone"}).json()
        raw = next(j for j in app.jobs if j["job"] == again["job"])
        app.work(raw, None, lambda urls: one, sleep=lambda s: client.delete(f"/api/downloads/{raw['job']}"))
        assert raw["status"] == "removed" and len(tries) == 1
    finally:
        app.spotify_urls = real_urls


def check_playlists():
    import os

    here = os.getcwd()
    os.chdir(tempfile.mkdtemp())
    real_cover, real_jf = app.playlist_cover, dict(app.JELLYFIN)
    app.playlist_cover = lambda pid: {"id": pid, "name": f"List {pid}", "owner": "me", "image": None, "color": None}
    try:
        with app.lock:
            app.jobs.clear()
        app.saved.clear()

        # downloading a playlist saves it for syncing
        job = client.post("/api/downloads", json={"kind": "playlist", "id": "pl1", "title": "Road Trip"}).json()
        saved = client.get("/api/playlists").json()["playlists"]
        assert [(p["id"], p["name"]) for p in saved] == [("pl1", "Road Trip")], saved

        # when it finishes, a .m3u8 points at the files in playlist order (failed ones left out)
        raw = next(j for j in app.jobs if j["job"] == job["job"])

        def entry(name, path, status, pos):
            return {"name": name, "artists": ["A"], "status": status, "progress": 100, "message": "",
                    "path": path, "pos": pos, "secs": 200}

        raw["songs"] = {
            "u2": entry("Second", "A/Album/02 - Second.mp3", "skipped", 2),
            "u1": entry("First", "A/Album/01 - First.mp3", "done", 1),
            "u3": entry("Missing", "A/Album/03 - Missing.mp3", "failed", 3),
        }
        raw["status"] = "done"
        app.publish(raw)
        m3u = Path("Playlists/Road Trip.m3u8").read_text().splitlines()
        assert m3u == ["#EXTM3U", "#PLAYLIST:Road Trip", "#EXTINF:200,A - First", "../A/Album/01 - First.mp3",
                       "#EXTINF:200,A - Second", "../A/Album/02 - Second.mp3"], m3u
        info = client.get("/api/playlists").json()["playlists"][0]
        assert info["songs"] == 2 and info["file"] == "Playlists/Road Trip.m3u8" and info["synced"], info

        # syncing is daily: not due yet, due a day later, never twice at once
        assert app.due(time.time()) == [] and [p["id"] for p in app.due(time.time() + 86400)] == ["pl1"]
        assert client.post("/api/playlists/pl1/sync").status_code == 200
        assert client.post("/api/playlists/pl1/sync").status_code == 409  # already queued
        assert app.due(time.time() + 86400) == []

        # export -> import round trip (comments and junk lines ignored)
        text = client.get("/api/playlists/export").text
        assert "# Road Trip" in text and "https://open.spotify.com/playlist/pl1" in text, text
        extra = "\nhello\nspotify:album:abc123\nhttps://open.spotify.com/playlist/pl2?si=x\n"
        r = client.post("/api/import", json={"text": text + extra}).json()
        assert (r["queued"], r["ignored"]) == (2, ["hello"]), r  # pl1 is already queued
        assert {p["id"]: p["name"] for p in client.get("/api/playlists").json()["playlists"]}["pl2"] == "List pl2"

        # stop syncing forgets it (files stay)
        assert client.delete("/api/playlists/pl2").status_code == 200
        assert "pl2" not in {p["id"] for p in client.get("/api/playlists").json()["playlists"]}

        # with Jellyfin set up, the playlist is made there right away from the songs
        # Jellyfin already knows, then topped up once its scan finds the new ones
        jf = {"library": [{"Id": "j1", "Path": "/data/music/A/Album/01 - First.mp3"},
                          {"Id": "jx", "Path": "/data/music/B/Other/02 - Second.mp3"}],
              "playlist": None, "seen_at_create": None, "calls": []}
        new_song = {"Id": "j2", "Path": "/data/music/A/Album/02 - Second.mp3"}

        def fake(method, path, params=None, json=None):
            jf["calls"].append((method, path))
            if path == "/Users":
                return [{"Id": "u-admin", "Name": "chase", "Policy": {"IsAdministrator": True}}]
            if path == "/Library/Refresh":
                jf["library"].append(new_song)  # the scan finds the new download
            if path == "/ScheduledTasks":
                return [{"Key": "RefreshLibrary", "State": "Idle"}]
            if path == "/Items":
                return {"Items": list(jf["library"])}
            if path == "/Playlists":
                assert json["UserId"] == "u-admin", json
                jf["playlist"], jf["seen_at_create"] = list(json["Ids"]), list(json["Ids"])
                return {"Id": "jp1"}
            if path == "/Playlists/jp1/Items" and method == "GET":
                return {"Items": [{"PlaylistItemId": f"e{i}"} for i, _ in enumerate(jf["playlist"])]}
            if path == "/Playlists/jp1/Items" and method == "DELETE":
                jf["playlist"] = []
            if path == "/Playlists/jp1/Items" and method == "POST":
                jf["playlist"] += params["ids"].split(",")
            return None

        app.JELLYFIN.update(url="http://jf:8096", key="k", user="")
        app.jellyfin, real_request = fake, app.jellyfin
        try:
            Path("Playlists/Road Trip.m3u8").unlink()
            app.publish(raw, background=False)
            assert jf["seen_at_create"] == ["j1"], jf  # made before any scan
            assert jf["calls"].index(("POST", "/Playlists")) < jf["calls"].index(("POST", "/Library/Refresh"))
            assert jf["playlist"] == ["j1", "j2"], jf  # topped up after it
            info = next(p for p in client.get("/api/playlists").json()["playlists"] if p["id"] == "pl1")
            assert info["jellyfin_id"] == "jp1" and "2 of 2" in info["jellyfin"], info
            assert not Path("Playlists/Road Trip.m3u8").exists()

            # nothing new to find: no scan at all
            jf["calls"].clear()
            app.publish(raw, background=False)
            assert ("POST", "/Library/Refresh") not in jf["calls"] and jf["playlist"] == ["j1", "j2"], jf
        finally:
            app.jellyfin = real_request

        # the owner is shown once, however the playlist was added
        app.remember("own", owner="By Chase")
        assert app.saved["own"]["owner"] == "Chase"

        # after a restart, playlists whose Jellyfin push was cut off sync again,
        # and owners saved by older versions ("By Chase") are cleaned up
        app.SAVED.write_text(json.dumps({
            "cut": {"id": "cut", "name": "Rocky 3", "owner": "By Chase", "image": None, "synced": 1, "songs": 661,
                    "jellyfin": "Waiting for Jellyfin to pick up new songs…"},
            "ok": {"id": "ok", "name": "Fine", "owner": None, "image": None, "synced": 1, "songs": 2,
                   "jellyfin": "In Jellyfin: 2 of 2 songs"}}))
        # "had": its last download is in the history, so it's re-published right
        # away from there (no queue, no Spotify); "cut" has none, so it re-syncs
        stored = json.loads(app.SAVED.read_text())
        stored["had"] = {**stored["cut"], "id": "had", "name": "Had"}
        app.SAVED.write_text(json.dumps(stored))
        with app.lock:
            app.jobs[:] = [{"job": "old", "kind": "playlist", "id": "had", "status": "done", "title": "Had",
                            "songs": {"u": {"status": "skipped", "path": "A/a.mp3"}}}]
        app.saved.clear()
        republished, real_publish = [], app.publish
        app.publish = lambda job, background=True: republished.append(job["job"])
        try:
            app.load_saved()
        finally:
            app.publish = real_publish
        assert app.saved["cut"]["owner"] == "Chase" and republished == ["old"], republished
        assert [(j["id"], j["subtitle"]) for j in app.jobs if j["status"] == "queued"] == [("cut", "By Chase")]

        # a sync that fails (say the playlist was deleted on Spotify) waits a full
        # interval before trying again, rather than piling up a failed job every hour
        with app.lock:
            app.jobs.clear()
        app.saved.clear()
        app.remember("gone", "Deleted list")
        later = time.time() + 2 * 86400
        assert app.sync_due(later) == 1
        app.jobs[0]["status"] = "failed"
        assert app.sync_due(later + 3600) == 0 and app.sync_due(later + 86400) == 1

        # download history keeps the newest 300 finished jobs
        with app.lock:
            app.jobs[:] = [{"job": str(i), "kind": "track", "id": "x", "status": "done", "songs": {}} for i in range(400)]
        client.post("/api/downloads", json={"kind": "track", "id": "newest"})
        assert len(app.jobs) == 301 and app.jobs[0]["id"] == "newest" and app.jobs[-1]["job"] == "299"
    finally:
        app.playlist_cover = real_cover
        app.JELLYFIN.clear()
        app.JELLYFIN.update(real_jf)
        os.chdir(here)


def check_playlist_reuses_library():
    """A playlist that lists another release (remaster, single...) of a song you
    have, or a song sitting unorganized in the folder, links your copy instead."""
    import os
    import shutil
    import subprocess

    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, WOAS

    root = Path(tempfile.mkdtemp())
    silence = root / "silence.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "1", "-q:a", "9", str(silence)], check=True)

    def have(rel, title, track_id):  # tagged the way spotdl tags its downloads
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(silence, path)
        tags = EasyID3()
        tags.update({"title": title, "artist": "Radiohead", "albumartist": "Radiohead", "album": "OK Computer",
                     "tracknumber": "1/12"})
        tags.save(path)
        id3 = ID3(path)
        id3.add(WOAS(url=f"https://open.spotify.com/track/{track_id}"))
        id3.save()

    have("Radiohead/OK Computer/01 - Airbag.mp3", "Airbag", "orig1")
    have("Radiohead - Lucky.mp3", "Lucky", "lucky1")  # not organized yet
    def track(tid, name, secs=1):
        return NS(url=f"https://open.spotify.com/track/{tid}", name=name, artists=["Radiohead"], duration=secs,
                  display_name=f"Radiohead - {name}", list_position=None)
    listed = [track("rem1", "Airbag - Remastered 2017"),  # same recording, other release
              track("lucky1", "Lucky", 250),                # same Spotify song, flat file
              track("tourist", "Airbag - Remastered", 16),  # namesake 15s off: still linked
              track("live1", "Airbag - Live"),              # a different recording
              track("new1", "Brand New")]                   # not in the library
    here = os.getcwd()
    os.chdir(root)
    try:
        with app.lock:
            app.jobs.clear()
        job = client.post("/api/downloads", json={"kind": "playlist", "id": "mix", "title": "Mix"}).json()
        raw = next(j for j in app.jobs if j["job"] == job["job"])
        fetched = []

        def download(batch):
            fetched.extend(s.name for s in batch)
            return [(s, "x.mp3") for s in batch]

        app.run(raw, NS(download_multiple_songs=download), lambda urls: listed)
        assert fetched == ["Airbag - Live", "Brand New"], fetched
        assert {u.rsplit("/", 1)[-1] for u, e in raw["songs"].items() if e["status"] == "skipped"} == {"rem1", "lucky1", "tourist"}
        by_url = {u.rsplit("/", 1)[-1]: e for u, e in raw["songs"].items()}
        assert by_url["rem1"]["path"] == "Radiohead/OK Computer/01 - Airbag.mp3" and by_url["rem1"]["status"] == "skipped"
        assert by_url["lucky1"]["path"] == "Radiohead - Lucky.mp3"
        m3u = Path("Playlists/Mix.m3u8").read_text()
        assert "../Radiohead/OK Computer/01 - Airbag.mp3" in m3u and "../Radiohead - Lucky.mp3" in m3u, m3u
    finally:
        os.chdir(here)
        shutil.rmtree(root)
    for title, plain in [("Airbag - Remastered", "airbag"), ("Airbag (2011 Remaster)", "airbag"),
                         ("Airbag - 2009 Remastered Version", "airbag"), ("Airbag - Live", "airbag - live")]:
        assert app.plain_title(title) == plain, (title, app.plain_title(title))


def check_home():
    h = client.get("/api/home").json()
    assert len(h["chart"]) == 10 and h["new"] and len(h["playlists"]) >= 6, {k: len(v) for k, v in h.items()}


if __name__ == "__main__":
    check_spotdl_loads_before_threads()
    check_worker_survives_a_crash()
    check_discographies_wait_their_turn()
    check_links()
    check_cross_site_guard()
    check_jobs()
    check_organize_and_library()
    check_restart()
    check_spotify_waits()
    check_playlists()
    check_playlist_reuses_library()
    check_browse()
    check_reconnect()
    check_home()
    check_library_artist()
    print("ok")
