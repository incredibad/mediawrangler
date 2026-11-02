"""The connector interface and registry.

A connector *class* knows how to talk to one kind of app (Jellyfin, Sonarr...).
A connector *instance* is a `Source` row: its own URL, secret, path map and
options. Adding an app means writing one class and registering it.

Capabilities are declared only once implemented, so the UI and the sync engine
can rely on them. Every connector can test its connection.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

import httpx

from .. import __version__
from .paths import PathMap

# For progress messages ("series" has no extra s).
PLURAL = {"series": "series", "episode": "episodes", "movie": "movies"}


class Capability(StrEnum):
    LIST_ITEMS = "list_items"
    LIST_FILES = "list_files"
    READ_WATCH_STATE = "read_watch_state"
    WRITE_WATCH_STATE = "write_watch_state"
    REALTIME = "realtime"
    REMATCH = "rematch"
    SCAN = "scan"
    REFRESH = "refresh"
    LOOKUP = "lookup"
    ADD = "add"
    OVERRIDE = "override"
    LIST_USERS = "list_users"
    CREATE_USER = "create_user"


@dataclass
class MatchCandidate:
    """One answer to "which show is this?" from an app's own search."""

    title: str
    year: int | None
    ids: dict[str, str]  # lower-case keys: tvdb, tmdb, imdb
    overview: str = ""
    image_url: str = ""
    note: str = ""  # why the app offered it
    payload: dict = field(default_factory=dict)  # what the app wants back to apply it


@dataclass(frozen=True)
class Task:
    """Something to ask an app to do, as it names things: "scan" (look for new files, or an
    arr's rescan of one show) or "refresh" (download an item's details again)."""

    kind: str
    path: str | None = None  # a folder, in the app's own path
    library_id: str | None = None
    item_id: str | None = None


@dataclass(frozen=True)
class ConnectorConfig:
    base_url: str
    secret: str | None = None
    path_map: PathMap = field(default_factory=PathMap)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectionTest:
    ok: bool
    message: str
    version: str | None = None


class ConnectorError(Exception):
    """A connector call failed in a way worth showing the user."""


class ItemGone(ConnectorError):
    """The app does not know the item it was asked to change: its file was most likely
    replaced (an upgrade gives it a new id) or removed."""


ID_TYPES = ("tvdb", "tmdb", "imdb")


@dataclass
class LibraryRecord:
    external_id: str
    name: str
    kind: str  # "series", "movie" or "other"
    paths: list[str] = field(default_factory=list)


@dataclass
class ItemRecord:
    """A series, movie or episode as a connector reports it."""

    external_id: str
    kind: str  # "series", "movie", "episode"
    title: str
    library_external_id: str | None = None
    year: int | None = None
    tvdb: str | None = None
    tmdb: str | None = None
    imdb: str | None = None
    path: str | None = None
    parent_external_id: str | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    source_status: str | None = None
    # Set when the connector fetched per-item details (Silo) in this run.
    details_fetched: bool = False
    # Video files belonging to the item, when the source lists them per item
    # (Silo movies: one per version).
    file_paths: list[str] = field(default_factory=list)


@dataclass
class TitleSnapshot:
    """One show or film as an app has it now: the item (and a show's episodes) and the
    files, for reading a single title again instead of the whole library."""

    root_id: str
    items: list[ItemRecord]
    files: list[FileRecord]


@dataclass
class FileRecord:
    """A video file. Paths are as the source sees them; the path map makes
    them comparable."""

    path: str
    library_external_id: str | None = None
    size: int | None = None
    quality: str | None = None
    item_external_id: str | None = None
    season: int | None = None
    episode: int | None = None
    extra: bool = False
    # Why a file does not belong in a library (see paths.odd_kind); such a file is also extra.
    flag: str | None = None


@dataclass
class RootRecord:
    """A content folder the source scanned or skipped (Silo)."""

    path: str
    state: str
    library_external_id: str | None = None
    reason: str | None = None
    file_count: int | None = None
    item_external_id: str | None = None


@dataclass
class Snapshot:
    """Everything a full fetch saw. Anything the source stored before but is
    absent here is treated as removed."""

    libraries: list[LibraryRecord]
    items: list[ItemRecord]
    files: list[FileRecord] = field(default_factory=list)
    roots: list[RootRecord] = field(default_factory=list)


@dataclass
class UserRecord:
    external_id: str
    name: str
    account_id: str | None = None
    # Whether these credentials can read this user's watch state.
    watch_readable: bool = True
    # Library ids this user may open; None where the app does not restrict them.
    library_ids: list[str] | None = None


@dataclass
class WatchRecord:
    """One played or in-progress item for one user."""

    item_external_id: str
    played: bool
    play_count: int | None = None
    position_seconds: float | None = None
    duration_seconds: float | None = None
    last_played_at: datetime | None = None


def parse_time(value: str | None) -> datetime | None:
    """ISO 8601 from an API (Jellyfin uses 7 fractional digits) to naive UTC."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, _, rest = text.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        tail = rest[len(digits) :]
        text = f"{head}.{digits[:6].ljust(6, '0')}{tail}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


ProgressFn = Callable[[str], None]


def _no_progress(_message: str) -> None:
    pass


class Connector(ABC):
    type: ClassVar[str]
    display_name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]] = frozenset()
    # "media_server" (Jellyfin, Silo: can be the master), "arr" or "disk".
    role: ClassVar[str] = "media_server"
    # A word in the library-group name this source's libraries belong to, when
    # folder names can't tell (Sportarr's single root is just "/data").
    group_hint: ClassVar[str | None] = None
    # Whether forget_folder can tell the app a show's folder was deleted (see there).
    can_forget: ClassVar[bool] = False
    # "url" or "path" (the disk scanner takes a container path).
    location_kind: ClassVar[str] = "url"
    url_placeholder: ClassVar[str] = "http://host:port"
    # Where the user creates a key for MediaWrangler in this app.
    key_help: ClassVar[str] = ""
    needs_secret: ClassVar[bool] = True
    uses_path_map: ClassVar[bool] = True

    def __init__(self, config: ConnectorConfig, client: httpx.Client | None = None):
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"User-Agent": f"MediaWrangler/{__version__}"},
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Connector:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def _get(self, path: str, **kwargs) -> httpx.Response:
        try:
            resp = self._client.get(f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        return resp

    @abstractmethod
    def test_connection(self) -> ConnectionTest:
        """Check the URL answers and the secret is accepted. Must be read-only."""

    # The methods below are implemented by connectors that declare the matching
    # capability. They raise until then so a missing implementation is loud.

    def fetch_full(
        self,
        progress: ProgressFn = _no_progress,
        previous: dict[str, ItemRecord] | None = None,
    ) -> Snapshot:
        """Read every library and item. `previous` holds what the last run
        stored, so connectors with expensive per-item calls can reuse it."""
        raise NotImplementedError(f"{self.type} does not implement fetch_full yet")

    def fetch_delta(self, since: datetime) -> Iterable[Any]:
        raise NotImplementedError(f"{self.type} does not implement fetch_delta yet")

    @classmethod
    def watch_key(cls, item_external_id: str) -> str:
        """The id this source's watch data uses for an item (Silo: content id)."""
        return item_external_id

    # True where `items_exist` can tell a replaced or removed item from an unwatched one.
    checks_items = False

    def items_exist(self, user: UserRecord, item_ids: list[str]) -> set[str]:
        """Which of these items the app still has and this user can open."""
        raise NotImplementedError(f"{self.type} cannot check items")

    def users(self) -> list[UserRecord]:
        raise NotImplementedError(f"{self.type} does not list users")

    def fetch_watch(
        self, user: UserRecord, progress: ProgressFn = _no_progress
    ) -> list[WatchRecord]:
        """Every played or in-progress item for one user. Read-only."""
        raise NotImplementedError(f"{self.type} does not read watch state")

    def handle_webhook(self, payload: dict[str, Any]) -> Iterable[Any]:
        raise NotImplementedError(f"{self.type} does not implement webhooks yet")

    def set_played(
        self, user: UserRecord, item_id: str, played: bool, when: datetime | None = None
    ) -> None:
        """Mark one episode or film played (at `when`, where the app lets us say)
        or unplayed for one user. `item_id` is the app's own id (for Silo, the
        content id). A write: only the writer calls it, never a sync."""
        raise NotImplementedError(f"{self.type} does not implement watch-state writes")

    def create_user(self, name: str, password: str, library_ids: list[str] | None = None) -> str:
        """Make an account with a temporary password; returns the app's id for it.
        library_ids limits the libraries it may open (None leaves the app's default)."""
        raise NotImplementedError

    def search_matches(
        self, item_id: str, kind: str, title: str, year: int | None
    ) -> list[MatchCandidate]:
        """Ask the app which shows or films it could identify this item as. A search:
        it changes nothing."""
        raise NotImplementedError(f"{self.type} cannot search for matches")

    def match_call(self, item_id: str, candidate: MatchCandidate) -> tuple[str, str, Any]:
        """The exact (method, path, JSON body) apply_match will send, to show beforehand."""
        raise NotImplementedError(f"{self.type} cannot change a match")

    def apply_match(self, item_id: str, candidate: MatchCandidate) -> str:
        """Tell the app this item is the candidate. A write. Returns the app's id for the
        item afterwards (the same one, unless the app gave it a new one)."""
        raise NotImplementedError(f"{self.type} cannot change a match")

    def task_call(self, task: Task) -> tuple[str, str, Any]:
        """The exact (method, path with query, JSON body) run_task will send."""
        raise NotImplementedError(f"{self.type} cannot do that")

    def fetch_title(
        self, external_id: str, kind: str, library_external_id: str | None
    ) -> TitleSnapshot:
        """Read one show or film (and a show's episodes and files) from the app. A read."""
        raise NotImplementedError(f"{self.type} cannot read a single title")

    def lookup(self, term: str) -> list[MatchCandidate]:
        """Search the app's own database of shows or films by name. A read."""
        raise NotImplementedError(f"{self.type} cannot look titles up")

    def add_call(self, candidate: MatchCandidate, folder: str) -> tuple[str, str, Any]:
        """The exact (method, path, JSON body) add_title will send for a title whose files
        already sit in `folder` (the app's own path). May read settings (a quality profile)."""
        raise NotImplementedError(f"{self.type} cannot add titles")

    def add_title(self, candidate: MatchCandidate, folder: str) -> str:
        """Add the title to the app, tracking the folder, unmonitored and with no search for
        downloads. A write. Returns the app's id for it."""
        raise NotImplementedError(f"{self.type} cannot add titles")

    def override_call(
        self, library_id: str, root_path: str, candidate: MatchCandidate, kind: str
    ) -> tuple[str, str, Any]:
        """The exact call that tells the app what a folder is."""
        raise NotImplementedError(f"{self.type} cannot be told what a folder is")

    def set_override(
        self, library_id: str, root_path: str, candidate: MatchCandidate, kind: str
    ) -> None:
        """Tell the app what a folder is. A write."""
        raise NotImplementedError(f"{self.type} cannot be told what a folder is")

    def find_title(
        self, name: str, kind: str, library_external_id: str | None, path: str
    ) -> str | None:
        """The app's id for the show or film that lives in this folder (in the app's own
        path), or None when it has not picked it up. A read."""
        raise NotImplementedError(f"{self.type} cannot look up a single title")

    def find_titles(
        self, name: str, kind: str, library_external_id: str | None, path: str
    ) -> list[str]:
        """Every entry the app holds for this folder. Most apps keep one per title; Jellyfin
        keeps one per copy of a film."""
        found = self.find_title(name, kind, library_external_id, path)
        return [found] if found else []

    def task_done(self, token: str) -> bool:
        """Whether a task run_task started has finished (raises if it failed). Only for
        apps that give a task an id."""
        raise NotImplementedError(f"{self.type} does not report when a task finishes")

    def task_token(self, resp: httpx.Response) -> str | None:
        """The id the app gave the task it just started, when it gives one."""
        return None

    def web_link(self, external_id: str) -> str | None:
        """The address of this show or film in the app's own web UI, or None when the app has
        no page for one. May ask the app (an arr's page is named by its slug)."""
        return None

    def forget_folder(self, path: str) -> None:
        """Tell the app a folder (in its own paths) was deleted, so it drops the show or film
        that lived there. A write to the app's state."""
        raise NotImplementedError(f"{self.type} cannot be told a folder was deleted")

    def scan_all_calls(self, library_ids: list[str]) -> list[tuple[str, str, Any]]:
        """The calls that ask the app to scan every library for added, changed and deleted
        files, sent before a full read. Empty when the app has none to ask."""
        return []

    def scan_all(self, library_ids: list[str]) -> list[str]:
        """Ask the app to scan every library. Returns the ids to wait on (see scan_all_done);
        an app that gives no id is read straight away."""
        tokens = [self._request(*call) for call in self.scan_all_calls(library_ids)]
        return [t for t in tokens if t]

    def scan_all_done(self, token: str) -> bool:
        return self.task_done(token)

    def run_task(self, task: Task) -> str | None:
        """Ask the app to do the task. A write to the app's state (never to files).
        Returns the task's id when the app gives one (see task_done)."""
        return self._request(*self.task_call(task))

    def _request(self, method: str, path: str, body: Any) -> str | None:
        try:
            resp = self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._auth_headers(),  # type: ignore[attr-defined]
                json=body,
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"cannot reach {self.base_url}: {exc.__class__.__name__}") from exc
        if resp.status_code == 404:
            raise ItemGone(f"{method} {path}: {self.display_name} does not know this item")
        if resp.status_code not in (200, 201, 202, 204):
            raise ConnectorError(f"{method} {path}: {describe_http_failure(resp)}")
        return self.task_token(resp)


def describe_http_failure(resp: httpx.Response) -> str:
    if resp.status_code in (401, 403):
        return f"key rejected (HTTP {resp.status_code})"
    if resp.status_code == 429:
        return "rate limited (HTTP 429); try again shortly"
    if 300 <= resp.status_code < 400:
        return f"redirected (HTTP {resp.status_code}); check the URL"
    return f"unexpected HTTP {resp.status_code}"


_REGISTRY: dict[str, type[Connector]] = {}


def register(cls: type[Connector]) -> type[Connector]:
    if cls.type in _REGISTRY:
        raise ValueError(f"connector type {cls.type!r} registered twice")
    _REGISTRY[cls.type] = cls
    return cls


def connector_types() -> dict[str, type[Connector]]:
    return dict(sorted(_REGISTRY.items(), key=lambda kv: kv[1].display_name))


def get_connector_class(type_: str) -> type[Connector]:
    try:
        return _REGISTRY[type_]
    except KeyError:
        raise ConnectorError(f"unknown connector type {type_!r}") from None
