"""Telling Sonarr which episode an orphaned file is (0.12.84)."""

import pytest
from sqlalchemy import select

from mediawrangler import episode_import, writes
from mediawrangler.connectors.arr import SonarrConnector
from mediawrangler.fixes import FixRefused
from mediawrangler.models import AuditLog

from .conftest import post
from .test_physical import share, synced  # noqa: F401
from .test_trash import orphan  # noqa: F401

EPISODES = [
    {"id": i, "seasonNumber": s, "episodeNumber": e, "title": t, "hasFile": f}
    for i, s, e, t, f in (
        (11, 1, 1, "A Study in Pink", True),
        (90, 0, 1, "Unaired Pilot", False),
        (12, 1, 2, "The Blind Banker", False),
    )
]


@pytest.fixture
def sonarr_api(orphan, monkeypatch):  # noqa: F811
    """Sonarr's episode list and Manual Import answers, and a record of what is sent."""
    sent = []
    found = {"rejections": []}

    def get_json(self, path, **params):
        if path == "/api/v3/episode":
            return EPISODES
        if path == "/api/v3/manualimport":
            return [
                {
                    "path": params["folder"] + "/" + p.rsplit("/", 1)[-1],
                    "quality": {"quality": {"name": "Bluray-1080p"}},
                    "languages": [{"id": 1, "name": "English"}],
                    "rejections": found["rejections"],
                }
                for p in [orphan[5]]
            ]
        raise AssertionError(path)

    monkeypatch.setattr(SonarrConnector, "_get_json", get_json)
    monkeypatch.setattr(
        SonarrConnector,
        "import_episode",
        lambda self, ext, path, ep, f: sent.append((ext, path, ep)) or "77",
    )
    return sent, found


def _plan(db, group, sonarr, folder, path, view):
    return episode_import.plan(db, group, sonarr, folder, path, view.roots)


def test_episodes_come_in_episode_order_with_specials_last(orphan, app, sonarr_api):  # noqa: F811
    db, group, view, folder, sonarr, disk_file, *_ = orphan
    p = _plan(db, group, sonarr, folder, disk_file, view)
    eps = episode_import.episodes(app.state.box, p)
    assert [episode_import.label(e) for e in eps] == [
        "S01E01 · A Study in Pink",
        "S01E02 · The Blind Banker",
        "S00E01 · Unaired Pilot",
    ]


def test_the_import_is_previewed_checked_and_sent(orphan, app, sonarr_api):  # noqa: F811
    db, group, view, folder, sonarr, disk_file, *_ = orphan
    sent, found = sonarr_api
    box = app.state.box
    p = _plan(db, group, sonarr, folder, disk_file, view)
    seen = episode_import.preview(box, p, 90)
    assert seen["label"] == "S00E01 · Unaired Pilot" and seen["quality"] == "Bluray-1080p"
    entry = seen["body"]["files"][0]
    assert seen["body"]["name"] == "ManualImport" and seen["body"]["importMode"] == "move"
    assert entry["episodeIds"] == [90] and entry["path"] == p.app_path
    with pytest.raises(FixRefused, match="already has a file"):
        episode_import.preview(box, p, 11)

    with pytest.raises(FixRefused, match="switched off"):
        episode_import.run(db, box, p, 90)
    writes.set_flag(db, writes.ENABLED_KEY, True)
    found["rejections"] = [{"reason": "Not a sample"}]
    with pytest.raises(FixRefused, match="would refuse it"):
        episode_import.run(db, box, p, 90)
    assert sent == []
    found["rejections"] = []
    assert episode_import.run(db, box, p, 90) == "77"
    assert sent == [(p.item.external_id, p.app_path, 90)]
    (log,) = db.execute(select(AuditLog).where(AuditLog.op == "import")).scalars()
    assert log.result == "applied" and "Unaired Pilot" in log.why

    with pytest.raises(FixRefused, match="not inside"):
        _plan(db, group, sonarr, folder + "-other", disk_file, view)


def test_the_folder_page_and_dialog(orphan, app, logged_in, sonarr_api, monkeypatch):  # noqa: F811
    db, group, view, folder, sonarr, disk_file, *_ = orphan
    page = logged_in.get("/physical/folder", params={"group": group.id, "folder": folder}).text
    assert 'class="card-pair"' in page and "Choose the episode…" in page
    q = {"group": group.id, "source": sonarr.id, "folder": folder, "path": disk_file}
    dialog = logged_in.get("/fix/import", params=q).text
    assert 'data-filter-select="episode-pick"' in dialog
    assert '<option value="11" disabled>' in dialog and "S00E01 · Unaired Pilot" in dialog
    preview = post(logged_in, "/fix/import/preview", {**q, "episode": 90}).text
    assert "ManualImport" in preview and "Import into Sonarr" in preview

    writes.set_flag(db, writes.ENABLED_KEY, True)
    started = []
    monkeypatch.setattr(
        app.state.syncs,
        "import_job",
        lambda g, s, f, path, ep, label: started.append((path, ep, label)) or 5,
    )
    done = post(logged_in, "/fix/import", {**q, "episode": 90})
    assert "data-close-now" in done.text
    assert started == [(disk_file, 90, f"Importing {disk_file.rsplit('/', 1)[-1]} into Sonarr")]


def test_a_file_imported_in_place_is_renamed_into_its_season_folder(
    orphan,  # noqa: F811
    app,
    monkeypatch,
):
    db, group, view, folder, sonarr, disk_file, *_ = orphan
    p = _plan(db, group, sonarr, folder, disk_file, view)
    pending = [  # another file in the show is out of date too: it is never sent
        {"episodeFileId": 777, "newPath": "Season 01/Show - S01E05.mkv"},
        {"episodeFileId": 501, "newPath": "Specials/Show - S00E01.mkv"},
    ]

    def get_json(self, path, **params):
        if path == "/api/v3/episode/90":
            return {"id": 90, "episodeFileId": 501}
        if path == "/api/v3/rename":
            return list(pending)
        raise AssertionError(path)

    posted = []

    class Answer:
        status_code = 201

        def json(self):
            return {"id": 4242}

    monkeypatch.setattr(SonarrConnector, "_get_json", get_json)
    monkeypatch.setattr(
        "httpx.Client.post", lambda self, url, **kw: posted.append(kw["json"]) or Answer()
    )
    assert episode_import.rename(db, app.state.box, p, 90) == ("4242", "Specials/Show - S00E01.mkv")
    assert [(b["name"], b["files"]) for b in posted] == [("RenameFiles", [501])]
    pending.clear()  # Sonarr already moved it: nothing to rename
    assert episode_import.rename(db, app.state.box, p, 90) is None and len(posted) == 1
