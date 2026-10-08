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

    raw["status"] = "downloading"  # a running job can't be removed
    assert client.delete(f"/api/downloads/{job['job']}").status_code == 409
    raw["status"] = "queued"
    app.run(raw, None, lambda urls: [])  # nothing resolved -> failed with a reason
    assert raw["status"] == "failed" and raw["error"]


if __name__ == "__main__":
    check_links()
    check_jobs()
    check_browse()
    print("ok")
