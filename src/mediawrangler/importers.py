"""Reading watch history exported from Yamtrack (CSV) and Trakt (zip of JSON).

Both are files you upload: nothing here talks to either service, and nothing
is ever written back to them. Each parser returns plain `ImportRec`s.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from .db import utcnow
from .filters import search_text
from .matching import normalise_title
from .models import (
    CanonicalItem,
    LibraryGroup,
    SourceItem,
    WatchImport,
    WatchImportItem,
    WatchImportPlay,
)

KINDS = {"yamtrack": "Yamtrack", "trakt": "Trakt"}
MAX_UPLOAD = 100 * 1024 * 1024
MAX_UNPACKED = 400 * 1024 * 1024  # a zip that expands past this is refused


class ImportFileError(ValueError):
    """The file isn't what it was said to be."""


@dataclass
class Play:
    at: datetime | None
    action: str | None = None


@dataclass
class ImportRec:
    kind: str  # "movie", "episode", "other"
    title: str
    year: int | None = None
    tvdb: str | None = None
    tmdb: str | None = None
    imdb: str | None = None
    season: int | None = None
    episode: int | None = None
    watched_at: datetime | None = None
    mal: str | None = None
    progress: int | None = None
    note: str | None = None
    plays: list[Play] = field(default_factory=list)  # every recorded play, not just the last


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _id(value) -> str | None:
    return str(value) if value not in (None, "", 0) else None


def parse_yamtrack(data: bytes) -> list[ImportRec]:
    """Episode rows say which episodes were watched; the show, season and
    "In progress" rows only summarise them. Films count when Completed or
    Repeating. MyAnimeList anime have no episode list, only how many episodes
    were watched, so they are kept as "anime" and resolved through the anime
    ID list when compared."""
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        need = {"media_id", "source", "media_type", "title", "status", "end_date"}
        if not need <= set(reader.fieldnames or ()):
            raise ImportFileError("This doesn't look like a Yamtrack CSV export.")
        out: list[ImportRec] = []
        merged: dict[tuple, ImportRec] = {}  # an episode or film can have several rows (rewatches)

        def add(key: tuple, rec: ImportRec) -> None:
            have = merged.get(key)
            if have is None:
                merged[key] = rec
                out.append(rec)
                have = rec
            at = _time(rec_date)
            if rec_date and all(p.at != at for p in have.plays):
                have.plays.append(Play(at, "yamtrack"))
            if at and (have.watched_at is None or at > have.watched_at):
                have.watched_at = at

        for row in reader:
            kind, source, mid = row["media_type"], row["source"], row["media_id"]
            rec_date = row["end_date"]
            if kind == "episode" and source == "tmdb":
                if not (row["season_number"].isdigit() and row["episode_number"].isdigit()):
                    continue
                if not rec_date:
                    continue
                add(
                    ("ep", mid, row["season_number"], row["episode_number"]),
                    ImportRec(
                        "episode",
                        row["title"],
                        tmdb=mid,
                        season=int(row["season_number"]),
                        episode=int(row["episode_number"]),
                    ),
                )
            elif (
                kind == "movie" and source == "tmdb" and row["status"] in ("Completed", "Repeating")
            ):
                add(("mv", mid), ImportRec("movie", row["title"], tmdb=mid))
            elif source == "mal" and row["status"] and (row["progress"] or "0").isdigit():
                if int(row["progress"]) > 0:
                    out.append(
                        ImportRec(
                            "anime",
                            row["title"],
                            mal=mid,
                            progress=int(row["progress"]),
                            watched_at=_time(row["end_date"]),
                            note=row["status"],
                        )
                    )
    except UnicodeDecodeError as exc:
        raise ImportFileError("This isn't a text (CSV) file.") from exc
    return out


def _json_parts(zf: zipfile.ZipFile, prefix: str) -> Iterable[list]:
    names = sorted(
        (n for n in zf.namelist() if re.fullmatch(rf"(?:.*/)?{prefix}-\d+\.json", n)),
        key=lambda n: int(re.search(r"-(\d+)\.json$", n).group(1)),
    )
    for name in names:
        part = json.loads(zf.read(name))
        if isinstance(part, list):
            yield part


def parse_trakt(data: bytes) -> list[ImportRec]:
    """Films come from the watched-movies and history files, episodes from the
    history (the watched-shows file has play counts only, no episodes)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ImportFileError("This isn't a zip file.") from exc
    if sum(i.file_size for i in zf.infolist()) > MAX_UNPACKED:
        raise ImportFileError("That zip unpacks to far more than a Trakt export should.")
    movies: dict[str, ImportRec] = {}
    episodes: dict[tuple, ImportRec] = {}
    found = False

    def newest(rec: ImportRec, at: datetime | None) -> None:
        if at and (rec.watched_at is None or at > rec.watched_at):
            rec.watched_at = at

    def movie(ids: dict, title: str, year, when, play: str | None) -> None:
        key = _id(ids.get("tmdb")) or _id(ids.get("imdb"))
        if key is None:
            return
        at = _time(when)
        rec = movies.get(key)
        if rec is None:
            rec = movies[key] = ImportRec(
                "movie", title, year, tmdb=_id(ids.get("tmdb")), imdb=_id(ids.get("imdb"))
            )
        newest(rec, at)
        if play:
            rec.plays.append(Play(at, play))

    try:
        for part in _json_parts(zf, "watched-movies"):
            found = True
            for item in part:
                m = item.get("movie") or {}
                movie(
                    m.get("ids") or {},
                    m.get("title", "?"),
                    m.get("year"),
                    item.get("last_watched_at"),
                    None,
                )
        for part in _json_parts(zf, "watched-history"):
            found = True
            for ev in part:
                if ev.get("type") == "movie":
                    m = ev.get("movie") or {}
                    movie(
                        m.get("ids") or {},
                        m.get("title", "?"),
                        m.get("year"),
                        ev.get("watched_at"),
                        ev.get("action") or "watch",
                    )
                elif ev.get("type") == "episode":
                    show, ep = ev.get("show") or {}, ev.get("episode") or {}
                    ids = show.get("ids") or {}
                    if ep.get("season") is None or ep.get("number") is None:
                        continue
                    key = (
                        _id(ids.get("tvdb")) or _id(ids.get("tmdb")) or _id(ids.get("imdb")),
                        ep["season"],
                        ep["number"],
                    )
                    if key[0] is None:
                        continue
                    at = _time(ev.get("watched_at"))
                    rec = episodes.get(key)
                    if rec is None:
                        rec = episodes[key] = ImportRec(
                            "episode",
                            show.get("title", "?"),
                            show.get("year"),
                            tvdb=_id(ids.get("tvdb")),
                            tmdb=_id(ids.get("tmdb")),
                            imdb=_id(ids.get("imdb")),
                            season=ep["season"],
                            episode=ep["number"],
                        )
                    newest(rec, at)
                    rec.plays.append(Play(at, ev.get("action") or "watch"))
        for rec in movies.values():  # watched, but no history entry for it
            if not rec.plays and rec.watched_at:
                rec.plays.append(Play(rec.watched_at, "summary"))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ImportFileError("A file inside the zip isn't valid Trakt data.") from exc
    if not found:
        raise ImportFileError(
            "No watched-movies or watched-history files found: is this a Trakt export?"
        )
    return [*movies.values(), *episodes.values()]


PARSERS = {"yamtrack": parse_yamtrack, "trakt": parse_trakt}


def apply_import(
    db: Session, person_id: int, kind: str, filename: str, recs: list[ImportRec]
) -> WatchImport:
    """Store a person's import, replacing their previous one of the same kind."""
    db.execute(
        delete(WatchImport).where(WatchImport.app_user_id == person_id, WatchImport.kind == kind)
    )
    imp = WatchImport(
        app_user_id=person_id,
        kind=kind,
        filename=filename[:255],
        imported_at=utcnow(),
        stats={
            "movies": sum(r.kind == "movie" for r in recs),
            "episodes": sum(r.kind == "episode" for r in recs),
            "anime": sum(r.kind == "anime" for r in recs),
            "plays": sum(len(r.plays) for r in recs),
        },
    )
    db.add(imp)
    db.flush()
    items = [
        WatchImportItem(
            import_id=imp.id,
            kind=r.kind,
            title=r.title[:512],
            year=r.year,
            tvdb=r.tvdb,
            tmdb=r.tmdb,
            imdb=r.imdb,
            season=r.season,
            episode=r.episode,
            watched_at=r.watched_at,
            mal=r.mal,
            progress=r.progress,
            note=r.note,
        )
        for r in recs
    ]
    db.add_all(items)
    db.flush()
    plays = [
        {"item_id": item.id, "played_at": p.at, "action": p.action}
        for item, rec in zip(items, recs, strict=True)
        for p in rec.plays
    ]
    if plays:
        db.execute(insert(WatchImportPlay), plays)
    db.commit()
    return imp


# --- matching to titles the apps have ----------------------------------------

SERIES_IDS = ("tvdb", "tmdb", "imdb")
MOVIE_IDS = ("tmdb", "imdb", "tvdb")


def canonical_lookup(db: Session) -> dict[tuple[str, str, str], int]:
    """(series|movie, id type, id) -> canonical item, from every id any app has
    reported for the title (a canonical item only keeps its creator's ids)."""
    out: dict[tuple[str, str, str], int] = {}
    for cid, kind, tvdb, tmdb, imdb in db.execute(
        select(
            CanonicalItem.id,
            CanonicalItem.kind,
            CanonicalItem.tvdb,
            CanonicalItem.tmdb,
            CanonicalItem.imdb,
        )
    ).all():
        for t, v in (("tvdb", tvdb), ("tmdb", tmdb), ("imdb", imdb)):
            if v:
                out.setdefault((kind, t, v), cid)
    for cid, kind, tvdb, tmdb, imdb in db.execute(
        select(
            SourceItem.canonical_id,
            SourceItem.kind,
            SourceItem.tvdb,
            SourceItem.tmdb,
            SourceItem.imdb,
        ).where(
            SourceItem.canonical_id.is_not(None),
            SourceItem.kind.in_(("series", "movie")),
            SourceItem.removed_at.is_(None),
        )
    ).all():
        for t, v in (("tvdb", tvdb), ("tmdb", tmdb), ("imdb", imdb)):
            if v:
                out.setdefault((kind, t, v), cid)
    return out


def match_rec(lookup: dict, rec) -> int | None:
    kind = "movie" if rec.kind == "movie" else "series"
    for t in MOVIE_IDS if kind == "movie" else SERIES_IDS:
        value = getattr(rec, t)
        if value and (cid := lookup.get((kind, t, value))) is not None:
            return cid
    return None


def anime_target(anime: dict, rec) -> tuple[str, ImportRec] | None:
    """A MyAnimeList entry as ("series"|"movie", a record carrying the apps'
    ids and the TVDB season), or None when the list doesn't know it well
    enough to place it."""
    entry = anime.get(rec.mal or "")
    if entry is None:
        return None
    if entry.type == "MOVIE":
        if not (entry.tmdb_movie or entry.imdb):
            return None
        return "movie", ImportRec(
            "movie",
            rec.title,
            tmdb=entry.tmdb_movie,
            imdb=entry.imdb[0] if entry.imdb else None,
            progress=rec.progress,
            watched_at=rec.watched_at,
        )
    if not (entry.tvdb or entry.tmdb_tv or entry.imdb):
        return None
    # The list leaves the season blank for some whole-show entries (Dragon Ball
    # GT, Dragon Ball Super): take those as the show's first season.
    season = entry.tvdb_season
    if season is None:
        if entry.type != "TV":
            return None
        season = 1
    return "series", ImportRec(
        "episode",
        rec.title,
        tvdb=entry.tvdb,
        tmdb=entry.tmdb_tv,
        imdb=entry.imdb[0] if entry.imdb else None,
        season=season,
        progress=rec.progress,
        watched_at=rec.watched_at,
    )


@dataclass
class Resolved:
    kind: str  # "series", "movie", "other" (can't be placed)
    cid: int | None  # the canonical title, when some app has ever known it
    rec: ImportRec  # carries the apps' ids (and the season, for anime)
    landing: str | None  # "show", "anime" or "movie": which list it belongs in


def _anime_ids(anime) -> tuple[frozenset, frozenset]:
    known = getattr(anime, "known_ids", None)
    if known is not None:
        return known
    return (
        frozenset(e.tvdb for e in anime.values() if e.tvdb),
        frozenset(e.tmdb_tv for e in anime.values() if e.tmdb_tv),
    )


def resolve(rec: ImportRec | WatchImportItem, lookup: dict, anime) -> Resolved:
    """What an imported record is: a film or a series (and which title and
    list it belongs to), or something that can't be placed at all. A show is
    anime when the anime list knows its TVDB or TMDB id."""
    if rec.kind == "anime":
        target = anime_target(anime, rec)
        if target is None:
            return Resolved("other", None, rec, None)
        kind, shown = target
        return Resolved(
            kind, match_rec(lookup, shown), shown, "movie" if kind == "movie" else "anime"
        )
    if rec.kind == "movie":
        return Resolved("movie", match_rec(lookup, rec), rec, "movie")
    tvdb_ids, tmdb_ids = _anime_ids(anime)
    is_anime = bool((rec.tvdb and rec.tvdb in tvdb_ids) or (rec.tmdb and rec.tmdb in tmdb_ids))
    return Resolved("series", match_rec(lookup, rec), rec, "anime" if is_anime else "show")


def title_key(rec, cid) -> tuple:
    """Same title in different files: by canonical item, else by TMDB (both
    Yamtrack and Trakt have it), else any other id, else the title."""
    if cid is not None:
        return ("canon", cid)
    for t in ("tmdb", "tvdb", "imdb"):
        if getattr(rec, t):
            return (t, getattr(rec, t))
    return ("title", normalise_title(rec.title))


def library_canonicals(db: Session) -> set[int]:
    """Titles some app has right now in a library."""
    return set(
        db.execute(
            select(SourceItem.canonical_id)
            .where(
                SourceItem.canonical_id.is_not(None),
                SourceItem.removed_at.is_(None),
                SourceItem.kind.in_(("series", "movie")),
            )
            .distinct()
        ).scalars()
    )


# --- where unmatched titles are listed ------------------------------------------

LANDINGS = {"show": "Shows", "anime": "Anime", "movie": "Films", "none": "Nowhere"}


def guess_landing(group: LibraryGroup) -> str:
    name = group.name.lower()
    if group.kind == "movie":
        return "none" if any(w in name for w in ("4k", "2160", "uhd")) else "movie"
    if "anime" in name:
        return "anime"
    return "none" if "sport" in name else "show"


def landing_of(group: LibraryGroup) -> str:
    return group.import_landing or guess_landing(group)


def landing_groups(db: Session) -> dict[str, int]:
    """landing -> the group (by position) that lists imported titles no app has."""
    out: dict[str, int] = {}
    for g in db.execute(
        select(LibraryGroup).order_by(LibraryGroup.position, LibraryGroup.id)
    ).scalars():
        landing = landing_of(g)
        if landing != "none":
            out.setdefault(landing, g.id)
    return out


# --- what the files have that no app does --------------------------------------


@dataclass
class ElsewhereCell:
    count: int  # episodes watched, for a series; 1 for a film
    last: datetime | None
    note: str | None


@dataclass
class ElsewhereRow:
    kind: str  # "movie", "series", "other"
    title: str
    year: int | None
    cells: dict  # import column id -> ElsewhereCell
    matched: bool  # a title some app knows, but has nothing left of in a library
    flags: set = field(default_factory=set)
    search: str = ""


def elsewhere(
    db: Session, person_id: int, id_base: int, anime: dict | None = None
) -> tuple[list[WatchImport], list]:
    """The person's imports, and the titles in them that can't be listed in
    Watch states' regular lists: no app has them and no group takes that kind
    of title, or they can't be placed at all."""
    imports = list(
        db.execute(
            select(WatchImport)
            .where(WatchImport.app_user_id == person_id)
            .order_by(WatchImport.kind)
        ).scalars()
    )
    if not imports:
        return imports, []
    anime = anime or {}
    lookup = canonical_lookup(db)
    in_library = library_canonicals(db)
    placed = landing_groups(db)
    rows: dict[tuple, ElsewhereRow] = {}
    for imp in imports:
        col = id_base + imp.id
        for rec in db.execute(
            select(WatchImportItem).where(WatchImportItem.import_id == imp.id)
        ).scalars():
            res = resolve(rec, lookup, anime)
            if res.cid in in_library or (res.landing and res.landing in placed):
                continue  # it has a row in a regular list
            note = None
            if res.kind == "other":
                note = f"MyAnimeList #{rec.mal} has no TVDB/TMDB/IMDb id in the anime ID list"
            kind = res.kind
            key = (kind, *title_key(res.rec, res.cid), rec.mal if kind == "other" else None)
            row = rows.get(key)
            if row is None:
                row = rows[key] = ElsewhereRow(kind, rec.title, rec.year, {}, res.cid is not None)
                row.search = search_text(rec.title)
            cell = row.cells.get(col)
            if cell is None:
                cell = row.cells[col] = ElsewhereCell(0, rec.watched_at, note)
            cell.count += rec.progress if rec.kind == "anime" and kind == "series" else 1
            if rec.watched_at and (cell.last is None or rec.watched_at > cell.last):
                cell.last = rec.watched_at
            row.flags |= {"any", kind, f"any:{col}", f"{kind}:{col}"}
    return imports, sorted(rows.values(), key=lambda r: (r.kind, r.title.lower()))
