"""Self-check: python test_app.py (hits Spotify live for the browse endpoints)."""

from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

import app

client = TestClient(app.app)


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


def check_jobs():
    songs = [NS(url=f"u{i}", name=f"Song {i}", artists=["A"], display_name=f"A - Song {i}") for i in range(3)]

    def download_multiple_songs(batch):
        app.on_progress(NS(song=batch[0], progress=100), "Skipped")
        app.on_progress(NS(song=batch[1], progress=60), "Downloading")
        return [(batch[0], "x.mp3"), (batch[1], "y.mp3"), (batch[2], None)]

    job = client.post("/api/downloads", json={"kind": "track", "id": "abc"}).json()
    queued = client.post("/api/downloads", json={"kind": "album", "id": "def"}).json()
    assert client.post("/api/downloads", json={"kind": "show", "id": "x"}).status_code == 422
    assert client.delete(f"/api/downloads/{queued['job']}").status_code == 200

    raw = next(j for j in app.jobs if j["job"] == job["job"])
    app.run(raw, NS(download_multiple_songs=download_multiple_songs), lambda urls: songs)
    done = client.get("/api/downloads").json()
    assert len(done) == 1 and done[0]["title"] == "A - Song 0", done
    assert [s["status"] for s in done[0]["songs"]] == ["skipped", "done", "failed"]
    assert (done[0]["status"], done[0]["finished"], done[0]["failed"]) == ("done", 3, 1)

    raw["status"], raw["songs"] = "queued", {}
    app.run(raw, None, lambda urls: [])  # nothing resolved -> failed with a reason
    assert raw["status"] == "failed" and raw["error"]

    # a discography goes release by release; stopping it lands between batches
    real_urls = app.spotify_urls
    app.spotify_urls = lambda job: ["r1", "r2", "r3"]
    try:
        disco = client.post("/api/downloads", json={"kind": "artist", "id": "art", "title": "A"}).json()
        raw = next(j for j in app.jobs if j["job"] == disco["job"])
        seen = []

        def fake_download(batch):
            seen.append([s.url for s in batch])
            if raw["step"] == 2:  # user hits Stop while release 2 downloads
                assert client.delete(f"/api/downloads/{raw['job']}").json()["stopping"]
            return [(s, "f.mp3") for s in batch]

        per_release = {u: [NS(url=f"{u}-{i}", name="n", artists=["A"], display_name="A - n") for i in range(3)] for u in ("r1", "r2", "r3")}
        app.run(raw, NS(download_multiple_songs=fake_download), lambda urls: per_release[urls[0]], batch=2)
        assert seen == [["r1-0", "r1-1"], ["r1-2"], ["r2-0", "r2-1"]], seen
        assert (raw["status"], raw["step"], raw["steps"], len(raw["songs"])) == ("stopped", 2, 3, 6)
    finally:
        app.spotify_urls = real_urls


def check_home():
    h = client.get("/api/home").json()
    assert len(h["chart"]) == 10 and h["new"] and len(h["playlists"]) >= 6, {k: len(v) for k, v in h.items()}


if __name__ == "__main__":
    check_links()
    check_jobs()
    check_browse()
    check_home()
    print("ok")
