"""Jellyfin (10.11+).

10.11 removed /Users/{id}/Items; items come from /Items with paging. Episodes
with LocationType "Virtual" are placeholders for missing episodes, not files,
so they are skipped.
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx

from .. import __version__
from .base import (
    PLURAL,
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
    UserRecord,
    WatchRecord,
    _no_progress,
    describe_http_failure,
    parse_time,
    register,
)

PAGE_SIZE = 1000
# Paging needs a fixed order: unsorted, Jellyfin's pages can overlap and skip items (a
# 1,500-film library read as 1,000). DateCreated breaks ties between equal names.
PAGE_ORDER = {"SortBy": "SortName,ProductionYear,DateCreated", "SortOrder": "Ascending"}
LIBRARY_KINDS = {"tvshows": "series", "movies": "movie"}
ITEM_KINDS = {"Series": "series", "Movie": "movie", "Episode": "episode"}


def _ids(provider_ids: dict[str, Any] | None) -> dict[str, str | None]:
    # Jellyfin's keys are capitalised and occasionally vary in case.
    lowered = {k.lower(): str(v) for k, v in (provider_ids or {}).items() if v}
    return {"tvdb": lowered.get("tvdb"), "tmdb": lowered.get("tmdb"), "imdb": lowered.get("imdb")}


def item_record(raw: dict[str, Any], library_id: str) -> ItemRecord | None:
    kind = ITEM_KINDS.get(raw.get("Type", ""))
    if kind is None or raw.get("LocationType") == "Virtual":
        return None
    rec = ItemRecord(
        external_id=raw["Id"],
        kind=kind,
        title=raw.get("Name") or "",
        library_external_id=library_id,
        year=raw.get("ProductionYear"),
        path=raw.get("Path"),
        **_ids(raw.get("ProviderIds")),
    )
    if kind == "episode":
        rec.parent_external_id = raw.get("SeriesId")
        rec.season = raw.get("ParentIndexNumber")
        rec.episode = raw.get("IndexNumber")
        rec.episode_end = raw.get("IndexNumberEnd")
    return rec


@register
class JellyfinConnector(Connector):
    type = "jellyfin"
    display_name = "Jellyfin"
    capabilities = frozenset(
        {
            Capability.LIST_ITEMS,
            Capability.LIST_FILES,
            Capability.LIST_USERS,
            Capability.READ_WATCH_STATE,
            Capability.WRITE_WATCH_STATE,
            Capability.CREATE_USER,
            Capability.REMATCH,
            Capability.SCAN,
            Capability.REFRESH,
        }
    )
    url_placeholder = "http://192.168.1.10:8096"
    key_help = "Jellyfin Dashboard → API Keys → +. Name it MediaWrangler."

    can_forget = True

    def web_link(self, external_id: str) -> str | None:
        return f"{self.base_url}/web/#/details?id={external_id}"

    def forget_folder(self, path: str) -> None:
        """POST /Library/Media/Updated with the folder as "Deleted": what Jellyfin's own file
        watcher reports. After its LibraryMonitorDelay (60 s by default) it checks the parent
        and drops the show whose folder is gone (measured: about 70 s). Refreshing the show
        itself does not drop it."""
        body = {"Updates": [{"Path": path, "UpdateType": "Deleted"}]}
        try:
            resp = self._client.post(
                f"{self.base_url}/Library/Media/Updated",
                headers=self._auth_headers(),
                json=body,
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if resp.status_code not in (200, 204):
            raise ConnectorError(f"POST /Library/Media/Updated: {describe_http_failure(resp)}")

    def _auth_headers(self) -> dict[str, str]:
        # The MediaBrowser scheme is the current form; X-Emby-Token is legacy.
        return {
            "Authorization": (
                f'MediaBrowser Client="MediaWrangler", Device="MediaWrangler", '
                f'DeviceId="mediawrangler", Version="{__version__}", '
                f'Token="{self.config.secret or ""}"'
            )
        }

    def _get_json(self, path: str, **params) -> Any:
        resp = self._get(path, headers=self._auth_headers(), params=params, timeout=120)
        if resp.status_code != 200:
            raise ConnectorError(f"GET {path}: {describe_http_failure(resp)}")
        return resp.json()

    def set_played(
        self, user: UserRecord, item_id: str, played: bool, when: datetime | None = None
    ) -> None:
        params = {"userId": user.external_id}
        if played and when is not None:  # keep the original date, not "now"
            params["datePlayed"] = when.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        path = f"/UserPlayedItems/{item_id}"
        try:
            resp = self._client.request(
                "POST" if played else "DELETE",
                f"{self.base_url}{path}",
                headers=self._auth_headers(),
                params=params,
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        verb = "POST" if played else "DELETE"
        if resp.status_code == 404:
            raise ItemGone(f"{verb} {path}: Jellyfin does not know this item")
        if resp.status_code not in (200, 204):
            raise ConnectorError(f"{verb} {path}: {describe_http_failure(resp)}")

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = self._client.request(
                method, f"{self.base_url}{path}", headers=self._auth_headers(), timeout=60, **kwargs
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if resp.status_code not in (200, 204):
            raise ConnectorError(f"{method} {path}: {describe_http_failure(resp)}")
        return resp

    def search_matches(
        self, item_id: str, kind: str, title: str, year: int | None
    ) -> list[MatchCandidate]:
        """POST /Items/RemoteSearch/Series (or /Movie): Jellyfin's own "Identify" search."""
        info: dict[str, Any] = {"Name": title}
        if year:
            info["Year"] = year
        body = {"SearchInfo": info, "ItemId": item_id, "IncludeDisabledProviders": False}
        path = "/Items/RemoteSearch/" + ("Movie" if kind == "movie" else "Series")
        out = []
        for raw in self._send("POST", path, json=body).json() or []:
            ids = {k.lower(): str(v) for k, v in (raw.get("ProviderIds") or {}).items() if v}
            premiere = str(raw.get("PremiereDate") or "")[:4]
            year = raw.get("ProductionYear") or (int(premiere) if premiere.isdigit() else None)
            out.append(
                MatchCandidate(
                    title=raw.get("Name") or "",
                    year=raw.get("ProductionYear")
                    or (int(premiere) if premiere.isdigit() else None),
                    ids=ids,
                    overview=raw.get("Overview") or "",
                    image_url=raw.get("ImageUrl") or "",
                    note=raw.get("SearchProviderName") or "",
                    payload=raw,
                )
            )
        return out

    def task_call(self, task: Task) -> tuple[str, str, Any]:
        """Scan: with the folder's own entry as item_id, POST /Items/{id}/Refresh looks at that
        folder at once; otherwise POST /Library/Media/Updated (after the file watcher's delay),
        or POST /Library/Refresh for every library. Refresh: POST /Items/{id}/Refresh downloads
        the item's details again."""
        if task.kind == "scan" and task.item_id:
            # The folder's own entry, looked at now: skips the file watcher's delay
            # (LibraryMonitorDelay, 60 s by default) that /Library/Media/Updated waits out.
            query = (
                "Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=None"
                "&ReplaceAllMetadata=false&ReplaceAllImages=false"
            )
            return ("POST", f"/Items/{task.item_id}/Refresh?{query}", None)
        if task.kind == "refresh":
            query = (
                "Recursive=true&MetadataRefreshMode=FullRefresh&ImageRefreshMode=FullRefresh"
                "&ReplaceAllMetadata=false&ReplaceAllImages=false"
            )
            return ("POST", f"/Items/{task.item_id}/Refresh?{query}", None)
        if task.path:
            body = {"Updates": [{"Path": task.path, "UpdateType": "Created"}]}
            return ("POST", "/Library/Media/Updated", body)
        return ("POST", "/Library/Refresh", None)

    def _library_task(self) -> dict[str, Any]:
        tasks = self._get_json("/ScheduledTasks")
        return next((t for t in tasks if t.get("Key") == "RefreshLibrary"), {})

    def scan_all_calls(self, library_ids: list[str]) -> list[tuple[str, str, Any]]:
        return [("POST", "/Library/Refresh", None)]

    def scan_all(self, library_ids: list[str]) -> list[str]:
        """POST /Library/Refresh: Jellyfin's "Scan All Libraries" task. It gives no id, so the
        token is when the task last started; it is done once it has started again and is idle."""
        before = (self._library_task().get("LastExecutionResult") or {}).get("StartTimeUtc")
        self._request(*self.scan_all_calls(library_ids)[0])
        return [f"RefreshLibrary@{before or ''}"]

    def scan_all_done(self, token: str) -> bool:
        before = token.partition("@")[2]
        task = self._library_task()
        started = (task.get("LastExecutionResult") or {}).get("StartTimeUtc") or ""
        return task.get("State") == "Idle" and started != before

    def match_call(self, item_id: str, candidate: MatchCandidate) -> tuple[str, str, Any]:
        return (
            "POST",
            f"/Items/RemoteSearch/Apply/{item_id}?replaceAllImages=true",
            candidate.payload,
        )

    def apply_match(self, item_id: str, candidate: MatchCandidate) -> str:
        """POST /Items/RemoteSearch/Apply/{id}: Jellyfin sets the IDs and refreshes the
        item's metadata. The item keeps its id."""
        try:
            resp = self._client.post(
                f"{self.base_url}/Items/RemoteSearch/Apply/{item_id}",
                headers=self._auth_headers(),
                params={"replaceAllImages": "true"},
                json=candidate.payload,
                timeout=120,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        path = f"/Items/RemoteSearch/Apply/{item_id}"
        if resp.status_code == 404:
            raise ItemGone(f"POST {path}: Jellyfin does not know this item")
        if resp.status_code not in (200, 204):
            raise ConnectorError(f"POST {path}: {describe_http_failure(resp)}")
        return item_id

    def create_user(self, name: str, password: str, library_ids: list[str] | None = None) -> str:
        """POST /Users/New, then limit the libraries it may open (POST /Users/{id}/Policy
        with the policy Jellyfin gave it, changed only in EnableAllFolders/EnabledFolders)."""
        user_id = self._send(
            "POST", "/Users/New", json={"Name": name, "Password": password}
        ).json()["Id"]
        if library_ids is not None:
            policy = self._get_json(f"/Users/{user_id}").get("Policy") or {}
            policy["EnableAllFolders"] = False
            policy["EnabledFolders"] = list(library_ids)
            self._send("POST", f"/Users/{user_id}/Policy", json=policy)
        return str(user_id)

    def test_connection(self) -> ConnectionTest:
        try:
            public = self._get("/System/Info/Public")
            if public.status_code != 200:
                return ConnectionTest(False, describe_http_failure(public))
            version = public.json().get("Version")
            info = self._get("/System/Info", headers=self._auth_headers())
        except (ConnectorError, ValueError) as exc:
            return ConnectionTest(False, str(exc))
        if info.status_code != 200:
            return ConnectionTest(False, describe_http_failure(info), version)
        name = info.json().get("ServerName", "Jellyfin")
        return ConnectionTest(True, f"Connected to {name}", version)

    def libraries(self) -> list[LibraryRecord]:
        return [
            LibraryRecord(
                external_id=v["ItemId"],
                name=v.get("Name") or v["ItemId"],
                kind=LIBRARY_KINDS.get(v.get("CollectionType") or "", "other"),
                paths=list(v.get("Locations") or []),
            )
            for v in self._get_json("/Library/VirtualFolders")
        ]

    def _as_admin(self) -> dict[str, str]:
        """userId of an admin who sees every library, for reading a library by ParentId.
        Asked with the API key alone, Jellyfin can leave out items it filed under the
        library's folder instead of the library itself (one server: 674 of 1,176 films)."""
        if not hasattr(self, "_admin"):
            self._admin = next(
                (
                    u["Id"]
                    for u in self._get_json("/Users")
                    if (p := u.get("Policy") or {}).get("IsAdministrator")
                    and not p.get("IsDisabled")
                    and p.get("EnableAllFolders") is not False
                ),
                None,
            )
        return {"userId": self._admin} if self._admin else {}

    def _items(self, library_id: str, item_type: str) -> Iterator[dict[str, Any]]:
        start = 0
        while True:
            page = self._get_json(
                "/Items",
                **self._as_admin(),
                ParentId=library_id,
                Recursive="true",
                IncludeItemTypes=item_type,
                Fields="ProviderIds,Path,ProductionYear",
                EnableImages="false",
                EnableUserData="false",
                StartIndex=start,
                Limit=PAGE_SIZE,
                **PAGE_ORDER,
            )
            items = page.get("Items") or []
            yield from items
            start += len(items)
            if not items or start >= page.get("TotalRecordCount", 0):
                return

    def fetch_full(
        self, progress: ProgressFn = _no_progress, previous: dict | None = None
    ) -> Snapshot:
        libraries = self.libraries()
        items: list[ItemRecord] = []
        files: dict[str, FileRecord] = {}
        for lib in libraries:
            if lib.kind == "other":
                continue
            types = ("Series", "Episode") if lib.kind == "series" else ("Movie",)
            for item_type in types:
                progress(f"{lib.name}: reading {PLURAL[item_type.lower()]}")
                for raw in self._items(lib.external_id, item_type):
                    rec = item_record(raw, lib.external_id)
                    if rec is None:
                        continue
                    items.append(rec)
                    # Each episode/movie item is one file Jellyfin picked up
                    # (a multi-episode file is one item, so paths stay unique).
                    if rec.kind != "series" and rec.path and rec.path not in files:
                        files[rec.path] = FileRecord(
                            path=rec.path,
                            library_external_id=lib.external_id,
                            item_external_id=rec.parent_external_id or rec.external_id,
                            season=rec.season,
                            episode=rec.episode,
                        )
        return Snapshot(libraries=libraries, items=items, files=list(files.values()))

    FIELDS = "ProviderIds,Path,ProductionYear"

    def folder_entry(self, path: str, hints: list[str]) -> str | None:
        """The id of Jellyfin's own entry for a folder (a film's folder, or a show), found by
        walking up from any item known to be in it."""
        path = path.rstrip("/")
        for item in hints:
            for _ in range(5):
                found = (
                    self._get_json(
                        "/Items",
                        Ids=item,
                        Fields="ParentId,Path",
                        EnableImages="false",
                        EnableUserData="false",
                    ).get("Items")
                    or []
                )
                if not found:
                    break
                if (found[0].get("Path") or "").rstrip("/") == path:
                    return found[0]["Id"]
                item = found[0].get("ParentId")
                if not item:
                    break
        return None

    def titles_under(self, folder_id: str) -> set[str]:
        """The films or show Jellyfin now holds under a folder entry (one film per copy)."""
        me = (
            self._get_json(
                "/Items", Ids=folder_id, EnableImages="false", EnableUserData="false"
            ).get("Items")
            or []
        )
        if not me:
            return set()
        if me[0].get("Type") in ("Series", "Movie"):
            return {folder_id}
        kids = self._get_json(
            "/Items",
            ParentId=folder_id,
            Recursive="true",
            IncludeItemTypes="Movie,Series",
            EnableImages="false",
            EnableUserData="false",
        )
        return {k["Id"] for k in kids.get("Items") or []}

    def find_title(self, name, kind, library_external_id, path) -> str | None:
        """Search by name, then keep the one whose path is the folder (a film's Path is its
        file, which sits in the folder)."""
        found = self.find_titles(name, kind, library_external_id, path)
        return found[0] if found else None

    def find_titles(self, name, kind, library_external_id, path) -> list[str]:
        """Every item in the folder: Jellyfin keeps one per copy of a film."""
        params = {
            "searchTerm": name,
            "IncludeItemTypes": "Movie" if kind == "movie" else "Series",
            "Recursive": "true",
            "Fields": "Path",
            "EnableImages": "false",
            "EnableUserData": "false",
            "Limit": 50,
        }
        if library_external_id:
            params.update(self._as_admin(), ParentId=library_external_id)
        folder = path.rstrip("/")
        return [
            raw["Id"]
            for raw in self._get_json("/Items", **params).get("Items") or []
            if (found := (raw.get("Path") or "").rstrip("/")) == folder
            or found.startswith(folder + "/")
        ]

    def fetch_title(self, external_id, kind, library_external_id) -> TitleSnapshot:
        """One show (with its episodes) or film: GET /Items?Ids=, then the show's
        episodes by ParentId. Each episode or film item is one file Jellyfin picked up."""
        found = (
            self._get_json(
                "/Items",
                Ids=external_id,
                Fields=self.FIELDS,
                EnableImages="false",
                EnableUserData="false",
            ).get("Items")
            or []
        )
        rec = item_record(found[0], library_external_id or "") if found else None
        if rec is None:
            raise ItemGone(f"Jellyfin does not have item {external_id}")
        items, files = [rec], {}
        if rec.kind == "series":
            start = 0
            while True:
                page = self._get_json(
                    "/Items",
                    ParentId=external_id,
                    Recursive="true",
                    IncludeItemTypes="Episode",
                    Fields=self.FIELDS,
                    EnableImages="false",
                    EnableUserData="false",
                    StartIndex=start,
                    Limit=PAGE_SIZE,
                    **PAGE_ORDER,
                )
                rows = page.get("Items") or []
                for raw in rows:
                    ep = item_record(raw, library_external_id or "")
                    if ep is None:
                        continue
                    items.append(ep)
                    if ep.path and ep.path not in files:
                        files[ep.path] = FileRecord(
                            path=ep.path,
                            library_external_id=library_external_id,
                            item_external_id=ep.parent_external_id or ep.external_id,
                            season=ep.season,
                            episode=ep.episode,
                        )
                start += len(rows)
                if not rows or start >= page.get("TotalRecordCount", 0):
                    break
        elif rec.path:
            files[rec.path] = FileRecord(
                path=rec.path,
                library_external_id=library_external_id,
                item_external_id=rec.external_id,
            )
        return TitleSnapshot(rec.external_id, items, list(files.values()))

    checks_items = True
    EXIST_CHUNK = 100  # ids per request, to keep the URL short

    def items_exist(self, user: UserRecord, item_ids: list[str]) -> set[str]:
        """Asked as the user, so an item they can no longer open (a replaced file,
        a library taken away) is absent here too: neither is an unwatch."""
        found: set[str] = set()
        for i in range(0, len(item_ids), self.EXIST_CHUNK):
            chunk = item_ids[i : i + self.EXIST_CHUNK]
            page = self._get_json(
                "/Items",
                userId=user.external_id,
                ids=",".join(chunk),
                Recursive="true",
                EnableImages="false",
                EnableUserData="false",
                Limit=len(chunk),
            )
            found.update(item["Id"] for item in page.get("Items") or [])
        return found

    def users(self) -> list[UserRecord]:
        # An admin API key reads every user's data.
        out = []
        for u in self._get_json("/Users"):
            policy = u.get("Policy") or {}
            restricted = policy.get("EnableAllFolders") is False
            out.append(
                UserRecord(
                    external_id=u["Id"],
                    name=u["Name"],
                    library_ids=list(policy.get("EnabledFolders") or []) if restricted else None,
                )
            )
        return out

    def fetch_watch(
        self, user: UserRecord, progress: ProgressFn = _no_progress
    ) -> list[WatchRecord]:
        out: dict[str, WatchRecord] = {}
        for flt in ("IsPlayed", "IsResumable"):
            start = 0
            while True:
                page = self._get_json(
                    "/Items",
                    userId=user.external_id,
                    Recursive="true",
                    IncludeItemTypes="Episode,Movie",
                    Filters=flt,
                    EnableUserData="true",
                    EnableImages="false",
                    StartIndex=start,
                    Limit=PAGE_SIZE,
                    **PAGE_ORDER,
                )
                items = page.get("Items") or []
                for raw in items:
                    data = raw.get("UserData") or {}
                    ticks = data.get("PlaybackPositionTicks") or 0
                    out[raw["Id"]] = WatchRecord(
                        item_external_id=raw["Id"],
                        played=bool(data.get("Played")),
                        play_count=data.get("PlayCount"),
                        position_seconds=ticks / 10_000_000 if ticks else None,
                        duration_seconds=(raw.get("RunTimeTicks") or 0) / 10_000_000 or None,
                        last_played_at=parse_time(data.get("LastPlayedDate")),
                    )
                start += len(items)
                if not items or start >= page.get("TotalRecordCount", 0):
                    break
        return list(out.values())
