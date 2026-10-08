"""Self-check: python test_app.py (hits Spotify live for the browse endpoints)."""

from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

import json
import tempfile
from pathlib import Path

import app

app.STATE = Path(tempfile.mkdtemp()) / "jobs.json"  # keep the real queue file out of it
client = TestClient(app.app)
client.headers["X-Requested-With"] = "music-findr"  # what the UI sends on every change


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
        done = client.post("/api/organize", params={"apply": True, "plan": preview["plan"]}).json()
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
    assert app.pending.get_nowait() is app.jobs[0] and app.pending.empty()

    assert client.post("/api/downloads/j0/retry").json()["status"] == "queued"
    assert client.post("/api/downloads/j1/retry").status_code == 409
    client.post("/api/downloads", json={"kind": "track", "id": "zzz"})
    assert json.loads(app.STATE.read_text())[0]["id"] == "zzz"


def check_home():
    h = client.get("/api/home").json()
    assert len(h["chart"]) == 10 and h["new"] and len(h["playlists"]) >= 6, {k: len(v) for k, v in h.items()}


if __name__ == "__main__":
    check_links()
    check_cross_site_guard()
    check_jobs()
    check_organize_and_library()
    check_restart()
    check_browse()
    check_home()
    check_library_artist()
    print("ok")
