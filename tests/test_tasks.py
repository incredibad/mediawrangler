"""Asking apps to scan or refresh from the Physical page (0.11)."""

import httpx
from sqlalchemy import select

from mediawrangler import tasks, writes
from mediawrangler.connectors.base import Connector, ConnectorConfig, Task
from mediawrangler.connectors.jellyfin import JellyfinConnector
from mediawrangler.connectors.paths import PathMap
from mediawrangler.connectors.silo import SiloConnector
from mediawrangler.models import AuditLog
from mediawrangler.physical import build_physical_view

from .conftest import post
from .test_physical import _src, _tv, share, synced  # noqa: F401


def _folder(db, name):
    view = build_physical_view(db, _tv(db))
    return view, next(r.folder for r in view.rows if r.name == name)


def test_the_calls_each_app_gets():
    cfg = ConnectorConfig("http://x", "k")
    jf = JellyfinConnector(cfg, httpx.Client())
    assert jf.task_call(Task("scan", path="/data/TV/Orphan Show")) == (
        "POST",
        "/Library/Media/Updated",
        {"Updates": [{"Path": "/data/TV/Orphan Show", "UpdateType": "Created"}]},
    )
    assert jf.task_call(Task("refresh", item_id="abc"))[1].startswith("/Items/abc/Refresh?")
    silo = SiloConnector(cfg, httpx.Client())
    assert silo.task_call(Task("scan", path="/mnt/media/TV/X", library_id="2")) == (
        "POST",
        "/api/v2/scan",
        {"library_id": "2", "path": "/mnt/media/TV/X"},
    )
    assert silo.task_call(Task("refresh", item_id="2/series-tvdb-1")) == (
        "POST",
        "/api/v2/admin/items/series-tvdb-1/refresh-metadata",
        {"mode": "complete"},
    )


def test_a_canonical_path_is_turned_back_into_the_apps_own():
    pm = PathMap.from_pairs([["/data/TV", "/media/TV"], ["/mnt/media", "/media"]])
    assert pm.reverse("/media/TV/Sherlock") == "/data/TV/Sherlock"
    assert pm.reverse("/media/TV") == "/data/TV"
    assert pm.reverse("/elsewhere/x") is None


def test_plans_for_each_kind_of_app(synced):  # noqa: F811
    db = synced
    group = _tv(db)
    view, orphan = _folder(db, "Orphan Show")
    jf, silo, sonarr = (_src(db, n) for n in ("Jellyfin", "Silo", "Sonarr"))
    p = tasks.plan(db, group, view.roots, jf, orphan, "scan")
    assert p.task.kind == "scan" and p.task.path.startswith("/data/TV/")
    p = tasks.plan(db, group, view.roots, silo, orphan, "scan")
    assert p.task.path.startswith("/mnt/media/TV/") and p.task.library_id
    _view, sherlock = _folder(db, "Sherlock")
    p = tasks.plan(db, group, view.roots, sonarr, sherlock, "scan")
    assert p.item is not None and p.task.item_id.startswith("series-")
    p = tasks.plan(db, group, view.roots, silo, sherlock, "refresh")
    assert p.task.kind == "refresh" and p.item.title == "Sherlock"
    try:
        tasks.plan(db, group, view.roots, sonarr, sherlock, "refresh")
    except tasks.FixRefused as exc:
        assert "cannot" in str(exc)
    else:
        raise AssertionError("an arr cannot refresh details")


def test_a_sportarr_league_is_found_by_the_files_it_holds(synced):  # noqa: F811
    from mediawrangler.models import SourceItem

    db = synced
    group = _tv(db)
    view, sherlock = _folder(db, "Sherlock")
    arr = _src(db, "Sonarr")
    item = tasks.find_item(db, group, arr, sherlock, view.roots)
    arr.type = "sportarr"  # Sportarr's leagues carry no folder of their own
    for it in db.query(SourceItem).filter(SourceItem.source_id == arr.id):
        it.path = None
    db.flush()
    assert tasks.find_item(db, group, arr, sherlock, view.roots).id == item.id
    _view, orphan = _folder(db, "Orphan Show")
    assert tasks.find_item(db, group, arr, orphan, view.roots) is None
    db.rollback()


def test_preview_apply_and_audit(synced, app, logged_in, monkeypatch):  # noqa: F811
    import mediawrangler.sync as sync_mod

    monkeypatch.setattr(sync_mod, "SETTLE_SCALE", 0)
    db = synced
    group = _tv(db)
    _view, orphan = _folder(db, "Orphan Show")
    jf = _src(db, "Jellyfin")
    sent = []
    monkeypatch.setattr(Connector, "run_task", lambda self, task: sent.append((self.type, task)))
    q = f"group={group.id}&source={jf.id}&kind=scan&folder="
    page = logged_in.get("/fix/folder-task?" + q + httpx.QueryParams({"f": orphan})["f"])
    assert page.status_code == 200 and "/Library/Media/Updated" in page.text
    assert "Ask Jellyfin to look for new files" in page.text
    assert "Writes to your apps are off" in page.text

    form = {"group": group.id, "source": jf.id, "kind": "scan", "folder": orphan}
    refused = post(logged_in, "/fix/folder-task", form)
    assert "switched off" in refused.text and not sent

    writes.set_flag(db, writes.ENABLED_KEY, True)
    done = post(logged_in, "/fix/folder-task", form)
    assert "data-close-now" in done.text
    app.state.syncs.wait_idle()
    assert sent[0][0] == "jellyfin"
    (entry,) = db.execute(select(AuditLog).where(AuditLog.op == "scan")).scalars()
    assert entry.result == "applied" and entry.source_name == "Jellyfin"
    assert "Scans, refreshes and folder fixes" in logged_in.get("/activity?tab=audit").text


def test_physical_cells_get_a_fix_menu(synced, logged_in):  # noqa: F811
    db = synced
    page = logged_in.get(f"/physical?group={_tv(db).id}&filter=all").text
    assert "Ask Jellyfin to look for new files" in page
    assert "List the files in this folder" in page
    assert "Re-download Silo&#39;s details for this show" in page


def test_the_folder_page_has_a_silo_column_and_no_separate_silo_box(synced, logged_in):  # noqa: F811
    db = synced
    group = _tv(db)
    _view, sherlock = _folder(db, "Sherlock")
    resp = logged_in.get("/physical/folder", params={"group": group.id, "folder": sherlock})
    assert resp.status_code == 200 and ">Silo</a></th>" in resp.text
    assert "view of this folder" not in resp.text


def test_silo_series_files_are_read_with_their_paths():
    from .fakes import SiloServer

    base = SiloServer()
    asked = []

    def handler(request):
        if request.url.path.endswith("/files"):
            asked.append(request.url.path)
            rows = [
                {
                    "file_path": "/mnt/media/TV/Sherlock/Season 01/Sherlock - S01E01.mkv",
                    "library_id": "2",
                    "season_number": 1,
                    "episode_number": 1,
                }
            ]
            return httpx.Response(200, json={"items": rows, "page": {"has_more": False}})
        return base(request)

    silo = SiloConnector(
        ConnectorConfig("http://silo", "k"), httpx.Client(transport=httpx.MockTransport(handler))
    )
    snap = silo.fetch_full()
    series = [i for i in snap.items if i.kind == "series"]
    assert len(asked) == len(series) > 0
    first = next(f for f in snap.files if "Sherlock" in f.path)
    assert (first.season, first.episode, first.library_external_id) == (1, 1, "2")
    assert first.item_external_id in {i.external_id for i in series}


def test_arr_tasks_report_when_they_finish():
    from mediawrangler.connectors.arr import SonarrConnector
    from mediawrangler.connectors.base import ConnectorError

    polls = []

    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json={"id": 77, "status": "queued"})
        polls.append(1)
        status = {1: "started", 2: "completed"}.get(len(polls), "failed")
        return httpx.Response(200, json={"status": status, "message": "boom"})

    sonarr = SonarrConnector(
        ConnectorConfig("http://s", "k"), httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert sonarr.run_task(Task("scan", item_id="series-5")) == "77"
    assert sonarr.task_done("77") is False and sonarr.task_done("77") is True
    try:
        sonarr.task_done("77")
    except ConnectorError as exc:
        assert "failed" in str(exc)
    else:
        raise AssertionError("a failed command must raise")


def test_sportarr_scan_triggers_its_disk_scan():
    from mediawrangler.connectors.arr import SportarrConnector

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"queued": True})

    sportarr = SportarrConnector(
        ConnectorConfig("http://sp", "k"), httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert sportarr.run_task(Task("scan")) is None
    assert seen == [("POST", "/api/task/scheduled/disk-scan/trigger")]


def test_sportarr_reads_one_league():
    from mediawrangler.connectors.arr import SportarrConnector

    def handler(request):
        path = request.url.path
        if path == "/api/rootfolder":
            return httpx.Response(200, json=[{"id": 1, "path": "/data"}])
        if path == "/api/leagues/8":
            return httpx.Response(200, json={"id": 8, "name": "NBA"})
        if path == "/api/leagues/8/files":
            f = "/data/NBA/Season 2026/NBA - S2026E111 - Celtics vs Bulls.mkv"
            return httpx.Response(200, json={"files": [{"filePath": f, "size": 5}]})
        return httpx.Response(404)

    sportarr = SportarrConnector(
        ConnectorConfig("http://sp", "k"), httpx.Client(transport=httpx.MockTransport(handler))
    )
    snap = sportarr.fetch_title("league-8", "series", None)
    assert [(i.external_id, i.title, i.library_external_id) for i in snap.items] == [
        ("league-8", "NBA", "1")
    ]
    assert [(f.season, f.episode, f.item_external_id) for f in snap.files] == [
        (2026, 111, "league-8")
    ]


def test_one_title_is_read_without_listing_the_library():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        q = request.url.params
        if q.get("Ids") == "S1":
            show = {"Id": "S1", "Type": "Series", "Name": "The Stand", "ProductionYear": 1994}
            return httpx.Response(200, json={"Items": [{**show, "ProviderIds": {"Tvdb": "79866"}}]})
        ep = {
            "Id": "E1",
            "Type": "Episode",
            "Name": "The Plague",
            "SeriesId": "S1",
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
            "Path": "/data/TV/The Stand/S01E01.mkv",
        }
        return httpx.Response(200, json={"Items": [ep], "TotalRecordCount": 1})

    jf = JellyfinConnector(
        ConnectorConfig("http://jf", "k"), httpx.Client(transport=httpx.MockTransport(handler))
    )
    snap = jf.fetch_title("S1", "series", "lib1")
    assert [i.kind for i in snap.items] == ["series", "episode"] and snap.items[0].tvdb == "79866"
    assert snap.files[0].item_external_id == "S1" and (
        snap.files[0].season,
        snap.files[0].episode,
    ) == (1, 1)
    assert all("Ids" in q or q.get("ParentId") == "S1" for q in seen)  # never the whole library


def test_silo_title_read_and_lookup():
    def handler(request):
        path = request.url.path
        if path == "/api/v2/profiles":
            return httpx.Response(200, json={"items": [{"id": "p-1", "is_primary": True}]})
        if path == "/api/v2/catalog/items/series-tvdb-79866":
            detail = {
                "type": "series",
                "title": "The Stand",
                "year": 1994,
                "tvdb_id": "79866",
                "folder_paths": ["/mnt/media/TV/The Stand (1994)"],
            }
            return httpx.Response(200, json=detail)
        if path.endswith("/seasons"):
            return httpx.Response(200, json={"items": [{"season_number": 1}]})
        if path.endswith("/seasons/1/episodes"):
            ep = {
                "content_id": "episode-tvdb-79866-1-1",
                "season_number": 1,
                "episode_number": 1,
                "title": "The Plague",
            }
            return httpx.Response(200, json={"items": [ep]})
        if path.endswith("/files"):
            row = {
                "file_path": "/mnt/media/TV/The Stand (1994)/S01E01.mkv",
                "library_id": "2",
                "season_number": 1,
                "episode_number": 1,
            }
            return httpx.Response(200, json={"items": [row], "page": {"has_more": False}})
        if path == "/api/v2/catalog":
            return httpx.Response(
                200,
                json={"items": [{"content_id": "series-tvdb-79866"}], "page": {"has_more": False}},
            )
        return httpx.Response(404)

    silo = SiloConnector(
        ConnectorConfig("http://silo", "k"), httpx.Client(transport=httpx.MockTransport(handler))
    )
    snap = silo.fetch_title("2/series-tvdb-79866", "series", "2")
    assert [i.kind for i in snap.items] == ["series", "episode"]
    assert snap.items[1].parent_external_id == "2/series-tvdb-79866" and snap.items[1].season == 1
    assert snap.files[0].path.endswith("S01E01.mkv") and snap.root_id == "2/series-tvdb-79866"
    found = silo.find_title("The Stand", "series", "2", "/mnt/media/TV/The Stand (1994)")
    assert found == "2/series-tvdb-79866"
    assert silo.find_title("The Stand", "series", "2", "/mnt/media/TV/Other") is None


def test_silo_find_title_sends_a_profile_so_a_movie_can_be_found_first():
    """The catalog answers 422 without X-Profile-Id; a fresh connector must ask for one."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/profiles":
            return httpx.Response(200, json={"items": [{"id": "7", "is_primary": True}]})
        if not request.headers.get("X-Profile-Id"):
            return httpx.Response(422)
        if path == "/api/v2/catalog":
            return httpx.Response(
                200, json={"items": [{"content_id": "movie-1"}], "page": {"has_more": False}}
            )
        if path == "/api/v2/catalog/items/movie-1":
            return httpx.Response(200, json={"folder_paths": ["/mnt/media/Movies/Heat (1995)"]})
        return httpx.Response(404)

    silo = SiloConnector(
        ConnectorConfig("http://silo", "k"), httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert silo.find_title("Heat", "movie", "1", "/mnt/media/Movies/Heat (1995)") == "1/movie-1"


def _history_world(db):
    from mediawrangler.models import (
        AppUser,
        CanonicalItem,
        LocalPlay,
        LocalState,
        Source,
        SourceItem,
    )

    src = Source(type="jellyfin", name="Jellyfin", base_url="http://jf")
    person = AppUser(name="Alex")
    old = CanonicalItem(key="old", kind="series", title="The Stand", year=2020)
    new = CanonicalItem(key="new", kind="series", title="The Stand", year=1994)
    db.add_all([src, person, old, new])
    db.flush()
    db.add(
        SourceItem(
            source_id=src.id,
            external_id="S1",
            kind="series",
            title="The Stand",
            canonical_id=new.id,
        )
    )
    for n in (1, 2):
        db.add(
            SourceItem(
                source_id=src.id,
                external_id=f"E{n}",
                kind="episode",
                title="x",
                parent_external_id="S1",
                season=1,
                episode=n,
            )
        )
    for n in (1, 2, 5):  # the old show had episode 5; the new one has two episodes
        db.add(
            LocalState(
                app_user_id=person.id,
                kind="ep",
                canonical_id=old.id,
                season=1,
                episode=n,
                played=True,
            )
        )
        db.add(
            LocalPlay(
                app_user_id=person.id,
                kind="ep",
                canonical_id=old.id,
                season=1,
                episode=n,
                day="2026-02-05",
                sources="Jellyfin",
            )
        )
    db.commit()
    return src, person, old, new


def test_history_is_cut_and_pasted_to_the_new_show(app):
    from mediawrangler.local import move_history
    from mediawrangler.models import LocalPlay, LocalState

    with app.state.db.sessionmaker() as db:
        src, person, old, new = _history_world(db)
        assert move_history(db, src, "S1", old.id) == 2
        states = db.execute(select(LocalState.canonical_id, LocalState.episode)).all()
        assert sorted(states) == [(old.id, 5), (new.id, 1), (new.id, 2)]  # episode 5 has no home
        plays = db.execute(select(LocalPlay.canonical_id, LocalPlay.episode)).all()
        assert sorted(plays) == [(old.id, 5), (new.id, 1), (new.id, 2)]
        assert move_history(db, src, "S1", old.id) == 0  # nothing left to move


def test_history_stays_while_another_app_still_has_the_old_show(app):
    from mediawrangler.local import move_history
    from mediawrangler.models import LocalState, Source, SourceItem

    with app.state.db.sessionmaker() as db:
        src, person, old, new = _history_world(db)
        other = Source(type="silo", name="Silo", base_url="http://silo")
        db.add(other)
        db.flush()
        db.add(
            SourceItem(
                source_id=other.id,
                external_id="x/s",
                kind="series",
                title="The Stand",
                canonical_id=old.id,
            )
        )
        db.commit()
        assert move_history(db, src, "S1", old.id) == 0
        assert (
            db.execute(select(LocalState).where(LocalState.canonical_id == old.id)).scalars().all()
        )


def test_a_title_read_updates_only_that_title(synced):  # noqa: F811
    from mediawrangler.connectors.base import FileRecord, ItemRecord, TitleSnapshot
    from mediawrangler.ingest import apply_title
    from mediawrangler.models import MediaFile, SourceItem

    db = synced
    jf = _src(db, "Jellyfin")
    show = db.execute(
        select(SourceItem).where(
            SourceItem.source_id == jf.id,
            SourceItem.title == "Sherlock",
            SourceItem.kind == "series",
        )
    ).scalar_one()
    before = db.execute(select(SourceItem).where(SourceItem.source_id == jf.id)).scalars().all()
    other_count = len(
        [
            i
            for i in before
            if i.external_id != show.external_id
            and i.parent_external_id != show.external_id
            and i.removed_at is None
        ]
    )
    snap = TitleSnapshot(
        show.external_id,
        [
            ItemRecord(
                external_id=show.external_id, kind="series", title="Sherlock", year=2010, tvdb="999"
            ),
            ItemRecord(
                external_id="new-ep",
                kind="episode",
                title="Pilot",
                parent_external_id=show.external_id,
                season=1,
                episode=1,
            ),
        ],
        [
            FileRecord(
                path="/data/TV/Sherlock/Season 01/new.mkv",
                item_external_id=show.external_id,
                season=1,
                episode=1,
            )
        ],
    )
    apply_title(db, jf, snap)
    db.refresh(show)
    assert show.tvdb == "999"
    live = (
        db.execute(
            select(SourceItem).where(SourceItem.source_id == jf.id, SourceItem.removed_at.is_(None))
        )
        .scalars()
        .all()
    )
    assert (
        len(
            [
                i
                for i in live
                if i.external_id != show.external_id and i.parent_external_id != show.external_id
            ]
        )
        == other_count
    )
    assert {i.external_id for i in live if i.parent_external_id == show.external_id} == {"new-ep"}
    gone = (
        db.execute(
            select(MediaFile).where(
                MediaFile.source_id == jf.id,
                MediaFile.item_external_id == show.external_id,
                MediaFile.removed_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    assert [f.path for f in gone] == ["/data/TV/Sherlock/Season 01/new.mkv"]


def test_the_refresh_steps_wait_read_and_reload(synced, logged_in, app, monkeypatch):  # noqa: F811
    db = synced
    jf = _src(db, "Jellyfin")
    started = []
    monkeypatch.setattr(
        app.state.syncs,
        "refresh_title",
        lambda sid, ext, old=None, replaces=None, locate=None: started.append((ext, old)) or 3,
    )
    states = iter(["running", "done"])
    monkeypatch.setattr(
        app.state.syncs, "title_job", lambda job: {"state": next(states), "error": ""}
    )
    first = logged_in.get(f"/fix/refresh?source={jf.id}&stage=read&reread=abc&old=9")
    assert "Reading just this one title" in first.text and "stage=job" in first.text
    assert started == [("abc", 9)]
    done = logged_in.get(f"/fix/refresh?source={jf.id}&stage=job&job=3")
    assert "data-reload-now" in done.text


def test_arr_lookup_and_add_calls():
    from mediawrangler.connectors.arr import RadarrConnector, SonarrConnector

    sent = []

    def handler(request):
        path = request.url.path
        if request.method == "POST":
            sent.append((path, __import__("json").loads(request.content)))
            return httpx.Response(201, json={"id": 41})
        if path.endswith("/lookup"):
            row = {"title": "Orphan Show", "year": 2019, "tvdbId": 5, "tmdbId": 0, "imdbId": "tt1"}
            return httpx.Response(200, json=[row])
        if path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[{"id": 4}])
        if path == "/api/v3/languageprofile":
            return httpx.Response(404)
        if path == "/api/v3/rootfolder":
            return httpx.Response(200, json=[{"path": "/data/TV"}])
        return httpx.Response(404)

    def make(cls):
        return cls(
            ConnectorConfig("http://x", "k"), httpx.Client(transport=httpx.MockTransport(handler))
        )

    sonarr = make(SonarrConnector)
    (cand,) = sonarr.lookup("Orphan Show")
    assert cand.ids == {"tvdb": "5", "imdb": "tt1"} and cand.year == 2019
    method, path, body = sonarr.add_call(cand, "/data/TV/Orphan Show")
    assert (method, path) == ("POST", "/api/v3/series")
    assert body["rootFolderPath"] == "/data/TV" and body["path"] == "/data/TV/Orphan Show"
    assert body["monitored"] is False and body["addOptions"]["searchForMissingEpisodes"] is False
    assert body["qualityProfileId"] == 4 and "languageProfileId" not in body
    assert sonarr.add_title(cand, "/data/TV/Orphan Show") == "series-41"
    assert sent[0][0] == "/api/v3/series"
    radarr = make(RadarrConnector)
    body = radarr.add_call(cand, "/data/TV/Orphan Show")[2]
    assert body["addOptions"] == {"monitor": "movieOnly", "searchForMovie": False}


def test_silo_override_body():
    silo = SiloConnector(ConnectorConfig("http://silo", "k"), httpx.Client())
    from mediawrangler.connectors.base import MatchCandidate

    cand = MatchCandidate("Orphan Show", 2019, {"tvdb": "5"})
    method, path, body = silo.override_call("2", "/mnt/media/TV/Orphan Show", cand, "series")
    assert (method, path) == ("PUT", "/api/v2/libraries/roots/override")
    assert (
        body["library_id"] == "2" and body["forced_tvdb_id"] == "5" and body["forced_year"] == 2019
    )
    assert body["forced_type"] == "series" and body["root_path"] == "/mnt/media/TV/Orphan Show"


def test_identify_dialog_preview_apply_and_audit(synced, logged_in, app, monkeypatch):  # noqa: F811
    import mediawrangler.sync as sync_mod

    monkeypatch.setattr(sync_mod, "SETTLE_SCALE", 0)
    from mediawrangler.connectors.base import Connector, MatchCandidate
    from mediawrangler.connectors.silo import SiloConnector as Silo

    db = synced
    group = _tv(db)
    view, orphan = _folder(db, "Orphan Show")
    silo = _src(db, "Silo")
    sent = []
    monkeypatch.setattr(Silo, "set_override", lambda self, *a: sent.append(a))
    monkeypatch.setattr(Connector, "run_task", lambda self, task: None)
    monkeypatch.setattr(
        "mediawrangler.connectors.arr.SonarrConnector.lookup",
        lambda self, term: [MatchCandidate("Orphan Show", 2019, {"tvdb": "5"}, payload={"x": 1})],
    )
    params = {"group": group.id, "source": silo.id, "folder": orphan, "action": "override"}
    page = logged_in.get("/fix/identify", params=params)
    assert "Tell Silo what this folder is" in page.text and "Orphan Show" in page.text
    cand = page.text.split('name="candidate" value=\'')[1].split("'>")[0].replace("&#34;", '"')
    preview = post(logged_in, "/fix/identify/preview", {**params, "candidate": cand})
    assert "PUT</strong>" in preview.text and "forced_tvdb_id" in preview.text
    assert "Writes to your apps are off" in preview.text
    refused = post(logged_in, "/fix/identify", {**params, "candidate": cand})
    assert "switched off" in refused.text and not sent
    writes.set_flag(db, writes.ENABLED_KEY, True)
    done = post(logged_in, "/fix/identify", {**params, "candidate": cand})
    assert "data-close-now" in done.text
    app.state.syncs.wait_idle()
    assert sent
    (entry,) = db.execute(select(AuditLog).where(AuditLog.op == "override")).scalars()
    assert entry.result == "applied" and entry.source_name == "Silo"
    manual = post(logged_in, "/fix/identify/preview", {**params, "m_title": "X", "m_tmdb": "9"})
    assert "forced_tmdb_id" in manual.text
    assert (
        "Fill in a title"
        in post(logged_in, "/fix/identify/preview", {**params, "m_title": "X"}).text
    )


def test_physical_menu_offers_add_and_override(synced, logged_in):  # noqa: F811
    db = synced
    page = logged_in.get(f"/physical?group={_tv(db).id}&filter=all").text
    assert "Add to Sonarr" in page and "Tell Silo what this folder is" in page


def test_orphans_are_listed_not_deleted(synced, logged_in):  # noqa: F811
    from mediawrangler.models import MediaFile
    from mediawrangler.physical import folder_detail, orphans

    db = synced
    group = _tv(db)
    view, folder = _folder(db, "Sherlock")
    sonarr = _src(db, "Sonarr")
    disk = _src(db, "Media share")
    gone = (
        db.execute(
            select(MediaFile).where(
                MediaFile.source_id == sonarr.id,
                MediaFile.canonical_path.like(folder + "/%S01E01%"),
            )
        )
        .scalars()
        .first()
    )
    gone.removed_at = __import__("datetime").datetime.now()
    db.commit()
    view = build_physical_view(db, group)
    row = next(r for r in view.rows if r.folder == folder)
    lines, _roots = folder_detail(db, view, folder)
    found = orphans(view, row, lines)
    assert sonarr.id in found and any("S01E01" in line.path for line in found[sonarr.id])
    page = logged_in.get("/physical/folder", params={"group": group.id, "folder": folder}).text
    assert "no record of" in page and "rm -- '" in page and disk.id


def test_a_title_an_arr_tracks_under_other_ids_is_flagged_as_a_mismatch(synced, logged_in, app):  # noqa: F811
    from mediawrangler.models import CanonicalItem, SourceItem
    from mediawrangler.presence import build_group_view

    db = synced
    group = _tv(db)
    sonarr = _src(db, "Sonarr")
    item = db.execute(
        select(SourceItem).where(
            SourceItem.source_id == sonarr.id,
            SourceItem.title.like("Sherlock%"),
            SourceItem.kind == "series",
        )
    ).scalar_one()
    before = build_group_view(db, group)
    row = next(r for r in before.rows if r.canonical.id == item.canonical_id)
    assert row.cells[sonarr.id].state == "tracked" and "mismatch" not in row.flags
    # The arr's Sherlock is paired with a different show (other IDs): it no longer matches.
    other = CanonicalItem(key="x-sherlock", kind="series", title="Sherlock", year=2010)
    db.add(other)
    db.flush()
    # Like Battlestar Galactica: the arr's year is a later one, its title carries the right one.
    item.canonical_id, item.tvdb, item.tmdb = other.id, "999", "888"
    item.title, item.year = "Sherlock (2010)", 2013
    db.commit()
    app.state.syncs.bump()  # cached pages are rebuilt, as after any sync or fix
    view = build_group_view(db, group)
    row = next(
        r for r in view.rows if r.canonical.title == "Sherlock" and r.canonical.id != other.id
    )
    twin = next(r for r in view.rows if r.canonical.id == other.id)
    assert twin.cells[sonarr.id].state == "tracked" and "not_in_master" in twin.flags
    assert (row, twin) in view.possible_mismatches  # tracked in Sonarr, not found in the servers
    assert row.cells[sonarr.id].state == "untracked"
    assert [i.id for i in row.cells[sonarr.id].look_alike] == [item.id]
    assert {"mismatch", f"arr_mismatch:{sonarr.id}"} <= row.flags
    page = logged_in.get(f"/presence?group={group.id}&filter=issues").text
    assert "Sonarr has “Sherlock (2010)” with other IDs (tvdb 999, tmdb 888" in page
    assert "Change which show Jellyfin thinks this is" in page


def _second_title_in_sonarr(db, status):
    """Sonarr also tracks a different show with the same name, with the given file status."""
    from mediawrangler.models import CanonicalItem, SourceItem

    sonarr = _src(db, "Sonarr")
    item = db.execute(
        select(SourceItem).where(
            SourceItem.source_id == sonarr.id,
            SourceItem.title.like("Sherlock%"),
            SourceItem.kind == "series",
        )
    ).scalar_one()
    other = CanonicalItem(key="x-sherlock-2", kind="series", title="Sherlock", year=1954)
    db.add(other)
    db.flush()
    db.add(
        SourceItem(
            source_id=sonarr.id,
            external_id="sherlock-1954",
            library_id=item.library_id,
            kind="series",
            title="Sherlock",
            year=1954,
            canonical_id=other.id,
            source_status=status,
        )
    )
    db.commit()
    return other


def test_a_second_title_sonarr_tracks_with_no_files_is_not_a_mismatch(synced, app):  # noqa: F811
    from mediawrangler.presence import build_group_view

    db = synced
    other = _second_title_in_sonarr(db, "no_files")
    app.state.syncs.bump()
    view = build_group_view(db, _tv(db))
    # Sonarr tracks the version the servers have too, so the extra one is just a bookmark.
    assert not any(r.canonical.id == other.id for r in view.rows)


def test_a_second_title_sonarr_has_files_for_is_still_flagged(synced, app):  # noqa: F811
    from mediawrangler.presence import build_group_view

    db = synced
    other = _second_title_in_sonarr(db, "has_files")
    app.state.syncs.bump()
    view = build_group_view(db, _tv(db))
    row = next(r for r in view.rows if r.canonical.id == other.id)
    assert {"mismatch", "not_in_master"} <= row.flags


def test_silo_files_attached_to_no_episode_are_not_silo_files():
    def handler(request):
        path = request.url.path
        if path == "/api/v2/profiles":
            return httpx.Response(200, json={"items": [{"id": "p-1", "is_primary": True}]})
        if path == "/api/v2/catalog/items/series-tvdb-79866":
            detail = {"type": "series", "title": "The Stand", "year": 1994, "tvdb_id": "79866"}
            return httpx.Response(200, json=detail)
        if path.endswith("/seasons"):
            return httpx.Response(200, json={"items": [{"season_number": 1}]})
        if path.endswith("/seasons/1/episodes"):
            ep = {
                "content_id": "episode-tvdb-79866-1-1",
                "season_number": 1,
                "episode_number": 1,
                "title": "The Plague",
                "files": [{"file_id": "10"}],
            }
            return httpx.Response(200, json={"items": [ep]})
        if path.endswith("/files"):
            rows = [
                {
                    "id": "10",
                    "file_path": "/mnt/x/S01E01.mkv",
                    "library_id": "2",
                    "season_number": 1,
                    "episode_number": 1,
                },
                {
                    "id": "99",
                    "file_path": "/mnt/x/S01E09 -.mkv",
                    "library_id": "2",
                    "season_number": 1,
                    "episode_number": 9,
                },
            ]
            return httpx.Response(200, json={"items": rows, "page": {"has_more": False}})
        return httpx.Response(404)

    silo = SiloConnector(
        ConnectorConfig("http://silo", "k"), httpx.Client(transport=httpx.MockTransport(handler))
    )
    snap = silo.fetch_title("2/series-tvdb-79866", "series", "2")
    # Only the file attached to an episode is a Silo file; the admin list gave its path.
    assert [f.path for f in snap.files] == ["/mnt/x/S01E01.mkv"]


def test_a_silo_series_never_reuses_its_old_file_paths():
    from mediawrangler.connectors.base import ItemRecord

    from .fakes import SiloServer

    base = SiloServer()

    def handler(request):
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"items": [], "page": {"has_more": False}})
        return base(request)

    silo = SiloConnector(
        ConnectorConfig("http://silo", "k"), httpx.Client(transport=httpx.MockTransport(handler))
    )
    first = silo.fetch_full()
    show = next(i for i in first.items if i.kind == "series")
    previous = {
        show.external_id: ItemRecord(
            external_id=show.external_id,
            kind="series",
            title=show.title,
            details_fetched=True,
            file_paths=["/mnt/media/TV/Gone/S01E01.mkv"],  # a file Silo no longer has
        )
    }
    again = silo.fetch_full(previous=previous)
    assert not any(f.path == "/mnt/media/TV/Gone/S01E01.mkv" for f in again.files)


def _silo_film_world(versions):
    """Silo with a film in both the Movies and the Movies 4K library, listing both copies."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/profiles":
            return httpx.Response(200, json={"items": [{"id": "7", "is_primary": True}]})
        if path == "/api/v2/libraries":
            libs = [
                {"id": "1", "name": "Movies", "type": "movies", "paths": ["/mnt/media/Movies"]},
                {
                    "id": "5",
                    "name": "Movies 4K",
                    "type": "movies",
                    "paths": ["/mnt/media/Movies4K"],
                },
            ]
            return httpx.Response(200, json={"items": libs})
        if path == "/api/v2/catalog":
            card = {"content_id": "movie-imdb-tt1", "type": "movie", "title": "Heat"}
            return httpx.Response(200, json={"items": [card], "page": {"has_more": False}})
        if path == "/api/v2/catalog/items/movie-imdb-tt1":
            detail = {
                "type": "movie",
                "title": "Heat",
                "versions": [{"file_path": p} for p in versions],
            }
            return httpx.Response(200, json=detail)
        if path.startswith("/api/v2/libraries/") and path.endswith("roots"):
            return httpx.Response(200, json={"items": [], "page": {"has_more": False}})
        return httpx.Response(404)

    return SiloConnector(
        ConnectorConfig("http://silo", "k"), httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_a_silo_film_title_read_files_each_copy_under_the_library_holding_it():
    both = ["/mnt/media/Movies/Heat/h.1080p.mkv", "/mnt/media/Movies4K/Heat/h.2160p.mkv"]
    snap = _silo_film_world(both).fetch_title("5/movie-imdb-tt1", "movie", "5")
    assert {f.path: f.library_external_id for f in snap.files} == {
        "/mnt/media/Movies/Heat/h.1080p.mkv": "1",
        "/mnt/media/Movies4K/Heat/h.2160p.mkv": "5",
    }


def test_a_silo_film_never_reuses_its_old_file_paths():
    from mediawrangler.connectors.base import ItemRecord

    silo = _silo_film_world(["/mnt/media/Movies4K/Heat/new name.mkv"])
    previous = {
        "5/movie-imdb-tt1": ItemRecord(
            external_id="5/movie-imdb-tt1",
            kind="movie",
            title="Heat",
            details_fetched=True,
            file_paths=["/mnt/media/Movies4K/Heat/old name.mkv"],  # renamed since
        )
    }
    snap = silo.fetch_full(previous=previous)
    assert [f.path for f in snap.files if f.item_external_id == "5/movie-imdb-tt1"] == [
        "/mnt/media/Movies4K/Heat/new name.mkv"
    ]


def test_silo_is_asked_to_scan_the_library_when_the_folder_is_gone_from_the_share(
    synced,  # noqa: F811
    app,
):
    """Silo answers 400 to a scan of a path that no longer exists; the library is scanned."""
    from mediawrangler import tasks
    from mediawrangler.physical import build_physical_view

    db = synced
    group = _tv(db)
    silo = _src(db, "Silo")
    view = build_physical_view(db, group)
    row = next(r for r in view.rows if r.cells[silo.id].present)
    here = tasks.plan(db, group, view.roots, silo, row.folder, "scan")
    assert here.task.path  # the folder is there: a one-folder scan
    gone = tasks.plan(db, group, view.roots, silo, row.folder + " (deleted)", "scan")
    assert gone.task.path is None and gone.task.library_id
    assert "whole" in gone.title and "no longer on the share" in gone.explanation
    method, path, body = SiloConnector(ConnectorConfig("http://silo", "k")).task_call(gone.task)
    assert (method, path) == ("POST", "/api/v2/scan") and "path" not in body
