"""Silo, native API v2 only.

Never use Silo's Jellyfin-compat listener: it returns no episodes for unfiltered
enumeration and misreports admin keys. Admin calls are rate limited (HTTP 429).

Catalog reads need an X-Profile-Id header; the key owner's primary profile is
used. Catalog cards carry one provider ID inside `content_id` (e.g.
"series-tvdb-411959", "episode-tvdb-248742-3-17", or "local-..." when Silo
hasn't matched the item); the full ID set and paths come from the item detail,
which is fetched once per item and reused on later syncs.
"""

import logging
import re
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

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
    RootRecord,
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

log = logging.getLogger(__name__)
PAGE_SIZE = 200
DETAIL_WORKERS = 3
MAX_RETRIES = 5
DEFAULT_RETRY_AFTER = 20.0
LIBRARY_KINDS = {"series": "series", "movies": "movie"}
CONTENT_ID_RE = re.compile(r"^(series|movie|episode)-(tvdb|tmdb|imdb)-([^-]+)")


def ids_from_content_id(content_id: str) -> dict[str, str]:
    """The provider ID Silo built the content ID from, if any."""
    match = CONTENT_ID_RE.match(content_id)
    if not match:
        return {}
    _kind, provider, value = match.groups()
    if provider == "tvdb" and match.group(1) == "episode":
        # episode-tvdb-<series tvdb>-<season>-<episode>: the ID is the series'.
        return {}
    return {provider: value}


def external_id(library_id: str, content_id: str) -> str:
    """Silo reuses one content ID for a title that sits in two libraries (a film
    in both Movies and Movies 4K), so items are keyed per library."""
    return f"{library_id}/{content_id}"


def content_id_of(external: str) -> str:
    return external.split("/", 1)[1] if "/" in external else external


def card_record(card: dict[str, Any], library_id: str) -> ItemRecord | None:
    kind = card.get("type")
    if kind not in ("series", "movie", "episode"):
        return None
    rec = ItemRecord(
        external_id=external_id(library_id, card["content_id"]),
        kind=kind,
        title=card.get("title") or "",
        library_external_id=library_id,
        year=card.get("year") or None,
        source_status=card.get("status") or None,
        **ids_from_content_id(card["content_id"]),
    )
    if kind == "episode":
        series_id = card.get("series_id")
        rec.parent_external_id = external_id(library_id, series_id) if series_id else None
        rec.season = card.get("season_number")
        rec.episode = card.get("episode_number")
        rec.episode_end = card.get("episode_number_end")
    return rec


def apply_detail(rec: ItemRecord, detail: dict[str, Any]) -> None:
    for id_type in ("tvdb", "tmdb", "imdb"):
        value = detail.get(f"{id_type}_id")
        if value:
            setattr(rec, id_type, str(value))
    folders = detail.get("folder_paths") or []
    versions = detail.get("versions") or []
    if folders:
        rec.path = folders[0]
    elif versions and versions[0].get("file_path"):
        rec.path = versions[0]["file_path"]
    if rec.kind == "movie":
        rec.file_paths = [v["file_path"] for v in versions if v.get("file_path")]
    rec.details_fetched = True


@register
class SiloConnector(Connector):
    type = "silo"
    display_name = "Silo"
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
            Capability.OVERRIDE,
        }
    )
    url_placeholder = "http://192.168.1.10:8090"
    key_help = (
        "Create an API key for an admin user in Silo. Use the native port "
        "(8090 by default), not the Jellyfin-compat one."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._profile_id: str | None = None
        self._throttle = threading.Lock()
        # Keys made for other login accounts (account id → key), so their profiles
        # can be read: Silo refuses another account's profile to any other key.
        self.account_keys: dict[str, str] = {}

    def web_link(self, external_id: str) -> str | None:
        """/item/{content id}?libraryId=, as Silo's web app links its titles. Our id for a
        title is "<library id>/<content id>"."""
        library, _, content = external_id.partition("/")
        if not content:
            return None
        return f"{self.base_url}/item/{quote(content, safe='')}?libraryId={library}"

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.config.secret or ''}"}
        if self._profile_id:
            headers["X-Profile-Id"] = self._profile_id
        return headers

    def _get_json(self, path: str, **params) -> Any:
        return self._get_json_as(self._auth_headers(), path, **params)

    def _get_json_as(self, headers: dict[str, str], path: str, **params) -> Any:
        """GET with backoff on 429. Retries share one lock so parallel workers
        all pause together instead of hammering a limited endpoint."""
        for _attempt in range(MAX_RETRIES):
            with self._throttle:
                pass
            resp = self._get(path, headers=headers, params=params, timeout=60)
            if resp.status_code == 429:
                wait = _retry_after(resp)
                with self._throttle:
                    time.sleep(wait)
                continue
            if resp.status_code != 200:
                raise ConnectorError(f"GET {path}: {describe_http_failure(resp)}")
            return resp.json()
        raise ConnectorError(f"GET {path}: still rate limited after {MAX_RETRIES} attempts")

    def _is_jellyfin_compat(self) -> bool:
        """The compat listener answers Jellyfin's public info endpoint."""
        try:
            resp = self._get("/System/Info/Public")
            return resp.status_code == 200 and resp.json().get("ProductName") == "Jellyfin Server"
        except (ConnectorError, ValueError):
            return False

    def test_connection(self) -> ConnectionTest:
        try:
            info = self._get("/api/v2/system/info")
            if info.status_code == 404 and self._is_jellyfin_compat():
                return ConnectionTest(
                    False,
                    "This is Silo's Jellyfin-compat port. Use the native API port "
                    "instead (8090 by default).",
                )
            if info.status_code != 200:
                return ConnectionTest(False, describe_http_failure(info))
            body = info.json()
            version = body.get("version") or body.get("server_version")
            me = self._get("/api/v2/account/me", headers=self._auth_headers())
        except (ConnectorError, ValueError) as exc:
            return ConnectionTest(False, str(exc))
        if me.status_code != 200:
            return ConnectionTest(False, describe_http_failure(me), version)
        account = me.json()
        who = account.get("username") or account.get("name") or "account"
        return ConnectionTest(True, f"Connected as {who}", version)

    def _ensure_profile(self) -> None:
        if self._profile_id:
            return
        profiles = self._get_json("/api/v2/profiles").get("items") or []
        if not profiles:
            raise ConnectorError("the key's account has no Silo profile")
        primary = next((p for p in profiles if p.get("is_primary")), profiles[0])
        self._profile_id = str(primary["id"])

    def libraries(self) -> list[LibraryRecord]:
        body = self._get_json("/api/v2/libraries")
        rows = body.get("items", body) if isinstance(body, dict) else body
        return [
            LibraryRecord(
                external_id=str(lib["id"]),
                name=lib.get("name") or str(lib["id"]),
                kind=LIBRARY_KINDS.get(lib.get("type") or "", "other"),
                paths=list(lib.get("paths") or []),
            )
            for lib in rows
            if lib.get("enabled", True)
        ]

    def _catalog(self, library_id: str, item_type: str) -> Iterator[dict[str, Any]]:
        return self._paged("/api/v2/catalog", library_id=library_id, type=item_type)

    def _paged(self, path: str, **params) -> Iterator[dict[str, Any]]:
        return self._paged_as(None, path, **params)

    def _paged_as(
        self, headers: dict[str, str] | None, path: str, **params
    ) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            q = {"limit": PAGE_SIZE, **params}
            if cursor:
                q["cursor"] = cursor
            page = (
                self._get_json(path, **q)
                if headers is None
                else self._get_json_as(headers, path, **q)
            )
            yield from page.get("items") or []
            info = page.get("page") or {}
            cursor = info.get("next_cursor")
            if not info.get("has_more") or not cursor:
                return

    def roots(self, libraries: list[LibraryRecord]) -> list[RootRecord]:
        """Folders Silo scanned (per library) and the ones it skipped."""
        out = []
        for lib in libraries:
            if lib.kind == "other":
                continue
            for r in self._paged("/api/v2/libraries/roots", library_id=lib.external_id):
                cid = r.get("content_id")
                out.append(
                    RootRecord(
                        path=r["root_path"],
                        state=r.get("state") or "unknown",
                        library_external_id=lib.external_id,
                        file_count=r.get("observed_file_count"),
                        item_external_id=external_id(lib.external_id, cid) if cid else None,
                    )
                )
        wanted = {lib.external_id for lib in libraries if lib.kind != "other"}
        for r in self._paged("/api/v2/libraries/skipped-roots"):
            lib_id = str(r.get("library_id") or "")
            if lib_id not in wanted:
                continue
            out.append(
                RootRecord(
                    path=r["root_path"],
                    state="skipped",
                    reason=r.get("reason"),
                    library_external_id=lib_id,
                    file_count=r.get("file_count"),
                )
            )
        return out

    def _detail(self, rec: ItemRecord) -> None:
        detail = self._get_json(
            f"/api/v2/catalog/items/{content_id_of(rec.external_id)}",
            library_id=rec.library_external_id,
        )
        apply_detail(rec, detail)

    def fetch_full(
        self,
        progress: ProgressFn = _no_progress,
        previous: dict[str, ItemRecord] | None = None,
    ) -> Snapshot:
        previous = previous or {}
        self._ensure_profile()
        libraries = self.libraries()
        items: list[ItemRecord] = []
        for lib in libraries:
            if lib.kind == "other":
                continue
            types = ("series", "episode") if lib.kind == "series" else ("movie",)
            for item_type in types:
                progress(f"{lib.name}: reading {PLURAL[item_type]}")
                for card in self._catalog(lib.external_id, item_type):
                    rec = card_record(card, lib.external_id)
                    if rec is not None:
                        items.append(rec)

        # Details (all provider IDs, paths) for series and movies not seen before.
        need = []
        for rec in items:
            if rec.kind == "episode":
                continue
            old = previous.get(rec.external_id)
            # A film's version files are read afresh every time, like a series' files: a file
            # renamed or replaced on disk would otherwise stay on record for ever.
            if old is not None and old.details_fetched and rec.kind != "movie":
                for field_name in ("tvdb", "tmdb", "imdb", "path"):
                    if getattr(rec, field_name) is None:
                        setattr(rec, field_name, getattr(old, field_name))
            else:
                need.append(rec)
        if need:
            done = 0
            progress(f"Reading details for {len(need)} items")
            with ThreadPoolExecutor(DETAIL_WORKERS) as pool:
                for _ in pool.map(self._detail, need):
                    done += 1
                    if done % 100 == 0:
                        progress(f"Read details for {done} of {len(need)} items")
        # A title in two libraries lists every version under each library's
        # item (a film's 1080p and 4K copies), so each file belongs to the
        # library whose folder holds it.
        roots = self._library_roots(libraries)
        files = []
        for rec in items:
            for p in rec.file_paths:
                lib_id = self._owning_library(roots, p)
                files.append(
                    FileRecord(
                        path=p,
                        library_external_id=lib_id or rec.library_external_id,
                        item_external_id=rec.external_id,
                    )
                )
        series = [rec for rec in items if rec.kind == "series"]
        if series:
            progress(f"Reading the files of {len(series)} series")
            done = 0
            with ThreadPoolExecutor(DETAIL_WORKERS) as pool:
                for found in pool.map(self._series_files, series):
                    files.extend(found)
                    done += 1
                    if done % 100 == 0:
                        progress(f"Read the files of {done} of {len(series)} series")
        progress("Reading scanned folders")
        return Snapshot(libraries=libraries, items=items, files=files, roots=self.roots(libraries))

    @staticmethod
    def _library_roots(libraries: list[LibraryRecord]) -> list[tuple[str, str]]:
        return sorted(
            ((p.rstrip("/") + "/", lib.external_id) for lib in libraries for p in lib.paths),
            key=lambda r: len(r[0]),
            reverse=True,
        )

    @staticmethod
    def _owning_library(roots: list[tuple[str, str]], path: str) -> str | None:
        return next((lid for prefix, lid in roots if path.startswith(prefix)), None)

    def override_call(self, library_id, root_path, candidate, kind) -> tuple[str, str, Any]:
        """PUT /api/v2/libraries/roots/override: Silo then treats the folder as this title."""
        body: dict[str, Any] = {
            "library_id": library_id,
            "root_path": root_path,
            "forced_type": kind,
            "forced_title": candidate.title,
            "note": "Set from MediaWrangler",
        }
        if candidate.year:
            body["forced_year"] = candidate.year
        for ident in ("tvdb", "tmdb", "imdb"):
            if candidate.ids.get(ident):
                body[f"forced_{ident}_id"] = candidate.ids[ident]
        return ("PUT", "/api/v2/libraries/roots/override", body)

    def set_override(self, library_id, root_path, candidate, kind) -> None:
        method, path, body = self.override_call(library_id, root_path, candidate, kind)
        try:
            resp = self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._auth_headers(),
                json=body,
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if resp.status_code not in (200, 204):
            raise ConnectorError(f"{method} {path}: {describe_http_failure(resp)}")

    def task_token(self, resp: httpx.Response) -> str | None:
        """A metadata refresh answers with a job; a scan answers "accepted" with no id."""
        try:
            return str(resp.json()["id"])
        except (ValueError, KeyError, TypeError):
            return None

    def find_title(self, name, kind, library_external_id, path) -> str | None:
        """Search the library by name, then keep the one whose folder is this one."""
        self._ensure_profile()  # the catalog refuses a request with no profile
        folder = path.rstrip("/")
        library = library_external_id or ""
        for n, card in enumerate(
            self._paged("/api/v2/catalog", library_id=library, type=kind, q=name)
        ):
            if n >= 20:
                break
            detail = self._get_json(
                f"/api/v2/catalog/items/{card['content_id']}", library_id=library
            )
            found = list(detail.get("folder_paths") or [])
            found += [v["file_path"] for v in detail.get("versions") or [] if v.get("file_path")]
            if any(p.rstrip("/") == folder or p.startswith(folder + "/") for p in found):
                return external_id(library, card["content_id"])
        return None

    def task_done(self, token: str) -> bool:
        """GET /api/v2/admin/jobs/{id}: done once the job is terminal."""
        job = self._get_json(f"/api/v2/admin/jobs/{token}")
        if job.get("failure"):
            raise ConnectorError(f"Silo's job failed: {job['failure']}")
        return bool(job.get("terminal"))

    def fetch_title(self, item_id, kind, library_external_id) -> TitleSnapshot:
        """One show or film: its detail, a show's seasons and episodes, and its files."""
        content, library = self._content_and_library(item_id)
        library = library_external_id or library
        self._ensure_profile()
        try:
            detail = self._get_json(f"/api/v2/catalog/items/{content}", library_id=library)
        except ConnectorError as exc:
            if "404" in str(exc):
                raise ItemGone(str(exc)) from exc
            raise
        rec = card_record({**detail, "content_id": content}, library)
        if rec is None:
            raise ItemGone(f"Silo has no show or film {content}")
        apply_detail(rec, detail)
        items, files = [rec], []
        if rec.kind == "series":
            seasons = self._get_json(
                f"/api/v2/catalog/series/{content}/seasons", library_id=library
            )
            for season in seasons.get("items") or []:
                number = season.get("season_number")
                if number is None:
                    continue
                eps = self._get_json(
                    f"/api/v2/catalog/series/{content}/seasons/{number}/episodes",
                    library_id=library,
                )
                for ep in eps.get("items") or []:
                    if not ep.get("content_id"):
                        continue
                    items.append(
                        ItemRecord(
                            external_id=external_id(library, ep["content_id"]),
                            kind="episode",
                            title=ep.get("title") or "",
                            library_external_id=library,
                            parent_external_id=rec.external_id,
                            season=ep.get("season_number", number),
                            episode=ep.get("episode_number"),
                            **ids_from_content_id(ep["content_id"]),
                        )
                    )
            files = self._series_files(rec)
        else:
            # A film in two libraries lists both copies under each item; each file belongs to
            # the library whose folder holds it, as in a full read.
            roots = self._library_roots(self.libraries())
            for path in rec.file_paths:
                files.append(
                    FileRecord(
                        path=path,
                        library_external_id=self._owning_library(roots, path) or library,
                        item_external_id=rec.external_id,
                    )
                )
        return TitleSnapshot(rec.external_id, items, files)

    def _series_files(self, rec: ItemRecord) -> list[FileRecord]:
        """The files Silo serves for a series: those attached to an episode, read from the
        seasons' episode lists. That is all Silo is compared on. The admin file list
        (GET /api/v2/admin/items/{id}/files) is used only to give those files their paths, the
        one place that has them; its other records (a file that was removed and never cleaned
        up, a second copy of an episode) are attached to no episode and are not Silo files, so
        they are dropped, not shown or counted. A series that cannot be read is left out (and
        logged); the next read tries again. If the episode lists cannot be read at all, the
        admin list is used as it stands, and that is logged."""
        content, _library = self._content_and_library(rec.external_id)
        try:
            rows = list(self._paged(f"/api/v2/admin/items/{content}/files", limit=200))
        except ConnectorError as exc:
            log.warning("could not read Silo's files for %s: %s", rec.title, exc)
            return []
        attached = self._attached_file_ids(rec, content)
        return [
            FileRecord(
                path=row["file_path"],
                library_external_id=str(row.get("library_id") or rec.library_external_id),
                item_external_id=rec.external_id,
                season=row.get("season_number"),
                episode=row.get("episode_number"),
            )
            for row in rows
            if row.get("file_path") and (attached is None or str(row.get("id")) in attached)
        ]

    def _attached_file_ids(self, rec: ItemRecord, content: str) -> set[str] | None:
        """The ids of the files Silo attaches to this series' episodes (what it plays), from
        the seasons' episode lists; None when they cannot be read."""
        _content, library = self._content_and_library(rec.external_id)
        ids: set[str] = set()
        try:
            seasons = self._get_json(
                f"/api/v2/catalog/series/{content}/seasons", library_id=library
            )
            for season in seasons.get("items") or []:
                number = season.get("season_number")
                if number is None:
                    continue
                eps = self._get_json(
                    f"/api/v2/catalog/series/{content}/seasons/{number}/episodes",
                    library_id=library,
                )
                for ep in eps.get("items") or []:
                    ids.update(str(f.get("file_id")) for f in ep.get("files") or [])
        except ConnectorError as exc:
            log.warning("could not read Silo's episodes for %s: %s", rec.title, exc)
            return None
        return ids or None  # no ids at all means the lists could not tell us, not "none"

    @classmethod
    def watch_key(cls, item_external_id: str) -> str:
        return content_id_of(item_external_id)

    def recent_plays(self, limit: int = 50) -> list[dict[str, Any]]:
        """Finished playbacks across every account and profile, newest first
        (an admin read). Each has the profile, the item (a content id), how far
        the viewer got (`watched_seconds`), the duration, `completed` (Silo's own
        verdict, from its watched threshold), and when it ended."""
        body = self._get_json("/api/v2/admin/playback-history", limit=limit)
        return list(body.get("items") or [])

    def users(self) -> list[UserRecord]:
        """Every profile of every account. Watch state is readable only for the
        key's own account: Silo refuses another account's X-Profile-Id."""
        me = str(self._get_json("/api/v2/account/me")["id"])
        out = []
        try:
            accounts = list(self._paged("/api/v2/admin/users"))
        except ConnectorError:
            accounts = []  # not an admin key: just our own profiles
        if not accounts:
            for p in self._get_json("/api/v2/profiles").get("items") or []:
                out.append(UserRecord(str(p["id"]), p.get("name") or str(p["id"]), me, True))
            return out
        for acct in accounts:
            acct_id = str(acct["id"])
            body = self._get_json(f"/api/v2/admin/users/{acct_id}/profiles")
            profiles = body.get("items", body) if isinstance(body, dict) else body
            for p in profiles or []:
                out.append(
                    UserRecord(
                        external_id=str(p["id"]),
                        name=p.get("name") or acct.get("username") or str(p["id"]),
                        account_id=acct_id,
                        watch_readable=acct_id == me or acct_id in self.account_keys,
                    )
                )
        return out

    def fetch_watch(
        self, user: UserRecord, progress: ProgressFn = _no_progress
    ) -> list[WatchRecord]:
        """The profile's progress list: every completed or started item.
        Item ids are content ids (not per library)."""
        # Headers are built per call, never kept on the connector, so users can
        # be read in parallel.
        key = self.account_keys.get(user.account_id or "")
        headers = {
            "Authorization": f"Bearer {key or self.config.secret or ''}",
            "X-Profile-Id": user.external_id,
        }
        rows = list(self._paged_as(headers, "/api/v2/progress"))
        out = []
        for r in rows:
            position = r.get("position_seconds") or 0
            if not r.get("completed") and position <= 0:
                continue
            out.append(
                WatchRecord(
                    item_external_id=str(r["media_item_id"]),
                    played=bool(r.get("completed")),
                    position_seconds=position or None,
                    duration_seconds=r.get("duration_seconds"),
                    last_played_at=parse_time(r.get("updated_at")),
                )
            )
        return out

    def set_played(
        self, user: UserRecord, item_id: str, played: bool, when: datetime | None = None
    ) -> None:
        """Mark watched (Silo dates it now; it takes no date) or unwatched (history
        is kept) as the profile, with its account's key. Backs off on 429."""
        key = self.account_keys.get(user.account_id or "")
        headers = {
            "Authorization": f"Bearer {key or self.config.secret or ''}",
            "X-Profile-Id": user.external_id,
        }
        method = "POST" if played else "DELETE"
        path = f"/api/v2/watched/{item_id}"
        for _attempt in range(MAX_RETRIES):
            with self._throttle:
                pass
            try:
                resp = self._client.request(
                    method, f"{self.base_url}{path}", headers=headers, timeout=60
                )
            except httpx.HTTPError as exc:
                raise ConnectorError(
                    f"cannot reach {self.base_url}: {exc.__class__.__name__}"
                ) from exc
            if resp.status_code == 429:
                with self._throttle:
                    time.sleep(_retry_after(resp))
                continue
            if resp.status_code not in (200, 204):
                raise ConnectorError(f"{method} {path}: {describe_http_failure(resp)}")
            return
        raise ConnectorError(f"{method} {path}: still rate limited after {MAX_RETRIES} attempts")

    def _admin_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(
                f"{self.base_url}{path}", headers=self._auth_headers(), json=body, timeout=120
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            raise ConnectorError(f"POST {path}: {describe_http_failure(resp)}")
        return resp.json()

    @staticmethod
    def _content_and_library(item_id: str) -> tuple[str, str]:
        """Silo's ids here are "library/content id" (a title in two libraries is two items)."""
        library, _, content = item_id.partition("/") if "/" in item_id else ("", "", item_id)
        return content, library

    def search_matches(
        self, item_id: str, kind: str, title: str, year: int | None
    ) -> list[MatchCandidate]:
        """POST /api/v2/admin/items/{id}/match/search: Silo's provider search for an item."""
        content, library = self._content_and_library(item_id)
        body: dict[str, Any] = {"title": title, "limit": 30}
        if year:
            body["year"] = year
        if library:
            body["library_id"] = library
        data = self._admin_post(f"/api/v2/admin/items/{content}/match/search", body)
        out = []
        for raw in data.get("candidates") or []:
            provider_ids = {k: str(v) for k, v in (raw.get("provider_ids") or {}).items() if v}
            out.append(
                MatchCandidate(
                    title=raw.get("title") or raw.get("matched_title") or "",
                    year=raw.get("year"),
                    ids={k.lower(): v for k, v in provider_ids.items()},
                    overview=raw.get("overview") or "",
                    image_url=raw.get("image_url") or "",
                    note=", ".join(str(r) for r in raw.get("match_reasons") or []),
                    payload={"provider_ids": provider_ids},
                )
            )
        return out

    def task_call(self, task: Task) -> tuple[str, str, Any]:
        """Scan: POST /api/v2/scan for a library, or one folder in it. Refresh: POST
        /api/v2/admin/items/{id}/refresh-metadata queues a complete refresh of the item."""
        if task.kind == "refresh":
            content, _library = self._content_and_library(task.item_id or "")
            return ("POST", f"/api/v2/admin/items/{content}/refresh-metadata", {"mode": "complete"})
        body: dict[str, Any] = {"library_id": task.library_id}
        if task.path:
            body["path"] = task.path
        return ("POST", "/api/v2/scan", body)

    def scan_all_calls(self, library_ids: list[str]) -> list[tuple[str, str, Any]]:
        """POST /api/v2/scan for each library. Silo gives a scan no id, so the read starts
        straight away; its scan-completed webhook reads again whatever the scan changes."""
        return [self.task_call(Task("scan", library_id=lib)) for lib in library_ids]

    def match_call(self, item_id: str, candidate: MatchCandidate) -> tuple[str, str, Any]:
        content, library = self._content_and_library(item_id)
        body = {**candidate.payload, **({"library_id": library} if library else {})}
        return ("POST", f"/api/v2/admin/items/{content}/match/apply", body)

    def apply_match(self, item_id: str, candidate: MatchCandidate) -> str:
        """POST /api/v2/admin/items/{id}/match/apply. `updated: false` means Silo does not
        know the id. Returns the item's id afterwards, built from the content id Silo
        answers with (the same id, unless Silo gave the item a new one)."""
        _method, path, body = self.match_call(item_id, candidate)
        data = self._admin_post(path, body)
        if not data.get("updated"):
            raise ItemGone("Silo did not change anything: it does not know this item id")
        content, library = self._content_and_library(item_id)
        new = str(data.get("content_id") or content)
        return f"{library}/{new}" if library else new

    def create_user(self, name: str, password: str, library_ids: list[str] | None = None) -> str:
        """POST /api/v2/admin/users with a default profile of the same name. Silo wants
        an email, so a placeholder one is made up; a password change is forced at first
        sign-in. Returns the new account's id."""
        slug = "".join(c if c.isalnum() else "." for c in name.lower()).strip(".") or "user"
        body: dict[str, Any] = {
            "username": name,
            "email": f"{slug}@mediawrangler.invalid",
            "password": password,
            "role": "user",
            "create_default_profile": True,
            "default_profile_name": name,
            "require_password_change": True,
        }
        if library_ids is not None:
            body["library_ids"] = list(library_ids)
        try:
            resp = self._client.post(
                f"{self.base_url}/api/v2/admin/users",
                headers=self._auth_headers(),
                json=body,
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if resp.status_code not in (200, 201):
            raise ConnectorError(f"POST /api/v2/admin/users: {describe_http_failure(resp)}")
        return str(resp.json()["id"])

    def password_reset_link(self, account_id: str) -> tuple[str, str]:
        """POST /api/v2/admin/users/{id}/password-reset with delivery=link: a one-time link
        the person opens to set their own password. Returns (url, expiry). Silo shows the
        link only in this response."""
        try:
            resp = self._client.post(
                f"{self.base_url}/api/v2/admin/users/{account_id}/password-reset",
                headers=self._auth_headers(),
                json={"delivery": "link"},
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if resp.status_code not in (200, 201):
            raise ConnectorError(f"POST password-reset: {describe_http_failure(resp)}")
        body = resp.json()
        return str(body["reset_url"]), str(body.get("expires_at") or "")

    # --- per-account keys (the only writes this connector makes, on request) ---------

    def accounts(self) -> list[dict[str, str]]:
        """Every login account: id and username (an admin read)."""
        return [
            {"id": str(a["id"]), "name": str(a.get("username") or a.get("name") or a["id"])}
            for a in self._paged("/api/v2/admin/users")
        ]

    def list_keys(self) -> list[dict[str, str]]:
        """Every API key in Silo (an admin read): id, owner account and label."""
        return [
            {
                "id": str(k["id"]),
                "user_id": str(k.get("user_id") or ""),
                "label": str(k.get("label") or ""),
            }
            for k in self._paged("/api/v2/admin/api-keys")
        ]

    def create_account_key(self, account_id: str, label: str) -> tuple[str, str]:
        """Make a key for one account; returns (Silo's id for it, the key). Silo
        shows the key only in this response."""
        try:
            resp = self._client.post(
                f"{self.base_url}/api/v2/admin/api-keys",
                headers=self._auth_headers(),
                json={"label": label, "user_id": account_id},
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if resp.status_code not in (200, 201):
            raise ConnectorError(f"POST /api/v2/admin/api-keys: {describe_http_failure(resp)}")
        body = resp.json()
        return str(body["id"]), str(body["key"])

    def delete_key(self, key_id: str) -> None:
        try:
            resp = self._client.delete(
                f"{self.base_url}/api/v2/admin/api-keys/{key_id}",
                headers=self._auth_headers(),
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if resp.status_code not in (200, 204, 404):
            raise ConnectorError(
                f"DELETE /api/v2/admin/api-keys/{key_id}: {describe_http_failure(resp)}"
            )


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(1.0, min(120.0, float(resp.headers.get("retry-after", ""))))
    except ValueError:
        return DEFAULT_RETRY_AFTER
