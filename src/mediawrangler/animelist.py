"""The community anime ID list (Fribb/anime-lists), used to turn MyAnimeList
ids from a Yamtrack export into the TVDB/TMDB/IMDb ids the apps use.

The only thing MediaWrangler downloads from outside your network. It is a
public file, fetched about daily; a failed fetch keeps the copy already
saved, and without any copy anime simply stay unmatched.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property
from pathlib import Path

import httpx

from .db import Database, utcnow
from .models import AppSetting

log = logging.getLogger(__name__)

URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
SETTING_KEY = "anime_list"
FILENAME = "anime-list.json"
REFRESH_EVERY = timedelta(days=1)
RETRY_AFTER = timedelta(hours=6)
MAX_BYTES = 60 * 1024 * 1024


@dataclass(frozen=True)
class AnimeEntry:
    type: str  # "TV", "MOVIE", "OVA", "ONA", "SPECIAL"...
    tvdb: str | None
    tvdb_season: int | None
    tmdb_tv: str | None
    tmdb_movie: str | None
    imdb: tuple[str, ...]


def _first(value) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if value not in (None, "") else None


class AnimeMap(dict):
    """MAL id -> entry, plus every TVDB/TMDB series id the list knows (a show
    with one of these is anime)."""

    @cached_property
    def known_ids(self) -> tuple[frozenset[str], frozenset[str]]:
        return (
            frozenset(e.tvdb for e in self.values() if e.tvdb),
            frozenset(e.tmdb_tv for e in self.values() if e.tmdb_tv),
        )


def parse(data: list) -> AnimeMap:
    out = AnimeMap()
    for e in data:
        if not isinstance(e, dict) or e.get("mal_id") is None:
            continue
        tmdb = e.get("themoviedb_id")
        tv = movie = None
        if isinstance(tmdb, dict):
            tv, movie = _first(tmdb.get("tv")), _first(tmdb.get("movie"))
        elif tmdb is not None:  # older entries: a bare number or list
            if e.get("type") == "MOVIE":
                movie = _first(tmdb)
            else:
                tv = _first(tmdb)
        season = (e.get("season") or {}).get("tvdb")
        imdb = e.get("imdb_id") or []
        entry = AnimeEntry(
            str(e.get("type") or ""),
            _first(e.get("tvdb_id")),
            season if isinstance(season, int) else None,
            tv,
            movie,
            tuple(str(i) for i in (imdb if isinstance(imdb, list) else [imdb]) if i),
        )
        mals = e["mal_id"] if isinstance(e["mal_id"], list) else [e["mal_id"]]
        for m in mals:
            out.setdefault(str(m), entry)
    return out


class AnimeList:
    """The saved file, parsed once and re-read only when the file changes."""

    def __init__(self, directory: Path):
        self.path = directory / FILENAME
        self._lock = threading.Lock()
        self._stamp: float | None = None
        self._map: AnimeMap = AnimeMap()

    def entries(self) -> AnimeMap:
        with self._lock:
            try:
                stamp = self.path.stat().st_mtime
            except OSError:
                self._stamp, self._map = None, AnimeMap()
                return self._map
            if stamp != self._stamp:
                try:
                    self._map = parse(json.loads(self.path.read_text()))
                except (OSError, ValueError):
                    log.exception("could not read %s", self.path)
                    self._map = AnimeMap()
                self._stamp = stamp
            return self._map


def _status(db: Database) -> dict:
    with db.sessionmaker() as s:
        row = s.get(AppSetting, SETTING_KEY)
        return dict(row.value) if row else {}


def status(db: Database) -> dict:
    return _status(db)


def _record(db: Database, **values) -> None:
    with db.sessionmaker() as s:
        row = s.get(AppSetting, SETTING_KEY)
        merged = {**(dict(row.value) if row else {}), **values}
        if row is None:
            s.add(AppSetting(key=SETTING_KEY, value=merged))
        else:
            row.value = merged
        s.commit()


def fetch(db: Database, lst: AnimeList, client: httpx.Client | None = None) -> bool:
    """Download and save the list; True on success. Never raises."""
    now = utcnow()
    try:
        with client or httpx.Client(timeout=60, follow_redirects=True) as http:
            resp = http.get(URL)
            resp.raise_for_status()
            if len(resp.content) > MAX_BYTES:
                raise ValueError("the file is far larger than expected")
            data = resp.json()
        entries = parse(data)
        if len(entries) < 1000:
            raise ValueError("the file doesn't look like the anime list")
        tmp = lst.path.with_suffix(".tmp")
        tmp.write_bytes(resp.content)
        os.replace(tmp, lst.path)
    except Exception as exc:
        log.warning("anime list refresh failed: %s", exc)
        _record(db, attempted_at=now.isoformat(), error=f"{exc.__class__.__name__}: {exc}"[:300])
        return False
    _record(
        db,
        fetched_at=now.isoformat(),
        attempted_at=now.isoformat(),
        entries=len(entries),
        error=None,
    )
    return True


def refresh_due(db: Database, lst: AnimeList, now: datetime | None = None) -> bool:
    """Fetch if the saved copy is over a day old (or missing), but not more
    often than every few hours after a failure."""
    now = now or utcnow()
    st = _status(db)
    if st.get("attempted_at") and st.get("error"):
        if now - datetime.fromisoformat(st["attempted_at"]) < RETRY_AFTER:
            return False
    elif st.get("fetched_at") and lst.path.exists():
        if now - datetime.fromisoformat(st["fetched_at"]) < REFRESH_EVERY:
            return False
    return fetch(db, lst)
