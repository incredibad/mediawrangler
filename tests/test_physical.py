"""Files, disk scan, derived path maps and the physical-media view."""

import httpx
import pytest
from sqlalchemy import select

from mediawrangler.connectors.paths import is_extra, is_video
from mediawrangler.ingest import recanonicalise
from mediawrangler.models import (
    Library,
    LibraryGroup,
    LibraryGroupMember,
    MediaFile,
    Source,
    SourceItem,
)
from mediawrangler.physical import build_physical_view, filter_rows, folder_detail
from mediawrangler.presence import build_group_view
from mediawrangler.sync import ensure_disk_source

from .conftest import filter_options, post, summary_col, table_rows
from .fakes import SiloServer, jellyfin_handler, make_share, radarr_handler, sonarr_handler

HANDLERS = {
    "jellyfin": lambda: jellyfin_handler(),
    "silo": lambda: SiloServer(),
    "sonarr": lambda: sonarr_handler,
    "radarr": lambda: radarr_handler,
}


@pytest.fixture
def share(tmp_path):
    root = tmp_path / "share"
    make_share(root)
    return root


@pytest.fixture
def synced(app, share, monkeypatch):
    """Every fake source plus the disk share, synced in a realistic order."""
    from mediawrangler.connectors.base import Connector

    real_init = Connector.__init__

    def fake_init(self, config, client=None):
        handler = HANDLERS.get(self.type)
        client = httpx.Client(transport=httpx.MockTransport(handler())) if handler else client
        real_init(self, config, client)

    monkeypatch.setattr(Connector, "__init__", fake_init)
    box = app.state.box
    with app.state.db.sessionmaker() as db:
        for typ, name, url in [
            ("jellyfin", "Jellyfin", "http://jf"),
            ("silo", "Silo", "http://silo"),
            ("sonarr", "Sonarr", "http://sonarr"),
            ("radarr", "Radarr", "http://radarr"),
        ]:
            db.add(Source(type=typ, name=name, base_url=url, secret_enc=box.encrypt("k")))
        db.add(Source(type="disk", name="Media share", base_url=str(share)))
        db.commit()
        ids = [s.id for s in db.execute(select(Source).order_by(Source.id)).scalars()]
    syncs = app.state.syncs
    for sid in ids:  # sequential pool: master first, disk last
        syncs.start(sid)
    syncs._pool.shutdown(wait=True)
    with app.state.db.sessionmaker() as db:
        from mediawrangler.models import SyncRun

        runs = db.execute(select(SyncRun)).scalars().all()
        assert all(r.status == "ok" for r in runs), [r.error for r in runs]
        yield db


def _tv(db):
    return db.execute(select(LibraryGroup).where(LibraryGroup.name == "TV")).scalar_one()


def _src(db, name):
    return db.execute(select(Source).where(Source.name == name)).scalar_one()


def test_video_and_extras_detection():
    assert is_video("/a/b.MKV") and not is_video("/a/b.srt")
    assert is_extra("/m/Movies/X/Featurettes/a.mkv") and is_extra("/m/Movies/X/x-trailer.mp4")
    assert is_extra("/m/Movies/X/sample.mkv")
    assert not is_extra("/m/TV/Other Show/Season 01/a.mkv")


def test_disk_and_arr_libraries_join_groups_and_path_maps_are_derived(synced, share):
    db = synced
    tv = _tv(db)
    names = set(
        db.execute(
            select(Source.name, Library.name)
            .join(Library, Library.source_id == Source.id)
            .join(LibraryGroupMember, LibraryGroupMember.library_id == Library.id)
            .where(LibraryGroupMember.group_id == tv.id)
        ).all()
    )
    assert names == {
        ("Jellyfin", "TV"),
        ("Silo", "Shows"),
        ("Sonarr", "/data/TV"),
        ("Media share", "TV"),
    }
    assert _src(db, "Sonarr").path_map == [["/data/TV", str(share / "TV")]]
    assert ["/mnt/media/TV", str(share / "TV")] in _src(db, "Silo").path_map
    # Music isn't in any group, so it was never walked.
    disk = _src(db, "Media share")
    paths = db.execute(select(MediaFile.path).where(MediaFile.source_id == disk.id)).scalars().all()
    assert not any("/Music/" in p for p in paths)
    assert not any(p.endswith(".jpg") for p in paths)


def test_physical_view_tv(synced):
    db = synced
    view = build_physical_view(db, _tv(db))
    jf, silo, sonarr, disk = (_src(db, n).id for n in ("Jellyfin", "Silo", "Sonarr", "Media share"))
    assert [c.id for c in view.columns] == [jf, silo, disk, sonarr]
    rows = {r.name: r for r in view.rows}

    sherlock = rows["Sherlock"]
    assert sherlock.cells[disk].files == 4  # the featurette is an extra
    assert sherlock.cells[jf].files == 4 and sherlock.cells[sonarr].files == 4
    # Silo's catalog has 2 regular episodes; its root's observed_file_count (1)
    # is ignored because the real Silo reports nonsense there.
    assert sherlock.cells[silo].reported == 2
    assert sherlock.flags == {f"count:{silo}", "count"}

    # Jellyfin knows The Stand but has no episode files for it.
    assert f"not_picked_up:{jf}" in rows["The Stand (1994)"].flags

    orphan = rows["Orphan Show"]
    assert {"unmanaged", f"not_picked_up:{jf}", f"skipped:{silo}"} <= orphan.flags
    assert orphan.cells[silo].reason == "missing_folder_ids"

    assert "duplicate" in rows["Dup Show"].flags and rows["Dup Show"].cells[disk].duplicate
    assert "not_on_disk" in rows["Gone Show"].flags
    assert "Wanted Show" not in rows  # no files anywhere: an arr wish, not a folder

    assert view.summaries[disk].issues["not_on_disk"] == 1
    assert view.summaries[jf].issues["not_picked_up"] == 3  # The Stand, Orphan, Dup
    assert view.summaries[silo].issues["skipped"] == 1
    assert [r.name for r in filter_rows(view, "skipped", source_id=silo)] == ["Orphan Show"]


def test_one_pace_silo_roots_fold_into_one_folder(synced):
    db = synced
    anime = db.execute(select(LibraryGroup).where(LibraryGroup.name == "Anime")).scalar_one()
    view = build_physical_view(db, anime)
    silo = _src(db, "Silo").id
    one_pace = next(r for r in view.rows if r.name == "One Pace")
    assert one_pace.cells[silo].state == "unmatched" and one_pace.cells[silo].present


def test_folder_detail_lines_up_files(synced):
    db = synced
    view = build_physical_view(db, _tv(db))
    row = next(r for r in view.rows if r.name == "Sherlock")
    lines, roots = folder_detail(db, view, row.folder)
    regular = [line for line in lines if not line.extra]
    assert len(regular) == 4 and all(len(line.by_source) == 3 for line in regular)
    assert [line.path for line in lines if line.extra] == ["Featurettes/Making Of.mkv"]
    assert [r.state for r in roots] == ["resolved"]


def test_hand_edited_path_map_is_kept_and_failures_reported(synced, logged_in, app, share):
    db = synced
    sonarr = _src(db, "Sonarr")
    resp = post(
        logged_in,
        f"/settings/connectors/{sonarr.id}",
        {
            "name": "Sonarr",
            "base_url": "http://sonarr",
            "secret": "",
            "path_map": "/data/Anime => /media/Anime",
            "enabled": "on",
        },
    )
    assert resp.status_code == 303
    db.expire_all()
    sonarr = _src(db, "Sonarr")
    assert sonarr.options["path_map_auto"] is False
    view = build_physical_view(db, _tv(db))
    assert view.unmapped[sonarr.id].count == 7
    # A later sync doesn't overwrite a hand-edited map.
    app.state.syncs.rematch()
    db.expire_all()
    assert _src(db, "Sonarr").path_map == [["/data/Anime", "/media/Anime"]]
    # Clearing it goes back to automatic.
    post(
        logged_in,
        f"/settings/connectors/{sonarr.id}",
        {
            "name": "Sonarr",
            "base_url": "http://sonarr",
            "secret": "",
            "path_map": "",
            "enabled": "on",
        },
    )
    db.expire_all()
    assert _src(db, "Sonarr").path_map == [["/data/TV", str(share / "TV")]]
    assert not build_physical_view(db, _tv(db)).unmapped


def test_arr_items_stay_out_of_the_presence_matrix(synced):
    from mediawrangler.presence import build_group_view

    db = synced
    view = build_group_view(db, _tv(db))
    assert [c.name for c in view.columns] == ["Jellyfin", "Silo"]
    assert not any(r.canonical.title == "Gone Show" for r in view.rows)
    gone = db.execute(select(SourceItem).where(SourceItem.title == "Gone Show")).scalar_one()
    assert gone.source_status == "has_files"


def test_physical_pages_render(synced, logged_in):
    db = synced
    tv = _tv(db)
    for flt in (
        "issues",
        "all",
        "not_picked_up",
        "skipped",
        "unmanaged",
        "not_on_disk",
        "count",
        "duplicate",
    ):
        assert logged_in.get(f"/physical?group={tv.id}&filter={flt}").status_code == 200
    page = logged_in.get(f"/physical?group={tv.id}")
    assert "Orphan Show" in page.text and "missing folder ids" in page.text
    view = build_physical_view(db, tv)
    row = next(r for r in view.rows if r.name == "Sherlock")
    detail = logged_in.get("/physical/folder", params={"group": tv.id, "folder": row.folder})
    assert detail.status_code == 200 and "Making Of.mkv" in detail.text
    assert (
        logged_in.get("/physical/folder", params={"group": tv.id, "folder": "/nope"}).status_code
        == 404
    )


def test_disk_source_created_once_and_not_after_delete(app, tmp_path, settings, logged_in):
    share = tmp_path / "share2"
    (share / "TV").mkdir(parents=True)
    settings.media_dir = share
    ensure_disk_source(app.state.db, settings)
    ensure_disk_source(app.state.db, settings)
    with app.state.db.sessionmaker() as db:
        disks = db.execute(select(Source).where(Source.type == "disk")).scalars().all()
        assert len(disks) == 1
        disk_id = disks[0].id
    post(logged_in, f"/settings/connectors/{disk_id}/delete")
    ensure_disk_source(app.state.db, settings)
    with app.state.db.sessionmaker() as db:
        assert not db.execute(select(Source).where(Source.type == "disk")).first()


def test_recanonicalise_after_map_change(synced, share):
    db = synced
    jf = _src(db, "Jellyfin")
    jf.path_map = [["/data", "/elsewhere"]]
    db.commit()
    recanonicalise(db, jf)
    some = db.execute(select(MediaFile).where(MediaFile.source_id == jf.id)).scalars().first()
    assert some.canonical_path.startswith("/elsewhere/")


def test_silo_movie_versions_are_files(synced):
    db = synced
    movies = db.execute(select(LibraryGroup).where(LibraryGroup.name == "Movies")).scalar_one()
    view = build_physical_view(db, movies)
    silo, radarr, disk = (_src(db, n).id for n in ("Silo", "Radarr", "Media share"))
    (heat,) = view.rows
    assert (
        heat.cells[silo].files == 1
        and heat.cells[radarr].files == 1
        and heat.cells[disk].files == 1
    )
    assert not heat.flags


def test_physical_page_has_every_folder_and_instant_filters(synced, logged_in):
    db = synced
    tv = _tv(db)
    jf, silo = _src(db, "Jellyfin").id, _src(db, "Silo").id
    view = build_physical_view(db, tv)
    page = logged_in.get(f"/physical?group={tv.id}")
    options = filter_options(page.text)
    assert options[0] == ("issues", "All issues", True) and options[-1][0] == "all"
    rows = table_rows(page.text)
    assert len(rows) == len(view.rows) >= 5
    by_search = {search: (flags.split(), hidden) for flags, search, hidden in rows}
    # A folder with no problems is only hidden; one with problems is shown.
    assert by_search["sherlock"][0] == ["count", f"count:{silo}"] and not by_search["sherlock"][1]
    clean = [s for s, (f, h) in by_search.items() if not f]
    assert all(by_search[s][1] for s in clean)
    orphan_flags, orphan_hidden = by_search["orphan show"]
    assert (
        not orphan_hidden and f"not_picked_up:{jf}" in orphan_flags and "unmanaged" in orphan_flags
    )
    assert "Previous" not in page.text and "Page 1 of" not in page.text
    skipped = logged_in.get(f"/physical?group={tv.id}&filter=skipped&source={silo}")
    assert [s for f, s, h in table_rows(skipped.text) if not h] == ["orphan show"]
    everything = logged_in.get(f"/physical?group={tv.id}&filter=all")
    assert not any(h for _f, _s, h in table_rows(everything.text))
    assert logged_in.get(f"/physical?group={tv.id}&page=2").status_code == 200
    assert "onchange=" not in page.text and 'src="/static/table-filter.js' in page.text


def test_physical_views_are_cached(synced, logged_in, app):
    db = synced
    tv = _tv(db)
    logged_in.get(f"/physical?group={tv.id}")
    cached = app.state.views._entries[("physical", tv.id)][1]
    logged_in.get(f"/physical?group={tv.id}&filter=all")
    assert app.state.views._entries[("physical", tv.id)][1] is cached
    row = next(r for r in cached.rows if r.name == "Sherlock")
    assert (
        logged_in.get("/physical/folder", params={"group": tv.id, "folder": row.folder}).status_code
        == 200
    )


def test_arr_cells_tell_unknown_from_tracked_without_files(synced, logged_in):
    from mediawrangler.models import Library, SourceItem

    db = synced
    tv = _tv(db)
    sonarr = _src(db, "Sonarr")
    root = db.execute(select(Library).where(Library.source_id == sonarr.id)).scalar_one()
    # Sonarr tracks Orphan Show but has imported nothing for it.
    db.add(
        SourceItem(
            source_id=sonarr.id,
            external_id="series-99",
            library_id=root.id,
            kind="series",
            title="Orphan Show",
            path="/data/TV/Orphan Show",
        )
    )
    db.commit()
    view = build_physical_view(db, tv)
    rows = {r.name: r for r in view.rows}
    assert (
        rows["Orphan Show"].cells[sonarr.id].tracked
        and not rows["Orphan Show"].cells[sonarr.id].present
    )
    assert not rows["Dup Show"].cells[sonarr.id].tracked
    assert rows["Sherlock"].cells[sonarr.id].present  # files win over tracking
    # No row appears just because an arr tracks a show with nothing anywhere.
    assert "Wanted Show" not in rows

    page = logged_in.get(f"/physical?group={tv.id}&filter=all").text
    orphan = page.split("<strong>Orphan Show</strong>")[1].split("</tr>")[0]
    dup = page.split("<strong>Dup Show</strong>")[1].split("</tr>")[0]
    assert "No files" in orphan and "tracked, nothing imported" in orphan
    assert "Not in Sonarr" in dup and "Not picked up" in dup  # Jellyfin and Silo
    assert ">None<" not in page


def test_disk_scan_finds_ogm_episodes(tmp_path):
    """Virtua Fighter (1995) is 24 .ogm files; the scan used to skip them."""
    from mediawrangler.connectors import ConnectorConfig
    from mediawrangler.connectors.disk import DiskConnector

    season = tmp_path / "Anime" / "Virtua Fighter (1995)" / "Season 01"
    season.mkdir(parents=True)
    for n in (1, 2):
        (season / f"Virtua Fighter (1995) - S01E0{n} - [DivX][Vorbis 2.0]-ASS.ogm").write_bytes(
            b"x"
        )
    (season / "cover.png").write_bytes(b"x")
    (tmp_path / "Music").mkdir()
    (tmp_path / "Music" / "song.ogm").write_bytes(b"x")  # not in a group: never read
    conn = DiskConnector(ConnectorConfig(str(tmp_path), options={"scan_libraries": ["Anime"]}))
    files = conn.fetch_full().files
    assert sorted(f.path.rsplit("/", 1)[1][:34] for f in files) == [
        "Virtua Fighter (1995) - S01E01 - [",
        "Virtua Fighter (1995) - S01E02 - [",
    ]


def test_physical_summary_shows_difference_from_the_master(synced, logged_in):
    db = synced
    tv = _tv(db)
    page = logged_in.get(f"/physical?group={tv.id}").text
    # The master, Jellyfin, is the reference: 5 files in 2 folders, no badge.
    jellyfin = summary_col(page, "Jellyfin")
    assert jellyfin.startswith("★ Jellyfin Jellyfin Files 5 Folders 2")
    assert "✓" not in jellyfin.split("Folders")[0]
    # The share has 9 files in 5 folders; Sonarr 7 in 4 (it still lists a show that left the disk).
    assert summary_col(page, "Media share").startswith(
        "Media share Media share (disk) Files 9 +4 Folders 5 +3"
    )
    sonarr = summary_col(page, "Sonarr")
    assert sonarr.startswith("Sonarr Sonarr Files 7 +2 Folders 4 +2")
    assert "2 more than Jellyfin" in page
    assert "compare each app with" not in page
    # Directional counts carry their sign: folders on the share the arr never imported.
    assert "On disk, not in any arr −2" in sonarr


def test_overview_badges_only_compare_media_servers(synced, logged_in):
    page = logged_in.get("/").text
    metadata = page.split("<h2>Metadata</h2>")[1].split("<h2>Physical</h2>")[0]
    head, silo_row = metadata.split("<tbody>")[0], metadata.split("<tbody>")[1]
    assert "Silo" in head and "Sonarr" in head  # arrs are columns too
    assert "tracked" in silo_row  # arrs say how many titles they track, uncompared
    assert 'class="diff' in silo_row  # media servers are compared with the master


def test_every_page_names_apps_as_star_name_type(synced, logged_in):
    db = synced
    tv = _tv(db)
    pages = {
        "overview": "/",
        "presence": f"/presence?group={tv.id}",
        "physical": f"/physical?group={tv.id}",
    }
    for label, url in pages.items():
        html = logged_in.get(url).text
        assert '<span class="star" data-tip="Master">★</span> Jellyfin' in html, label
        assert "★</span> Silo" not in html and "★</span> Sonarr" not in html, label
        assert '<span class="tag">master</span>' not in html, label
        assert '<span class="tag">reference</span>' not in html, label
        assert (
            '<span class="tag">arr</span>' not in html
            and '<span class="tag">on disk</span>' not in html
        )
        assert (
            '<span class="tag">Jellyfin</span>' in html and '<span class="tag">Silo</span>' in html
        ), label
    physical = logged_in.get(pages["physical"]).text
    assert '<span class="tag">Sonarr</span>' in physical
    assert '<span class="tag">Media share (disk)</span>' in physical
    assert logged_in.get("/settings/connectors").text.count("★</span> Jellyfin") == 1


def test_presence_shows_what_the_arrs_track_without_comparing_them(synced, logged_in):
    db = synced
    tv = _tv(db)
    view = build_group_view(db, tv)
    assert [a.name for a in view.arrs] == ["Sonarr"]
    rows = {r.canonical.title: r for r in view.rows}
    sonarr = view.arrs[0].id
    assert rows["Sherlock"].cells[sonarr].state == "tracked"
    assert "Orphan Show" not in rows  # an arr alone doesn't add a title
    assert view.summaries[sonarr].total == sum(
        r.cells[sonarr].state == "tracked" for r in view.rows
    )
    # No comparison flags (missing, count...); the one thing an arr can flag is a title it
    # tracks under other IDs than the servers' (The Stand).
    compared = [f for r in view.rows for f in r.flags if f.endswith(f":{sonarr}")]
    assert set(compared) == {f"arr_mismatch:{sonarr}"}  # (a row for each The Stand)
    page = logged_in.get(f"/presence?group={tv.id}&filter=all").text
    assert "Tracked" in page and "Not tracked" in page and "are not compared" not in page
    assert "Sonarr" in summary_col(page, "Sonarr")


def test_folder_marked_not_managed_by_an_arr(synced, logged_in, app):
    from mediawrangler.models import AuditLog
    from mediawrangler.physical import NOT_MANAGED_KEY
    from mediawrangler.settings_export import export_settings

    db = synced
    tv = _tv(db)
    sonarr = _src(db, "Sonarr")
    folder = next(r for r in build_physical_view(db, tv).rows if r.name == "Dup Show").folder
    start = build_physical_view(db, tv)
    before = start.summaries[sonarr.id].issues["unmanaged"]
    sonarr_files = start.summaries[sonarr.id].files
    sonarr_folders = start.summaries[sonarr.id].folders
    dup_files = next(r for r in start.rows if r.folder == folder).cells[start.disk_id].files
    assert dup_files > 0

    dialog = logged_in.get("/fix/not-managed", params={"group": tv.id, "folder": folder})
    assert dialog.status_code == 200 and "Mark as not managed" in dialog.text
    form = {"group": tv.id, "folder": folder, "reason": "Fan edit"}
    done = post(logged_in, "/fix/not-managed", form)
    assert done.status_code == 200
    assert "Marked Dup Show as not managed by an arr: Fan edit" in done.text

    db.expire_all()
    view = build_physical_view(db, tv)
    row = next(r for r in view.rows if r.folder == folder)
    assert row.not_managed == "Fan edit" and "unmanaged" not in row.flags
    assert view.summaries[sonarr.id].issues["unmanaged"] == before - 1
    # Sonarr's totals (Overview and Physical summaries) count the folder's files from the share.
    assert view.summaries[sonarr.id].files == sonarr_files + dup_files
    assert view.summaries[sonarr.id].folders == sonarr_folders + 1
    assert row.name not in [r.name for r in filter_rows(view, "unmanaged")]
    page = logged_in.get(f"/physical?group={tv.id}&filter=all").text
    cells = page.split("<strong>Dup Show</strong>")[1].split("</tr>")[0]
    assert "Not managed" in cells and "Fan edit" in cells and "Not in Sonarr" not in cells
    assert "Manage again" in cells  # the menu stays, to take the mark off
    exported = export_settings(db, app.state.box)["settings"][NOT_MANAGED_KEY]
    assert exported[folder]["reason"] == "Fan edit"
    audit = db.execute(select(AuditLog).where(AuditLog.item_id == folder)).scalars().all()
    assert [a.op for a in audit] == ["unmanage"]

    post(logged_in, "/fix/not-managed", {"group": tv.id, "folder": folder, "clear": "true"})
    db.expire_all()
    cleared = build_physical_view(db, tv)
    row = next(r for r in cleared.rows if r.folder == folder)
    assert row.not_managed is None and "unmanaged" in row.flags
    assert cleared.summaries[sonarr.id].files == sonarr_files
    page = logged_in.get(f"/physical?group={tv.id}&filter=all").text
    assert "Not in Sonarr" in page.split("<strong>Dup Show</strong>")[1].split("</tr>")[0]
    nope = logged_in.get("/fix/not-managed", params={"group": tv.id, "folder": "/nope"})
    assert nope.status_code == 404
