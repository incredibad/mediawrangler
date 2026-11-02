"""Settings -> Libraries: a section per library, a row per app, and placing libraries (0.12.3)."""

from mediawrangler.groups import fits_group, library_sections
from mediawrangler.models import Library, LibraryGroup, LibraryGroupMember, Source

from .conftest import post


def _setup(app):
    with app.state.db.sessionmaker() as s:
        jf = Source(
            type="jellyfin",
            name="Jellyfin",
            base_url="http://jf",
            path_map=[["/data/Anime", "/media/Anime"]],
        )
        silo = Source(type="silo", name="Silo", base_url="http://silo")
        disk = Source(type="disk", name="Media share", base_url="/media")
        s.add_all([jf, silo, disk])
        s.flush()
        j = Library(
            source_id=jf.id,
            external_id="1",
            name="Anime",
            kind="series",
            paths=["/data/Anime"],
            group_assigned=True,
        )
        si = Library(
            source_id=silo.id, external_id="1", name="Anime", kind="series", paths=["/mnt/Anime"]
        )
        d = Library(
            source_id=disk.id,
            external_id="Anime",
            name="Anime",
            kind="folder",
            paths=["/media/Anime"],
        )
        s.add_all([j, si, d])
        s.flush()
        g = LibraryGroup(name="Anime", kind="series")
        s.add(g)
        s.flush()
        s.add(LibraryGroupMember(group_id=g.id, library_id=j.id))
        s.commit()
        return g.id, si.id, d.id


def test_a_disk_folder_fits_a_group_of_any_kind():
    folder = Library(kind="folder")
    assert fits_group(LibraryGroup(kind="series"), folder) and fits_group(
        LibraryGroup(kind="movie"), folder
    )
    assert not fits_group(LibraryGroup(kind="movie"), Library(kind="series"))


def test_sections_show_every_app_and_what_is_missing(app):
    gid, _silo_lib, _disk_lib = _setup(app)
    with app.state.db.sessionmaker() as s:
        sections, left_out = library_sections(s)
    (sec,) = sections
    rows = {r.source.name: r for r in sec.rows}
    assert list(rows) == ["Jellyfin", "Silo", "Media share"]
    assert not sec.has_reference
    assert rows["Jellyfin"].status == "no_reference"
    assert rows["Silo"].status == "not_set" and rows["Media share"].status == "not_set"
    assert rows["Jellyfin"].libraries[0].paths == [("/data/Anime", "/media/Anime")]
    assert sorted(src.name for _lib, src in left_out) == ["Media share", "Silo"]


def test_a_share_folder_can_be_put_back_and_taken_out(logged_in, app):
    gid, silo_lib, disk_lib = _setup(app)
    page = logged_in.get("/settings/groups").text
    assert "No media share folder is set for Anime" in page
    assert (
        post(logged_in, f"/settings/groups/{gid}/add", {"library_id": disk_lib}).status_code == 303
    )
    post(logged_in, f"/settings/groups/{gid}/add", {"library_id": silo_lib})
    with app.state.db.sessionmaker() as s:
        sections, _ = library_sections(s)
    assert sections[0].has_reference
    with app.state.db.sessionmaker() as s:  # the path rule follows at once
        assert s.query(Source).filter_by(name="Silo").one().path_map == [
            ["/mnt/Anime", "/media/Anime"]
        ]
    assert {r.source.name: r.status for r in sections[0].rows}["Media share"] == "ok"
    page = logged_in.get("/settings/groups").text
    assert "No media share folder" not in page and "the reference" in page
    gone = post(logged_in, f"/settings/groups/library/{disk_lib}", {"group_id": ""})
    assert gone.status_code == 303
    page = logged_in.get("/settings/groups").text
    assert "Not in a library" in page and "No media share folder is set" in page


def test_a_library_cannot_join_a_group_of_another_kind(logged_in, app):
    gid, silo_lib, _ = _setup(app)
    with app.state.db.sessionmaker() as s:
        movie = LibraryGroup(name="Movies", kind="movie")
        s.add(movie)
        s.commit()
        movie_id = movie.id
    assert (
        post(logged_in, f"/settings/groups/{movie_id}/add", {"library_id": silo_lib}).status_code
        == 400
    )
    assert post(logged_in, "/settings/groups/9999/add", {"library_id": silo_lib}).status_code == 404


def test_libraries_can_be_reordered(logged_in, app):
    with app.state.db.sessionmaker() as s:
        groups = [LibraryGroup(name=n, kind="series", position=0) for n in ("TV", "Anime", "Kids")]
        s.add_all(groups)
        s.commit()
        tv, anime, kids = (g.id for g in groups)
    assert post(logged_in, f"/settings/groups/{kids}/move", {"direction": "up"}).status_code == 303
    post(logged_in, f"/settings/groups/{tv}/move", {"direction": "up"})  # already first
    with app.state.db.sessionmaker() as s:
        order = [g.id for g in s.query(LibraryGroup).order_by(LibraryGroup.position)]
    assert order == [tv, kids, anime]
    post(logged_in, f"/settings/groups/{tv}/move", {"direction": "down"})
    page = logged_in.get("/settings/groups").text
    assert page.index("<h2>Kids") < page.index("<h2>TV") < page.index("<h2>Anime")
