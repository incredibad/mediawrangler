"""Fake Jellyfin and Silo servers for ingest tests.

Payload shapes copy what the real APIs returned on 2026-10-26 (Jellyfin
10.11.8, Silo v2), trimmed to the fields MediaWrangler reads. Titles are public
show names; IDs and paths are made up or public.
"""

import json
from urllib.parse import parse_qs

import httpx

JF_LIBS = [
    {"Name": "TV", "CollectionType": "tvshows", "ItemId": "jf-lib-tv", "Locations": ["/data/TV"]},
    {
        "Name": "Anime",
        "CollectionType": "tvshows",
        "ItemId": "jf-lib-anime",
        "Locations": ["/data/Anime"],
    },
    {
        "Name": "Movies",
        "CollectionType": "movies",
        "ItemId": "jf-lib-movies",
        "Locations": ["/data/Movies"],
    },
    {
        "Name": "Sport",
        "CollectionType": "tvshows",
        "ItemId": "jf-lib-sport",
        "Locations": ["/data/Sport"],
    },
    {"Name": "Collections", "CollectionType": "boxsets", "ItemId": "jf-lib-box", "Locations": []},
]


def _jf_series(id_, name, year, path, **ids):
    return {
        "Id": id_,
        "Name": name,
        "Type": "Series",
        "ProductionYear": year,
        "Path": path,
        "LocationType": "FileSystem",
        "ProviderIds": {k.capitalize(): v for k, v in ids.items()},
    }


SERIES_DIRS = {
    "jf-sherlock": "/data/TV/Sherlock",
    "jf-poi": "/data/TV/Person of Interest (2011)",
    "jf-stand": "/data/TV/The Stand (1994)",
    "jf-op": "/data/Anime/One Pace",
    "jf-ps": "/data/Anime/Prison School",
}


def ep_name(folder: str, season: int, ep: int) -> str:
    show = folder.rsplit("/", 1)[1]
    season_dir = "Specials" if season == 0 else f"Season {season:02d}"
    return f"{season_dir}/{show} - S{season:02d}E{ep:02d}.mkv"


def _jf_ep(id_, series_id, season, ep, location="FileSystem"):
    folder = SERIES_DIRS[series_id]
    return {
        "Id": id_,
        "Name": f"Ep {ep}",
        "Type": "Episode",
        "SeriesId": series_id,
        "ParentIndexNumber": season,
        "IndexNumber": ep,
        "LocationType": location,
        "Path": f"{folder}/{ep_name(folder, season, ep)}",
        "ProviderIds": {},
    }


JF_ITEMS = {
    ("jf-lib-tv", "Series"): [
        _jf_series(
            "jf-stand",
            "The Stand",
            1994,
            "/data/TV/The Stand (1994)",
            tvdb="71267",
            imdb="tt0108941",
        ),
        _jf_series(
            "jf-sherlock", "Sherlock", 2010, "/data/TV/Sherlock", tvdb="176941", tmdb="19885"
        ),
        _jf_series(
            "jf-poi",
            "Person of Interest",
            2011,
            "/data/TV/Person of Interest (2011)",
            tvdb="248742",
        ),
    ],
    ("jf-lib-tv", "Episode"): [
        _jf_ep("jf-sh-1", "jf-sherlock", 1, 1),
        _jf_ep("jf-sh-2", "jf-sherlock", 1, 2),
        _jf_ep("jf-sh-3", "jf-sherlock", 1, 3),
        _jf_ep("jf-sh-s1", "jf-sherlock", 0, 1),
        _jf_ep("jf-poi-1", "jf-poi", 1, 1),
        _jf_ep("jf-poi-v", "jf-poi", 1, 2, location="Virtual"),
    ],
    ("jf-lib-anime", "Series"): [
        _jf_series("jf-op", "One Piece", 1999, "/data/Anime/One Pace", tvdb="81797", tmdb="37854"),
        _jf_series("jf-ps", "Prison School", 2015, "/data/Anime/Prison School", tvdb="295068"),
    ],
    ("jf-lib-anime", "Episode"): [_jf_ep(f"jf-op-{i}", "jf-op", 1, i) for i in range(1, 6)],
    ("jf-lib-movies", "Movie"): [
        {
            "Id": "jf-heat",
            "Name": "Heat",
            "Type": "Movie",
            "ProductionYear": 1995,
            "Path": "/data/Movies/Heat (1995)/Heat.mkv",
            "LocationType": "FileSystem",
            "ProviderIds": {"Tmdb": "949", "Imdb": "tt0113277"},
        },
    ],
    ("jf-lib-sport", "Series"): [],
    ("jf-lib-sport", "Episode"): [],
}


# What the fakes were asked to write: (method, item id, user).
JF_WRITES: list[tuple[str, str, str]] = []
CREATED: list[tuple[str, str, object]] = []  # app, what, request body
SILO_WRITES: list[tuple[str, str, str, str]] = []  # method, content id, profile, key
SILO_FAIL: set[str] = set()  # content ids whose write fails

JF_GONE: set[str] = set()  # item ids Jellyfin no longer has (a replaced file)

JF_USERS = [{"Id": "u-user1", "Name": "user1"}, {"Id": "u-al", "Name": "user2"}]


def _jf_watch(id_, played=True, position_ticks=0, last="2026-10-20T10:00:00.1234567Z"):
    return {
        "Id": id_,
        "RunTimeTicks": 36_000_000_000,
        "UserData": {
            "Played": played,
            "PlayCount": 1 if played else 0,
            "PlaybackPositionTicks": position_ticks,
            "LastPlayedDate": last,
        },
    }


# Mutable so tests can change what a user has watched between syncs.
JF_WATCH = {
    "u-user1": [
        _jf_watch("jf-sh-1"),
        _jf_watch("jf-sh-2"),
        _jf_watch("jf-sh-s1"),
        _jf_watch("jf-poi-1", played=False, position_ticks=6_000_000_000),
        _jf_watch("jf-heat"),
    ],
    "u-al": [_jf_watch("jf-sh-1")],
}


def jellyfin_handler(page_size: int = 2):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Token=" in request.headers.get("authorization", "")
        if request.url.path == "/Library/VirtualFolders":
            return httpx.Response(200, json=JF_LIBS)
        if request.url.path == "/Users":
            return httpx.Response(200, json=JF_USERS)
        if request.url.path == "/Users/New":
            CREATED.append(("jellyfin", "user", json.loads(request.content)))
            return httpx.Response(200, json={"Id": "u-new", "Name": "new"})
        if request.url.path == "/Users/u-new":
            return httpx.Response(200, json={"Id": "u-new", "Policy": {"IsAdministrator": False}})
        if request.url.path == "/Users/u-new/Policy":
            CREATED.append(("jellyfin", "policy", json.loads(request.content)))
            return httpx.Response(204)
        if request.url.path.startswith("/UserPlayedItems/"):
            if request.url.path.rsplit("/", 1)[1] in JF_GONE:
                return httpx.Response(404)
            q = parse_qs(request.url.query.decode())
            JF_WRITES.append((request.method, request.url.path.rsplit("/", 1)[1], q["userId"][0]))
            return httpx.Response(200, json={"Played": request.method == "POST"})
        if request.url.path == "/Items" and "ids=" in request.url.query.decode().lower():
            q = parse_qs(request.url.query.decode())
            wanted = (q.get("ids") or q.get("Ids"))[0].split(",")
            return httpx.Response(
                200,
                json={
                    "Items": [{"Id": i} for i in wanted if i not in JF_GONE],
                    "TotalRecordCount": len(wanted),
                },
            )
        if request.url.path == "/Items" and "userId" in request.url.query.decode():
            q = parse_qs(request.url.query.decode())
            played = q["Filters"][0] == "IsPlayed"
            rows = [
                r for r in JF_WATCH.get(q["userId"][0], []) if r["UserData"]["Played"] == played
            ]
            start = int(q["StartIndex"][0])
            return httpx.Response(
                200, json={"Items": rows[start : start + page_size], "TotalRecordCount": len(rows)}
            )
        if request.url.path == "/Items":
            q = parse_qs(request.url.query.decode())
            rows = JF_ITEMS[(q["ParentId"][0], q["IncludeItemTypes"][0])]
            start = int(q["StartIndex"][0])
            limit = min(int(q["Limit"][0]), page_size)  # force paging in tests
            return httpx.Response(
                200, json={"Items": rows[start : start + limit], "TotalRecordCount": len(rows)}
            )
        return httpx.Response(404)

    return handler


SILO_LIBS = [
    {"id": "2", "name": "Shows", "type": "series", "paths": ["/mnt/media/TV"], "enabled": True},
    {"id": "3", "name": "Anime", "type": "series", "paths": ["/mnt/media/Anime"], "enabled": True},
    {
        "id": "1",
        "name": "Movies",
        "type": "movies",
        "paths": ["/mnt/media/Movies"],
        "enabled": True,
    },
]


def _card(cid, title, year, typ, status="matched", **extra):
    return {"content_id": cid, "title": title, "year": year, "type": typ, "status": status, **extra}


def _ep(series, s, e):
    return _card(
        f"episode-tvdb-{series}-{s}-{e}",
        f"Ep {e}",
        2010,
        "episode",
        series_id=f"series-tvdb-{series}",
        season_number=s,
        episode_number=e,
    )


SILO_CATALOG = {
    ("2", "series"): [
        _card("series-tvdb-372051", "The Stand", 2020, "series"),  # wrong TVDB match
        _card("series-tvdb-176941", "Sherlock", 2010, "series"),
        _card("series-tvdb-248742", "Person of Interest", 2011, "series"),
    ],
    ("2", "episode"): [_ep("176941", 1, 1), _ep("176941", 1, 2), _ep("248742", 1, 1)],
    ("3", "series"): [
        _card(
            "local-onepace-a",
            "[One Pace][1-3] Romance Dawn [1080p]",
            0,
            "series",
            status="unmatched",
        ),
        _card(
            "local-onepace-b",
            "[One Pace][4-7] Orange Town [1080p]",
            0,
            "series",
            status="unmatched",
        ),
        _card("local-prison", "Prison School", 2015, "series", status="unmatched"),
    ],
    ("3", "episode"): [],
    ("1", "movie"): [_card("movie-imdb-tt0113277", "Heat", 1995, "movie")],
}

SILO_DETAILS = {
    "series-tvdb-372051": {
        "tvdb_id": "372051",
        "imdb_id": "tt0108941",
        "folder_paths": ["/mnt/media/TV/The Stand (1994)"],
    },
    "series-tvdb-176941": {
        "tvdb_id": "176941",
        "tmdb_id": "19885",
        "folder_paths": ["/mnt/media/TV/Sherlock"],
    },
    "series-tvdb-248742": {
        "tvdb_id": "248742",
        "folder_paths": ["/mnt/media/TV/Person of Interest (2011)"],
    },
    "local-onepace-a": {
        "folder_paths": ["/mnt/media/Anime/One Pace/[One Pace][1-3] Romance Dawn [1080p]"]
    },
    "local-onepace-b": {
        "folder_paths": ["/mnt/media/Anime/One Pace/[One Pace][4-7] Orange Town [1080p]"]
    },
    "local-prison": {"folder_paths": ["/mnt/media/Anime/Prison School"]},
    "movie-imdb-tt0113277": {
        "imdb_id": "tt0113277",
        "tmdb_id": "949",
        "versions": [{"file_path": "/mnt/media/Movies/Heat (1995)/Heat.mkv"}],
    },
}


SILO_ROOTS = [
    {
        "library_id": "2",
        "root_path": "/mnt/media/TV/Sherlock",
        "state": "resolved",
        "observed_file_count": 4,
        "content_id": "series-tvdb-176941",
    },
    {
        "library_id": "2",
        "root_path": "/mnt/media/TV/Person of Interest (2011)",
        "state": "resolved",
        "observed_file_count": 1,
        "content_id": "series-tvdb-248742",
    },
    {
        "library_id": "2",
        "root_path": "/mnt/media/TV/The Stand (1994)",
        "state": "resolved",
        "observed_file_count": 1,
        "content_id": "series-tvdb-372051",
    },
    {
        "library_id": "3",
        "root_path": "/mnt/media/Anime/One Pace/[One Pace][1-3] Romance Dawn [1080p]",
        "state": "unmatched",
        "observed_file_count": 3,
        "content_id": "local-onepace-a",
    },
    {
        "library_id": "3",
        "root_path": "/mnt/media/Anime/One Pace/[One Pace][4-7] Orange Town [1080p]",
        "state": "unmatched",
        "observed_file_count": 2,
        "content_id": "local-onepace-b",
    },
]
SILO_SKIPPED = [
    {
        "library_id": "2",
        "root_path": "/mnt/media/TV/Orphan Show",
        "reason": "missing_folder_ids",
        "file_count": 1,
    },
    {
        "library_id": "4",
        "root_path": "/mnt/media/Sport/NBA",
        "reason": "missing_folder_ids",
        "file_count": 7,
    },  # library 4 isn't ingested: ignored
]
SILO_PROGRESS = [
    {
        "media_item_id": "episode-tvdb-176941-1-1",
        "position_seconds": 0,
        "duration_seconds": 3600,
        "completed": True,
        "updated_at": "2026-10-20T10:00:00.000Z",
    },
    {
        "media_item_id": "movie-imdb-tt0113277",
        "position_seconds": 0,
        "duration_seconds": 6000,
        "completed": True,
        "updated_at": "2026-10-19T10:00:00.000Z",
    },
    {
        "media_item_id": "episode-tvdb-248742-1-1",
        "position_seconds": 0,
        "duration_seconds": 3000,
        "completed": False,
        "updated_at": "2026-10-19T10:00:00.000Z",
    },  # neither completed nor started: skipped
]


# The API keys Silo holds (shared by every fake connection, as the real server is one).
SILO_KEYS: list[dict] = []


class SiloServer:
    """Records detail calls so tests can check they're cached between syncs."""

    def __init__(self, page_size: int = 2, rate_limit_first: bool = False):
        self.page_size = page_size
        self.detail_calls: list[str] = []
        self.rate_limit_first = rate_limit_first
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.rate_limit_first and self.calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        path = request.url.path
        assert request.headers["authorization"].startswith("Bearer ")
        if path == "/api/v2/profiles":
            return httpx.Response(200, json={"items": [{"id": "p-1", "is_primary": True}]})
        if path.startswith("/api/v2/watched/"):
            cid = path.rsplit("/", 1)[1]
            if cid in SILO_FAIL:
                return httpx.Response(500)
            auth = request.headers["authorization"].removeprefix("Bearer ")
            SILO_WRITES.append((request.method, cid, request.headers["x-profile-id"], auth))
            return httpx.Response(204)
        if path == "/api/v2/admin/users" and request.method == "POST":
            CREATED.append(("silo", "user", json.loads(request.content)))
            return httpx.Response(201, json={"id": "9"})
        if path.endswith("/password-reset") and request.method == "POST":
            CREATED.append(("silo", "reset", json.loads(request.content)))
            return httpx.Response(
                201,
                json={
                    "delivery": "link",
                    "delivery_status": "not_requested",
                    "expires_at": "2026-11-01T00:00:00Z",
                    "reset_url": "https://silo.example/reset/one-time",
                },
            )
        if path == "/api/v2/admin/api-keys" and request.method == "POST":
            uid = json.loads(request.content)["user_id"]
            body = {"id": f"k{uid}", "user_id": uid, "label": "MediaWrangler", "key": f"key-{uid}"}
            SILO_KEYS[:] = [k for k in SILO_KEYS if k["id"] != body["id"]]
            SILO_KEYS.append({k: body[k] for k in ("id", "user_id", "label")})
            return httpx.Response(201, json=body)
        if path == "/api/v2/admin/api-keys":
            return httpx.Response(200, json={"items": list(SILO_KEYS), "page": {"has_more": False}})
        if path.startswith("/api/v2/admin/api-keys/") and request.method == "DELETE":
            SILO_KEYS[:] = [k for k in SILO_KEYS if k["id"] != path.rsplit("/", 1)[1]]
            return httpx.Response(204)
        if path == "/api/v2/progress":
            # Like the real Silo: another account's profile is "not found" to any
            # key but that account's own.
            profile, token = request.headers.get("x-profile-id"), request.headers["authorization"]
            if profile == "p-1" or (profile == "p-2" and token == "Bearer key-2"):
                return httpx.Response(
                    200, json={"items": SILO_PROGRESS, "page": {"has_more": False}}
                )
            return httpx.Response(404, json={"detail": "Profile not found"})
        # Account and admin endpoints don't need a profile; catalog reads do.
        if path == "/api/v2/account/me":
            return httpx.Response(200, json={"id": "1", "username": "user1"})
        if path == "/api/v2/admin/users":
            accounts = [{"id": "1", "username": "user1"}, {"id": "2", "username": "user2"}]
            return httpx.Response(200, json={"items": accounts, "page": {"has_more": False}})
        if path.startswith("/api/v2/admin/users/") and path.endswith("/profiles"):
            uid = path.split("/")[5]
            name = {"1": "user1", "2": "user2"}[uid]
            return httpx.Response(200, json={"items": [{"id": f"p-{uid}", "name": name}]})
        assert request.headers.get("x-profile-id") == "p-1", path
        if path == "/api/v2/libraries":
            return httpx.Response(200, json={"items": SILO_LIBS})
        if path == "/api/v2/catalog":
            q = parse_qs(request.url.query.decode())
            rows = SILO_CATALOG.get((q["library_id"][0], q["type"][0]), [])
            start = int(q.get("cursor", ["0"])[0])
            chunk = rows[start : start + self.page_size]
            more = start + self.page_size < len(rows)
            page = {"next_cursor": str(start + self.page_size) if more else None, "has_more": more}
            return httpx.Response(200, json={"items": chunk, "page": page, "total": len(rows)})
        if path == "/api/v2/libraries/roots":
            q = parse_qs(request.url.query.decode())
            rows = [r for r in SILO_ROOTS if r["library_id"] == q["library_id"][0]]
            return httpx.Response(200, json={"items": rows, "page": {"has_more": False}})
        if path == "/api/v2/libraries/skipped-roots":
            return httpx.Response(200, json={"items": SILO_SKIPPED, "page": {"has_more": False}})
        if path.startswith("/api/v2/catalog/items/"):
            cid = path.rsplit("/", 1)[1]
            self.detail_calls.append(cid)
            return httpx.Response(200, content=json.dumps({"content_id": cid, **SILO_DETAILS[cid]}))
        return httpx.Response(404)


# --- arrs ---------------------------------------------------------------------

SONARR_SERIES = [
    {
        "id": 1,
        "title": "Sherlock",
        "year": 2010,
        "tvdbId": 176941,
        "tmdbId": 19885,
        "imdbId": "tt1475582",
        "path": "/data/TV/Sherlock",
        "statistics": {"episodeFileCount": 4},
    },
    {
        "id": 2,
        "title": "Person of Interest",
        "year": 2011,
        "tvdbId": 248742,
        "path": "/data/TV/Person of Interest (2011)",
        "statistics": {"episodeFileCount": 1},
    },
    {
        "id": 3,
        "title": "The Stand",
        "year": 1994,
        "tvdbId": 79866,
        "path": "/data/TV/The Stand (1994)",
        "statistics": {"episodeFileCount": 1},
    },
    {
        "id": 4,
        "title": "Gone Show",
        "year": 2001,
        "tvdbId": 1234,
        "path": "/data/TV/Gone Show",
        "statistics": {"episodeFileCount": 1},
    },
    {
        "id": 5,
        "title": "Wanted Show",
        "year": 2026,
        "tvdbId": 999,
        "path": "/data/TV/Wanted Show",
        "statistics": {"episodeFileCount": 0},
    },
]
SONARR_EPISODES = {1: [(1, 1), (1, 2), (1, 3), (0, 1)], 2: [(1, 1)], 3: [(1, 1)], 4: [(1, 1)]}


def _sonarr_files(series):
    files, episodes = [], []
    for n, (season, ep) in enumerate(SONARR_EPISODES.get(series["id"], []), start=1):
        fid = series["id"] * 100 + n
        files.append(
            {
                "id": fid,
                "seriesId": series["id"],
                "seasonNumber": season,
                "size": 1000 + n,
                "path": f"{series['path']}/{ep_name(series['path'], season, ep)}",
                "quality": {"quality": {"name": "WEBDL-1080p"}},
            }
        )
        episodes.append(
            {"id": fid + 5000, "episodeFileId": fid, "seasonNumber": season, "episodeNumber": ep}
        )
    episodes.append({"id": 1, "episodeFileId": 0, "seasonNumber": 9, "episodeNumber": 9})
    return files, episodes


def sonarr_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["x-api-key"]
    path = request.url.path
    q = parse_qs(request.url.query.decode())
    if path == "/api/v3/rootfolder":
        return httpx.Response(200, json=[{"id": 13, "path": "/data/TV"}])
    if path == "/api/v3/series":
        return httpx.Response(200, json=SONARR_SERIES)
    if path in ("/api/v3/episodefile", "/api/v3/episode"):
        if "seriesId" not in q:
            return httpx.Response(
                400, json={"message": "seriesId or episodeFileIds must be provided"}
            )
        series = next(s for s in SONARR_SERIES if s["id"] == int(q["seriesId"][0]))
        files, episodes = _sonarr_files(series)
        return httpx.Response(200, json=files if path.endswith("episodefile") else episodes)
    return httpx.Response(404)


def radarr_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v3/rootfolder":
        return httpx.Response(200, json=[{"id": 8, "path": "/movies"}])
    if path == "/api/v3/movie":
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "title": "Heat",
                    "year": 1995,
                    "tmdbId": 949,
                    "imdbId": "tt0113277",
                    "path": "/movies/Heat (1995)",
                    "hasFile": True,
                    "movieFile": {
                        "path": "/movies/Heat (1995)/Heat.mkv",
                        "size": 5,
                        "quality": {"quality": {"name": "Bluray-1080p"}},
                    },
                },
                {
                    "id": 2,
                    "title": "Wanted Film",
                    "year": 2026,
                    "tmdbId": 5,
                    "path": "/movies/Wanted Film",
                    "hasFile": False,
                },
            ],
        )
    return httpx.Response(404)


def make_share(root) -> None:
    """A media share on disk matching the fakes, plus a few disagreements."""
    tv = root / "TV"
    files = [
        *(f"Sherlock/{ep_name('/x/Sherlock', s, e)}" for s, e in [(1, 1), (1, 2), (1, 3), (0, 1)]),
        "Sherlock/Featurettes/Making Of.mkv",  # extra: left out of counts
        f"Person of Interest (2011)/{ep_name('/x/Person of Interest (2011)', 1, 1)}",
        f"The Stand (1994)/{ep_name('/x/The Stand (1994)', 1, 1)}",
        "Orphan Show/Season 01/Orphan Show - S01E01.mkv",  # no arr or Jellyfin knows it
        "Dup Show/Season 01/Dup Show - S01E01 - WEBDL.mkv",
        "Dup Show/Season 01/Dup Show - S01E01 - Bluray.mkv",
    ]
    for rel in files:
        p = tv / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 10)
    (tv / "Sherlock" / "Season 01" / "cover.jpg").write_bytes(b"not a video")
    (root / "Anime" / "One Pace").mkdir(parents=True)
    (root / "Movies" / "Heat (1995)").mkdir(parents=True)
    (root / "Movies" / "Heat (1995)" / "Heat.mkv").write_bytes(b"x")
    (root / "Music" / "Album").mkdir(parents=True)
    (root / "Music" / "Album" / "song.mp4").write_bytes(b"x")  # not in a group: never read
