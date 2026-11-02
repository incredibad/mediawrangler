"""Sonarr, Radarr and Sportarr. Each running instance is its own source.

Root folders become libraries, series/movies become items, and the files the
arr has on record become files. Read-only throughout.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .base import (
    Capability,
    ConnectionTest,
    Connector,
    ConnectorError,
    FileRecord,
    ItemGone,
    ItemRecord,
    LibraryRecord,
    MatchCandidate,
    ProgressFn,
    Snapshot,
    Task,
    TitleSnapshot,
    _no_progress,
    describe_http_failure,
    register,
)

WORKERS = 4
SXXEXX_RE = re.compile(r"S(\d{1,4})E(\d{1,4})", re.IGNORECASE)


def _root_for(path: str | None, roots: list[LibraryRecord]) -> str | None:
    """The root folder containing a path (longest match)."""
    if not path:
        return None
    best = None
    for root in roots:
        prefix = root.paths[0].rstrip("/") + "/"
        if path.startswith(prefix) and (best is None or len(prefix) > len(best[1])):
            best = (root.external_id, prefix)
    return best[0] if best else None


def _str_id(value: Any) -> str | None:
    return str(value) if value not in (None, 0, "", "0") else None


class _ArrConnector(Connector):
    role = "arr"
    capabilities = frozenset({Capability.LIST_ITEMS, Capability.LIST_FILES})
    status_path = "/api/v3/system/status"
    rootfolder_path = "/api/v3/rootfolder"
    root_kind = "series"
    key_help = "Settings → General → Security → API Key."

    def _auth_headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.config.secret or ""}

    def rescan_body(self, item_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def task_call(self, task: Task) -> tuple[str, str, Any]:
        """POST /api/v3/command: the app re-reads the files of one show or film."""
        return ("POST", "/api/v3/command", self.rescan_body(task.item_id or ""))

    rescan_all = ""  # the command that re-reads every show or film

    def scan_all_calls(self, library_ids: list[str]) -> list[tuple[str, str, Any]]:
        """POST /api/v3/command with no id: the app re-reads the files of everything it tracks."""
        return [("POST", "/api/v3/command", {"name": self.rescan_all})]

    lookup_path = ""  # "/api/v3/series/lookup" or "/api/v3/movie/lookup"
    add_path = ""

    def lookup(self, term: str) -> list[MatchCandidate]:
        """GET <series|movie>/lookup?term=: the app's search of its metadata source."""
        out = []
        for raw in self._get_json(self.lookup_path, term=term)[:30]:
            ids = {
                "tvdb": _str_id(raw.get("tvdbId")),
                "tmdb": _str_id(raw.get("tmdbId")),
                "imdb": raw.get("imdbId") or None,
            }
            out.append(
                MatchCandidate(
                    title=raw.get("title") or "",
                    year=raw.get("year") or None,
                    ids={k: v for k, v in ids.items() if v and v != "0"},
                    overview=raw.get("overview") or "",
                    payload=raw,
                )
            )
        return out

    def web_link(self, external_id: str) -> str | None:
        """/series/<slug> or /movie/<slug>: the slug comes from the app (GET the title)."""
        kind, _, num = external_id.rpartition("-")
        data = self._get_json(f"/api/v3/{kind}/{num}")
        slug = data.get("titleSlug")
        return f"{self.base_url}/{kind}/{slug}" if slug else None

    def _profile(self) -> int:
        profiles = self._get_json("/api/v3/qualityprofile")
        if not profiles:
            raise ConnectorError(
                f"{self.display_name} has no quality profile to add the title with"
            )
        return profiles[0]["id"]

    def _root_for_folder(self, folder: str) -> str:
        roots = [r["path"] for r in self._get_json(self.rootfolder_path)]
        for root in sorted(roots, key=len, reverse=True):
            if folder.startswith(root.rstrip("/") + "/"):
                return root
        raise ConnectorError(f"{self.display_name} has no root folder that holds {folder}")

    def add_body(self, candidate: MatchCandidate, folder: str) -> dict[str, Any]:
        raise NotImplementedError

    def add_call(self, candidate: MatchCandidate, folder: str) -> tuple[str, str, Any]:
        return ("POST", self.add_path, self.add_body(candidate, folder))

    def add_title(self, candidate: MatchCandidate, folder: str) -> str:
        """POST /api/v3/series (or movie): adds it tracking the existing folder; the app reads
        the files already there. Not monitored, so nothing is searched for or downloaded."""
        _method, path, body = self.add_call(candidate, folder)
        try:
            resp = self._client.post(
                f"{self.base_url}{path}", headers=self._auth_headers(), json=body, timeout=60
            )
        except Exception as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if resp.status_code not in (200, 201):
            raise ConnectorError(f"POST {path}: {describe_http_failure(resp)}")
        return f"{self.root_kind}-{resp.json()['id']}"

    def task_token(self, resp) -> str | None:
        """The command's id, from the answer to POST /api/v3/command."""
        try:
            return str(resp.json()["id"])
        except (ValueError, KeyError, TypeError):
            return None

    def task_done(self, token: str) -> bool:
        """GET /api/v3/command/{id}: queued and started are not done; completed is."""
        data = self._get_json(f"/api/v3/command/{token}")
        status = str(data.get("status") or "").lower()
        if status == "completed":
            return True
        if status in ("failed", "aborted", "cancelled", "orphaned"):
            why = data.get("message") or ""
            raise ConnectorError(f"{self.display_name} reported the task {status}: {why}".strip())
        return False

    def _get_json(self, path: str, **params) -> Any:
        resp = self._get(path, headers=self._auth_headers(), params=params, timeout=120)
        if resp.status_code != 200:
            raise ConnectorError(f"GET {path}: {describe_http_failure(resp)}")
        return resp.json()

    def test_connection(self) -> ConnectionTest:
        try:
            resp = self._get(self.status_path, headers=self._auth_headers())
            if resp.status_code != 200:
                return ConnectionTest(False, describe_http_failure(resp))
            body = resp.json()
        except (ConnectorError, ValueError) as exc:
            return ConnectionTest(False, str(exc))
        app = body.get("appName") or self.display_name
        return ConnectionTest(True, f"Connected to {app}", body.get("version"))

    def libraries(self) -> list[LibraryRecord]:
        return [
            LibraryRecord(
                external_id=str(r["id"]),
                name=r["path"],
                kind=self.root_kind,
                paths=[r["path"]],
            )
            for r in self._get_json(self.rootfolder_path)
        ]


@register
class SonarrConnector(_ArrConnector):
    type = "sonarr"
    display_name = "Sonarr"
    lookup_path = "/api/v3/series/lookup"
    add_path = "/api/v3/series"
    capabilities = _ArrConnector.capabilities | {
        Capability.SCAN,
        Capability.LOOKUP,
        Capability.ADD,
    }

    rescan_all = "RescanSeries"

    def rescan_body(self, item_id: str) -> dict[str, Any]:
        return {"name": "RescanSeries", "seriesId": int(item_id.rsplit("-", 1)[-1])}

    def add_body(self, candidate: MatchCandidate, folder: str) -> dict[str, Any]:
        body = {
            **candidate.payload,
            "qualityProfileId": self._profile(),
            "rootFolderPath": self._root_for_folder(folder),
            "path": folder,
            "monitored": False,
            "seasonFolder": True,
            "addOptions": {"monitor": "none", "searchForMissingEpisodes": False},
        }
        try:  # Sonarr v3 also wants a language profile; v4 dropped it
            languages = self._get_json("/api/v3/languageprofile")
            if languages:
                body["languageProfileId"] = languages[0]["id"]
        except ConnectorError:
            pass
        return body

    url_placeholder = "http://192.168.1.10:8989"

    @staticmethod
    def _series_item(s: dict, root: str | None) -> ItemRecord:
        has_files = ((s.get("statistics") or {}).get("episodeFileCount") or 0) > 0
        return ItemRecord(
            external_id=f"series-{s['id']}",
            kind="series",
            title=s.get("title") or "",
            library_external_id=root,
            year=s.get("year") or None,
            tvdb=_str_id(s.get("tvdbId")),
            tmdb=_str_id(s.get("tmdbId")),
            imdb=s.get("imdbId") or None,
            path=s.get("path"),
            source_status="has_files" if has_files else "no_files",
        )

    def episodes(self, external_id: str) -> list[dict]:
        """GET /api/v3/episode: the show's episodes in episode order, specials last, each as
        {id, season, episode, title, has_file}."""
        sid = external_id.rsplit("-", 1)[-1]
        eps = [
            {
                "id": e["id"],
                "season": e.get("seasonNumber") or 0,
                "episode": e.get("episodeNumber") or 0,
                "title": e.get("title") or "",
                "has_file": bool(e.get("hasFile")),
            }
            for e in self._get_json("/api/v3/episode", seriesId=sid)
        ]
        return sorted(eps, key=lambda e: (e["season"] == 0, e["season"], e["episode"]))

    def import_candidate(self, external_id: str, path: str) -> dict:
        """GET /api/v3/manualimport: how Sonarr sees one file it has not imported (its
        quality, languages and any reason it would refuse it), as its Manual Import screen."""
        sid = external_id.rsplit("-", 1)[-1]
        folder = path.rsplit("/", 1)[0]
        for found in self._get_json("/api/v3/manualimport", folder=folder, seriesId=sid):
            if found.get("path") == path:
                return found
        raise ConnectorError(f"{self.display_name} does not offer {path} for import")

    def import_call(
        self, external_id: str, path: str, episode_id: int, found: dict
    ) -> tuple[str, str, Any]:
        """POST /api/v3/command ManualImport, moving the file: Sonarr puts it in the episode's
        season folder under its own naming format and records it."""
        entry = {
            "path": path,
            "seriesId": int(external_id.rsplit("-", 1)[-1]),
            "episodeIds": [episode_id],
            "quality": found.get("quality"),
            "languages": found.get("languages") or [],
            "releaseGroup": found.get("releaseGroup") or "",
            "indexerFlags": found.get("indexerFlags") or 0,
            "releaseType": found.get("releaseType") or "singleEpisode",
        }
        body = {"name": "ManualImport", "importMode": "move", "files": [entry]}
        return ("POST", "/api/v3/command", body)

    def import_episode(self, external_id: str, path: str, episode_id: int, found: dict) -> str:
        """Send the ManualImport command. A write: Sonarr moves and renames the file. Returns
        the command's id (see task_done)."""
        _method, api_path, body = self.import_call(external_id, path, episode_id, found)
        try:
            resp = self._client.post(
                f"{self.base_url}{api_path}", headers=self._auth_headers(), json=body, timeout=60
            )
        except Exception as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if resp.status_code not in (200, 201):
            raise ConnectorError(f"POST {api_path}: {describe_http_failure(resp)}")
        token = self.task_token(resp)
        if not token:
            raise ConnectorError(f"{self.display_name} gave no command id for the import")
        return token

    def rename_episode(self, external_id: str, episode_id: int) -> tuple[str, str] | None:
        """Ask Sonarr to rename the episode's file to its naming format, which also moves it
        into its season folder (a file imported from inside the show's folder stays where it
        was). Only this one file is named in the command, never the rest of the show (whose
        names may be stale after the episode list changed). GET /api/v3/rename first: None
        when the file already has its proper name, else (the RenameFiles command's id, the
        new path in the show's folder). A write."""
        sid = int(external_id.rsplit("-", 1)[-1])
        file_id = self._get_json(f"/api/v3/episode/{episode_id}").get("episodeFileId")
        if not file_id:
            raise ConnectorError(f"{self.display_name} has no file for that episode")
        pending = self._get_json("/api/v3/rename", seriesId=sid)
        mine = next((r for r in pending if r.get("episodeFileId") == file_id), None)
        if mine is None:
            return None
        body = {"name": "RenameFiles", "seriesId": sid, "files": [file_id]}
        try:
            resp = self._client.post(
                f"{self.base_url}/api/v3/command",
                headers=self._auth_headers(),
                json=body,
                timeout=60,
            )
        except Exception as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if resp.status_code not in (200, 201):
            raise ConnectorError(f"POST /api/v3/command: {describe_http_failure(resp)}")
        return self.task_token(resp), mine.get("newPath") or ""

    def fetch_title(self, external_id, kind, library_external_id) -> TitleSnapshot:
        """GET /api/v3/series/{id}, and its files when it has any."""
        roots = self.libraries()
        try:
            s = self._get_json(f"/api/v3/series/{external_id.rsplit('-', 1)[-1]}")
        except ConnectorError as exc:
            if "404" in str(exc):
                raise ItemGone(str(exc)) from exc
            raise
        rec = self._series_item(s, _root_for(s.get("path"), roots) or library_external_id)
        files = self._series_files(s) if rec.source_status == "has_files" else []
        for f in files:
            f.library_external_id = _root_for(f.path, roots) or rec.library_external_id
        return TitleSnapshot(rec.external_id, [rec], files)

    def _series_files(self, series: dict) -> list[FileRecord]:
        sid = series["id"]
        files = self._get_json("/api/v3/episodefile", seriesId=sid)
        if not files:
            return []
        # The first episode on each file gives its S/E (multi-episode files list several).
        first_ep: dict[int, tuple[int, int]] = {}
        for ep in self._get_json("/api/v3/episode", seriesId=sid):
            fid = ep.get("episodeFileId")
            if fid and fid not in first_ep:
                first_ep[fid] = (ep.get("seasonNumber"), ep.get("episodeNumber"))
        out = []
        for f in files:
            season, episode = first_ep.get(f["id"], (f.get("seasonNumber"), None))
            out.append(
                FileRecord(
                    path=f["path"],
                    size=f.get("size"),
                    quality=((f.get("quality") or {}).get("quality") or {}).get("name"),
                    item_external_id=f"series-{sid}",
                    season=season,
                    episode=episode,
                )
            )
        return out

    def fetch_full(self, progress: ProgressFn = _no_progress, previous=None) -> Snapshot:
        roots = self.libraries()
        progress("Reading series")
        series = self._get_json("/api/v3/series")
        items, files = [], []
        with_files = []
        for s in series:
            root = _root_for(s.get("path"), roots)
            rec = self._series_item(s, root)
            items.append(rec)
            has_files = rec.source_status == "has_files"
            if has_files:
                with_files.append((s, root))
        done = 0
        with ThreadPoolExecutor(WORKERS) as pool:
            for (_s, root), recs in zip(
                with_files, pool.map(lambda sr: self._series_files(sr[0]), with_files), strict=True
            ):
                for rec in recs:
                    rec.library_external_id = _root_for(rec.path, roots) or root
                files.extend(recs)
                done += 1
                if done % 50 == 0:
                    progress(f"Read files for {done} of {len(with_files)} series")
        return Snapshot(libraries=roots, items=items, files=files)


@register
class RadarrConnector(_ArrConnector):
    type = "radarr"
    display_name = "Radarr"
    lookup_path = "/api/v3/movie/lookup"
    add_path = "/api/v3/movie"
    capabilities = _ArrConnector.capabilities | {
        Capability.SCAN,
        Capability.LOOKUP,
        Capability.ADD,
    }

    rescan_all = "RescanMovie"

    def rescan_body(self, item_id: str) -> dict[str, Any]:
        # RescanMovie takes one movieId; Radarr ignores "movieIds" and rescans every film.
        return {"name": "RescanMovie", "movieId": int(item_id.rsplit("-", 1)[-1])}

    def add_body(self, candidate: MatchCandidate, folder: str) -> dict[str, Any]:
        return {
            **candidate.payload,
            "qualityProfileId": self._profile(),
            "rootFolderPath": self._root_for_folder(folder),
            "path": folder,
            "monitored": False,
            "addOptions": {"monitor": "movieOnly", "searchForMovie": False},
        }

    url_placeholder = "http://192.168.1.10:7878"
    root_kind = "movie"

    def fetch_full(self, progress: ProgressFn = _no_progress, previous=None) -> Snapshot:
        roots = self.libraries()
        progress("Reading movies")
        items, files = [], []
        for m in self._get_json("/api/v3/movie"):
            rec, found = self._movie(m, roots)
            items.append(rec)
            files.extend(found)
        return Snapshot(libraries=roots, items=items, files=files)

    @staticmethod
    def _movie(m: dict, roots) -> tuple[ItemRecord, list[FileRecord]]:
        root = _root_for(m.get("path"), roots)
        mf = m.get("movieFile") if m.get("hasFile") else None
        rec = ItemRecord(
            external_id=f"movie-{m['id']}",
            kind="movie",
            title=m.get("title") or "",
            library_external_id=root,
            year=m.get("year") or None,
            tmdb=_str_id(m.get("tmdbId")),
            imdb=m.get("imdbId") or None,
            path=m.get("path"),
            source_status="has_files" if mf else "no_files",
        )
        files = []
        if mf and mf.get("path"):
            files.append(
                FileRecord(
                    path=mf["path"],
                    library_external_id=_root_for(mf["path"], roots) or root,
                    size=mf.get("size"),
                    quality=((mf.get("quality") or {}).get("quality") or {}).get("name"),
                    item_external_id=rec.external_id,
                )
            )
        return rec, files

    def fetch_title(self, external_id, kind, library_external_id) -> TitleSnapshot:
        """GET /api/v3/movie/{id}."""
        roots = self.libraries()
        try:
            m = self._get_json(f"/api/v3/movie/{external_id.rsplit('-', 1)[-1]}")
        except ConnectorError as exc:
            if "404" in str(exc):
                raise ItemGone(str(exc)) from exc
            raise
        rec, files = self._movie(m, roots)
        return TitleSnapshot(rec.external_id, [rec], files)


@register
class SportarrConnector(_ArrConnector):
    """Uses Sportarr's native API, not its /api/v3 Sonarr shim (which invents
    tvdbIds from TheSportsDB IDs). Leagues are items; events have no IDs."""

    type = "sportarr"
    display_name = "Sportarr"
    capabilities = _ArrConnector.capabilities | {Capability.SCAN}

    def web_link(self, external_id: str) -> str | None:
        return None

    url_placeholder = "http://192.168.1.10:1867"
    status_path = "/api/system/status"
    rootfolder_path = "/api/rootfolder"
    group_hint = "sport"

    def task_call(self, task: Task) -> tuple[str, str, Any]:
        """POST /api/task/scheduled/disk-scan/trigger: Sportarr has no rescan of one league, so
        it checks every tracked file and looks for untracked ones. It gives the scan no id."""
        return ("POST", "/api/task/scheduled/disk-scan/trigger", None)

    def scan_all_calls(self, library_ids: list[str]) -> list[tuple[str, str, Any]]:
        return [self.task_call(Task("scan"))]

    def _league(self, league: dict, roots) -> tuple[ItemRecord, list[FileRecord]]:
        """GET /api/leagues/{id}/files: one league and the files it holds."""
        body = self._get_json(f"/api/leagues/{league['id']}/files")
        league_files = body.get("files") or []
        rec = ItemRecord(
            external_id=f"league-{league['id']}",
            kind="series",
            title=league.get("name") or "",
            source_status="has_files" if league_files else "no_files",
        )
        files = []
        for f in league_files:
            path = f.get("filePath")
            if not path:
                continue
            m = SXXEXX_RE.search(path.rsplit("/", 1)[-1])
            files.append(
                FileRecord(
                    path=path,
                    library_external_id=_root_for(path, roots),
                    size=f.get("size"),
                    quality=f.get("quality"),
                    item_external_id=rec.external_id,
                    season=int(m.group(1)) if m else None,
                    episode=int(m.group(2)) if m else None,
                )
            )
        rec.library_external_id = roots[0].external_id if len(roots) == 1 else None
        return rec, files

    def fetch_full(self, progress: ProgressFn = _no_progress, previous=None) -> Snapshot:
        roots = self.libraries()
        items, files = [], []
        leagues = self._get_json("/api/leagues")
        for i, league in enumerate(leagues, start=1):
            progress(f"Reading league {i} of {len(leagues)}")
            rec, league_files = self._league(league, roots)
            items.append(rec)
            files.extend(league_files)
        return Snapshot(libraries=roots, items=items, files=files)

    def fetch_title(self, external_id, kind, library_external_id) -> TitleSnapshot:
        """GET /api/leagues/{id}, then its files."""
        roots = self.libraries()
        try:
            league = self._get_json(f"/api/leagues/{external_id.rsplit('-', 1)[-1]}")
        except ConnectorError as exc:
            if "404" in str(exc):
                raise ItemGone(str(exc)) from exc
            raise
        rec, files = self._league(league, roots)
        return TitleSnapshot(rec.external_id, [rec], files)
