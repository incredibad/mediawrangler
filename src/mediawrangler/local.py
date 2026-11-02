"""Local: MediaWrangler's own record of what each person has watched.

Read-only toward every app. Local is filled once per person from the union of
what their readable sources say (the most progress wins), then kept up to date
from the changes each app reports (and, on every refresh, anything an app has played that Local
has no newer record of, so a play whose change was missed or ignored still arrives):
- watching or unwatching in any app changes Local;
- Local keeps a dated list of plays that unwatching never deletes;
- plays of one title on the same day (or the day either side) from different
  apps are one play;
- every change is applied as it arrives; nothing waits for a decision (unwatching
  loses no play, so there is nothing to protect until Local writes to the apps).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, inspect, or_, select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import (
    AppUser,
    LibraryGroup,
    LocalCursor,
    LocalEvent,
    LocalPlay,
    LocalState,
    Source,
    SourceItem,
    SourceUser,
    WatchEvent,
    WatchState,
)
from .watch import PROGRESS_EVENT_SECONDS
from .watchdiff import Watch, build_watch_view, readable_users

log = logging.getLogger("mediawrangler.local")

BATCH = 20000

LKey = tuple  # (kind, canonical id, season, episode)


def local_key(key: tuple) -> LKey:
    """A watchdiff key as a Local key."""
    return (key[0], key[1], key[2], key[3]) if key[0] == "ep" else (key[0], key[1], 0, 0)


def _where(person_id: int, key: LKey) -> dict:
    return dict(
        app_user_id=person_id, kind=key[0], canonical_id=key[1], season=key[2], episode=key[3]
    )


class _Store:
    """One person's Local rows, loaded once and written back by the caller."""

    def __init__(self, db: Session, person_id: int, shows: set[int] | None = None):
        """`shows`: load only these titles (canonical ids), for a change that names them."""
        self.db, self.person_id = db, person_id
        state_where = [LocalState.app_user_id == person_id]
        play_where = [LocalPlay.app_user_id == person_id]
        event_where = [LocalEvent.app_user_id == person_id, LocalEvent.change == "unplayed"]
        if shows is not None:
            state_where.append(LocalState.canonical_id.in_(shows))
            play_where.append(LocalPlay.canonical_id.in_(shows))
            event_where.append(LocalEvent.canonical_id.in_(shows))
        self.states: dict[LKey, LocalState] = {
            (s.kind, s.canonical_id, s.season, s.episode): s
            for s in db.execute(select(LocalState).where(*state_where)).scalars()
        }
        self.plays: dict[tuple, LocalPlay] = {
            (p.kind, p.canonical_id, p.season, p.episode, p.day): p
            for p in db.execute(select(LocalPlay).where(*play_where)).scalars()
        }
        # An unwatch drops the title's row, so the log is what says it was unwatched since.
        self.unwatched: dict[LKey, datetime] = {
            (kind, cid, season, episode): at
            for kind, cid, season, episode, at in db.execute(
                select(
                    LocalEvent.kind,
                    LocalEvent.canonical_id,
                    LocalEvent.season,
                    LocalEvent.episode,
                    func.max(LocalEvent.at),
                )
                .where(*event_where)
                .group_by(
                    LocalEvent.kind, LocalEvent.canonical_id, LocalEvent.season, LocalEvent.episode
                )
            )
        }
        self.changed = 0

    def log(self, key: LKey, change: str, reason: str, at: datetime) -> None:
        self.db.add(LocalEvent(**_where(self.person_id, key), change=change, reason=reason, at=at))
        if change == "unplayed":
            self.unwatched[key] = max(at, self.unwatched.get(key, at))
        self.changed += 1

    def add_play(self, key: LKey, when: datetime | None, source: str) -> None:
        """Record a play. One on the same day as an existing play of the title,
        or the day either side (apps date a finished play to within a day of
        each other, by time zone), is that play, now with this app named too."""
        day = when.strftime("%Y-%m-%d") if when else ""
        near = [day]
        if when:
            near += [(when + timedelta(days=d)).strftime("%Y-%m-%d") for d in (-1, 1)]
        existing = next((p for d in near if (p := self.plays.get((*key, d)))), None)
        if existing is None:
            play = LocalPlay(
                **_where(self.person_id, key), day=day, played_at=when, sources=source[:255]
            )
            self.db.add(play)
            self.plays[(*key, day)] = play
        else:
            names = existing.sources.split(", ") if existing.sources else []
            if source not in names:
                existing.sources = ", ".join([*names, source])[:255]

    def set(
        self,
        key: LKey,
        played: bool,
        position: float | None,
        duration: float | None,
        when: datetime | None,
    ) -> LocalState:
        now = utcnow()
        st = self.states.get(key)
        if st is None:
            st = LocalState(**_where(self.person_id, key), played=played)
            self.db.add(st)
            self.states[key] = st
        st.played, st.position_seconds, st.duration_seconds = played, position, duration
        st.last_played_at, st.updated_at = when, now
        return st

    def drop(self, key: LKey) -> None:
        st = self.states.pop(key, None)
        if st is None:
            return
        if inspect(st).pending:
            self.db.expunge(st)  # set() in this same batch and not saved yet: just forget it
            return
        self.db.delete(st)
        # Delete now: a later set() of the same title inserts a new row, and a pending
        # delete would be flushed after that insert and trip the unique key.
        self.db.flush()


# --- seeding --------------------------------------------------------------------


def _best(candidates: list[tuple[Watch, str]]) -> tuple[bool, Watch | None]:
    """The most progress wins: played beats part-watched (the furthest position)
    beats nothing; among plays, the latest."""
    played = [w for w, _ in candidates if w.played]
    if played:
        return True, max(played, key=lambda w: w.last_played_at or datetime.min)
    partial = [w for w, _ in candidates if w.in_progress]
    if partial:
        return False, max(partial, key=lambda w: w.position or 0)
    return False, None


def seed_person(db: Session, person: AppUser, anime: dict | None = None) -> int:
    """Fill Local from everything the person's sources say, once. Returns the
    number of titles stored. Titles that only an imported file has and no app
    knows have no stable id, so they are left to the "Not in any app" tab."""
    sus = list(readable_users(db, person).values())
    # Changes after this point are applied by the normal refresh (idempotently).
    cursors = {
        su.id: db.execute(
            select(func.coalesce(func.max(WatchEvent.id), 0)).where(
                WatchEvent.source_user_id == su.id
            )
        ).scalar()
        for su in sus
    }
    store = _Store(db, person.id)
    now = utcnow()
    for group in db.execute(select(LibraryGroup).order_by(LibraryGroup.position)).scalars():
        view = build_watch_view(db, person, group, anime or {})
        by_key: dict[tuple, list[tuple[Watch, str]]] = {}
        for col in view.columns:
            for key, watch in col.state.items():
                if key[1] > 0:
                    by_key.setdefault(key, []).append((watch, col.source.name))
        for key, candidates in by_key.items():
            played, best = _best(candidates)
            if best is None:
                continue
            lkey = local_key(key)
            if lkey in store.states:
                continue  # already stored from another group's view
            store.set(
                lkey, played, None if played else best.position, best.duration, best.last_played_at
            )
            store.log(lkey, "played" if played else "progress", "seed", now)
            if played:
                for watch, name in candidates:
                    if watch.played:
                        store.add_play(lkey, watch.last_played_at, name)
    for suid, last in cursors.items():
        db.merge(LocalCursor(source_user_id=suid, last_event_id=last, held_count=0))
    person.local_seeded_at = now
    db.commit()
    return len(store.states)


# --- keeping up -----------------------------------------------------------------


def item_keys(db: Session, source: Source) -> dict[str, LKey]:
    """Watch id → Local key, for every episode and film an app has. A title matched
    only by name and year (low confidence) is left out: until it is confirmed it could
    be a different show with the same name, and what is watched must not cross to it."""
    from .connectors import get_connector_class

    wk = get_connector_class(source.type).watch_key
    rows = db.execute(
        select(
            SourceItem.kind,
            SourceItem.external_id,
            SourceItem.parent_external_id,
            SourceItem.season,
            SourceItem.episode,
            SourceItem.canonical_id,
        ).where(
            SourceItem.source_id == source.id,
            SourceItem.removed_at.is_(None),
            SourceItem.match_confidence.is_distinct_from("low"),
            SourceItem.kind.in_(("series", "episode", "movie")),
        )
    ).all()
    series = {ext: canon for k, ext, _p, _s, _e, canon in rows if k == "series" and canon}
    out: dict[str, LKey] = {}
    for k, ext, parent, season, episode, canon in rows:
        if k == "movie" and canon:
            out[wk(ext)] = ("mv", canon, 0, 0)
        elif k == "episode" and season is not None and episode is not None:
            sc = series.get(parent)
            if sc:
                out[wk(ext)] = ("ep", sc, season, episode)
    return out


def item_keys_for(db: Session, source: Source, keys: set[LKey]) -> dict[str, LKey]:
    """`item_keys` narrowed to these titles: every copy the app has of each."""
    from .connectors import get_connector_class

    wk = get_connector_class(source.type).watch_key
    usable = (
        SourceItem.source_id == source.id,
        SourceItem.removed_at.is_(None),
        SourceItem.match_confidence.is_distinct_from("low"),
    )
    out: dict[str, LKey] = {}
    films = {cid for kind, cid, _s, _e in keys if kind == "mv"}
    if films:
        for ext, canon in db.execute(
            select(SourceItem.external_id, SourceItem.canonical_id).where(
                *usable, SourceItem.kind == "movie", SourceItem.canonical_id.in_(films)
            )
        ):
            out[wk(ext)] = ("mv", canon, 0, 0)
    shows = {cid for kind, cid, _s, _e in keys if kind == "ep"}
    if shows:
        series = dict(
            db.execute(
                select(SourceItem.external_id, SourceItem.canonical_id).where(
                    *usable, SourceItem.kind == "series", SourceItem.canonical_id.in_(shows)
                )
            ).all()
        )
        if series:
            for ext, parent, season, episode in db.execute(
                select(
                    SourceItem.external_id,
                    SourceItem.parent_external_id,
                    SourceItem.season,
                    SourceItem.episode,
                ).where(
                    *usable,
                    SourceItem.kind == "episode",
                    SourceItem.parent_external_id.in_(series),
                )
            ):
                key = ("ep", series[parent], season, episode)
                if key in keys:
                    out[wk(ext)] = key
    return out


def keys_of_watch_ids(db: Session, source: Source, wids: set[str]) -> set[LKey]:
    """The Local keys of the items an app's watch ids name (Silo's watch id is the
    item id without its library)."""
    from .connectors import get_connector_class

    wk = get_connector_class(source.type).watch_key
    named = or_(
        SourceItem.external_id.in_(wids),
        *(SourceItem.external_id.like(f"%/{w}") for w in wids),
    )
    rows = db.execute(
        select(
            SourceItem.kind,
            SourceItem.external_id,
            SourceItem.parent_external_id,
            SourceItem.season,
            SourceItem.episode,
            SourceItem.canonical_id,
        ).where(
            SourceItem.source_id == source.id,
            SourceItem.removed_at.is_(None),
            SourceItem.match_confidence.is_distinct_from("low"),
            SourceItem.kind.in_(("episode", "movie")),
            named,
        )
    ).all()
    rows = [r for r in rows if wk(r[1]) in wids]
    parents = {r[2] for r in rows if r[0] == "episode" and r[2]}
    series = (
        dict(
            db.execute(
                select(SourceItem.external_id, SourceItem.canonical_id).where(
                    SourceItem.source_id == source.id,
                    SourceItem.kind == "series",
                    SourceItem.external_id.in_(parents),
                    SourceItem.match_confidence.is_distinct_from("low"),
                    SourceItem.canonical_id.is_not(None),
                )
            ).all()
        )
        if parents
        else {}
    )
    out: set[LKey] = set()
    for kind, _ext, parent, season, episode, canon in rows:
        if kind == "movie" and canon:
            out.add(("mv", canon, 0, 0))
        elif kind == "episode" and season is not None and episode is not None:
            if series.get(parent):
                out.add(("ep", series[parent], season, episode))
    return out


def _apply_events(
    db: Session,
    store: _Store,
    su: SourceUser,
    source: Source,
    events: list[WatchEvent],
    keys_of: dict[int, dict[str, LKey]],
) -> None:
    if source.id not in keys_of:
        keys_of[source.id] = item_keys(db, source)
    keys = keys_of[source.id]
    copies: dict[
        LKey, list[str]
    ] = {}  # a title can be several items (1080p and 4K, a replaced file)
    for wid, k in keys.items():
        copies.setdefault(k, []).append(wid)
    played_now: set[str] | None = None
    for ev in events:
        key = keys.get(ev.item_external_id)
        if key is None:
            continue
        if ev.kind == "unplayed" and len(copies[key]) > 1:
            if played_now is None:
                played_now = set(
                    db.execute(
                        select(WatchState.item_external_id).where(
                            WatchState.source_user_id == su.id, WatchState.played.is_(True)
                        )
                    ).scalars()
                )
            if any(w in played_now for w in copies[key] if w != ev.item_external_id):
                continue  # another copy is still played: that copy went, the title wasn't unwatched
        when = ev.source_time or ev.at
        st = store.states.get(key)
        if ev.kind == "played":
            if st is not None and st.played:
                continue
            store.set(key, True, None, st.duration_seconds if st else None, when)
            store.add_play(key, when, source.name)
            store.log(key, "played", source.name, ev.at)
        elif ev.kind == "unplayed":
            if st is not None:
                store.drop(key)
                store.log(key, "unplayed" if st.played else "progress", source.name, ev.at)
            if (ev.position_seconds or 0) >= PROGRESS_EVENT_SECONDS:
                store.set(key, False, ev.position_seconds, None, when)
        elif ev.kind == "progress":
            if st is not None and st.played:
                continue
            pos = ev.position_seconds or 0
            if pos >= PROGRESS_EVENT_SECONDS:
                moved = abs(pos - ((st.position_seconds if st else 0) or 0))
                store.set(key, False, pos, st.duration_seconds if st else None, when)
                if moved >= PROGRESS_EVENT_SECONDS:
                    store.log(key, "progress", source.name, ev.at)
            elif st is not None:
                store.drop(key)
                store.log(key, "progress", source.name, ev.at)


def _merge_new_source(db: Session, store: _Store, su: SourceUser, source: Source) -> None:
    """An app became readable for someone Local already holds (a Silo key was
    made for them): what it says joins Local by the seeding rule, the most
    progress wins. Nothing already in Local is lowered or removed."""
    keys = item_keys(db, source)
    now = utcnow()
    for ws in db.execute(select(WatchState).where(WatchState.source_user_id == su.id)).scalars():
        key = keys.get(ws.item_external_id)
        if key is None:
            continue
        have = store.states.get(key)
        if ws.played:
            if have is None or not have.played:
                store.set(key, True, None, ws.duration_seconds, ws.last_played_at)
                store.log(key, "played", source.name, now)
            store.add_play(key, ws.last_played_at, source.name)
        elif ws.position_seconds and have is None:
            store.set(key, False, ws.position_seconds, ws.duration_seconds, ws.last_played_at)
            store.log(key, "progress", source.name, now)
        elif (
            ws.position_seconds
            and not have.played
            and ws.position_seconds > (have.position_seconds or 0)
        ):
            store.set(key, False, ws.position_seconds, ws.duration_seconds, ws.last_played_at)
            store.log(key, "progress", source.name, now)


def _catch_up(db: Session, person: AppUser, keys_of: dict[int, dict[str, LKey]]) -> int:
    store = _Store(db, person.id)
    for su in readable_users(db, person).values():
        cursor = db.get(LocalCursor, su.id)
        if cursor is None:  # became readable after the seed
            if su.watch_synced_at is None:
                continue  # wait for its first read, then fold it in
            last = db.execute(
                select(func.coalesce(func.max(WatchEvent.id), 0)).where(
                    WatchEvent.source_user_id == su.id
                )
            ).scalar()
            _merge_new_source(db, store, su, db.get(Source, su.source_id))
            db.add(LocalCursor(source_user_id=su.id, last_event_id=last, held_count=0))
            continue
        events = list(
            db.execute(
                select(WatchEvent)
                .where(WatchEvent.source_user_id == su.id, WatchEvent.id > cursor.last_event_id)
                .order_by(WatchEvent.id)
                .limit(BATCH)
            ).scalars()
        )
        source = db.get(Source, su.source_id)
        if events:
            _apply_events(db, store, su, source, events, keys_of)
            cursor.last_event_id = events[-1].id
        _fill_gaps(db, store, su, source, keys_of)
    db.commit()
    return store.changed


QUICK_LIMIT = 500  # more new changes than this is a read, not a webhook: the full pass takes it


def catch_up_quick(db: Session) -> dict[int, set[LKey]] | None:
    """Local takes in only what the apps reported since it last looked, for only the
    people and titles named: a webhook's change, carried straight through. Returns the
    titles touched per person, or None when there is too much for this (a read's worth)
    and the full refresh should run instead. Gaps are left to the full refresh."""
    rows = db.execute(
        select(WatchEvent.source_user_id, func.count())
        .join(LocalCursor, LocalCursor.source_user_id == WatchEvent.source_user_id)
        .where(WatchEvent.id > LocalCursor.last_event_id)
        .group_by(WatchEvent.source_user_id)
    ).all()
    if not rows:
        return {}
    if any(n > QUICK_LIMIT for _su, n in rows):
        return None
    people = {
        su.app_user_id
        for su in db.execute(
            select(SourceUser).where(SourceUser.id.in_([su for su, _n in rows]))
        ).scalars()
        if su.app_user_id is not None
    }
    touched: dict[int, set[LKey]] = {}
    for person in db.execute(select(AppUser).where(AppUser.id.in_(people))).scalars():
        if person.local_seeded_at is None:
            continue
        work = []
        for su in readable_users(db, person).values():
            cursor = db.get(LocalCursor, su.id)
            if cursor is None:
                continue
            events = list(
                db.execute(
                    select(WatchEvent)
                    .where(
                        WatchEvent.source_user_id == su.id,
                        WatchEvent.id > cursor.last_event_id,
                    )
                    .order_by(WatchEvent.id)
                ).scalars()
            )
            if events:
                source = db.get(Source, su.source_id)
                keys = keys_of_watch_ids(db, source, {ev.item_external_id for ev in events})
                work.append((su, cursor, source, events, keys))
        keys = {k for *_rest, ks in work for k in ks}
        store = _Store(db, person.id, shows={k[1] for k in keys})
        for su, cursor, source, events, ks in work:
            if ks:
                _apply_events(
                    db, store, su, source, events, {source.id: item_keys_for(db, source, ks)}
                )
            cursor.last_event_id = events[-1].id
        if keys:
            touched[person.id] = keys
    db.commit()
    return touched


def _fill_gaps(
    db: Session,
    store: _Store,
    su: SourceUser,
    source: Source,
    keys_of: dict[int, dict[str, LKey]],
) -> None:
    """What an app has played that Local has not: a play seen while its title was not matched
    with confidence (so its change was never applied), or one whose change was missed. Only
    what is newer than Local's own record of the title is taken, so a title unwatched in Local
    since is not played again, and nothing in Local is lowered."""
    if source.id not in keys_of:
        keys_of[source.id] = item_keys(db, source)
    keys = keys_of[source.id]
    now = utcnow()
    for ext, played_at, duration in db.execute(
        select(
            WatchState.item_external_id, WatchState.last_played_at, WatchState.duration_seconds
        ).where(WatchState.source_user_id == su.id, WatchState.played.is_(True))
    ):
        key = keys.get(ext)
        if key is None:
            continue
        have = store.states.get(key)
        if have is not None and (
            have.played or played_at is None or (have.updated_at and played_at <= have.updated_at)
        ):
            continue
        gone_at = store.unwatched.get(key)
        if have is None and gone_at and (played_at is None or played_at <= gone_at):
            continue
        store.set(key, True, None, duration, played_at)
        store.add_play(key, played_at, source.name)
        store.log(key, "played", source.name, now)


def refresh_local(db: Session, anime: dict | None = None) -> int:
    """Seed any person not yet seeded (once all their readable apps have been
    read), then apply new changes for everyone. Returns Local changes made."""
    changed = 0
    # One read of each app's keys per refresh, shared by everyone: the keys are the
    # app's, not the person's, and building them is most of a refresh's time.
    keys_of: dict[int, dict[str, LKey]] = {}
    for person in list(db.execute(select(AppUser)).scalars()):
        name = person.name
        try:
            sus = list(readable_users(db, person).values())
            if not sus:
                continue
            if person.local_seeded_at is None:
                if any(su.watch_synced_at is None for su in sus):
                    continue
                changed += seed_person(db, person, anime)
            else:
                changed += _catch_up(db, person, keys_of)
            if person.local_seeded_at is not None:
                from .unfiled import adopt_new_imports  # builds on this module

                changed += adopt_new_imports(db, person, anime)
        except Exception:
            # One person's failure must not stop everyone else's Local from updating.
            db.rollback()
            log.exception("could not update Local for %s", name)
    return changed


# --- carrying history across a match fix ----------------------------------------


def move_history(db: Session, source: Source, external_id: str, old_canonical: int | None) -> int:
    """After a fix changed which show an item is: cut the plays and progress recorded
    under the old show and paste them under the new one, episode for episode (the same
    season and episode number; a film moves whole). Done only when no app still has
    anything under the old show, and only for episodes the new show has. A play already
    on the new show is kept, played beating part-watched. Returns the rows moved."""
    item = db.execute(
        select(SourceItem).where(
            SourceItem.source_id == source.id, SourceItem.external_id == external_id
        )
    ).scalar_one_or_none()
    new = item.canonical_id if item is not None else None
    if not new or not old_canonical or new == old_canonical:
        return 0
    still_there = db.execute(
        select(func.count()).where(
            SourceItem.canonical_id == old_canonical, SourceItem.removed_at.is_(None)
        )
    ).scalar()
    if still_there:
        return 0  # another app still has the old show: its history is not for us to take
    has: set[tuple[int, int]] = set()
    shows = db.execute(
        select(SourceItem.source_id, SourceItem.external_id).where(
            SourceItem.canonical_id == new,
            SourceItem.kind == "series",
            SourceItem.removed_at.is_(None),
        )
    ).all()
    for sid, ext in shows:
        has.update(
            (season, episode)
            for season, episode in db.execute(
                select(SourceItem.season, SourceItem.episode).where(
                    SourceItem.source_id == sid,
                    SourceItem.parent_external_id == ext,
                    SourceItem.kind == "episode",
                    SourceItem.removed_at.is_(None),
                )
            )
        )
    now = utcnow()
    moved = 0
    for st in db.execute(
        select(LocalState).where(LocalState.canonical_id == old_canonical)
    ).scalars():
        if st.kind == "ep" and (st.season, st.episode) not in has:
            continue
        there = db.execute(
            select(LocalState).where(
                LocalState.app_user_id == st.app_user_id,
                LocalState.kind == st.kind,
                LocalState.canonical_id == new,
                LocalState.season == st.season,
                LocalState.episode == st.episode,
            )
        ).scalar_one_or_none()
        if there is None:
            st.canonical_id = new
            st.updated_at = now
            kept = st
        else:
            if st.played and not there.played:
                there.played, there.last_played_at = True, st.last_played_at
                there.position_seconds = None
            kept = there
            db.delete(st)
        db.add(
            LocalEvent(
                app_user_id=kept.app_user_id,
                kind=kept.kind,
                canonical_id=new,
                season=kept.season,
                episode=kept.episode,
                change="played" if kept.played else "progress",
                reason="moved",
                at=now,
            )
        )
        moved += 1
    for play in db.execute(
        select(LocalPlay).where(LocalPlay.canonical_id == old_canonical)
    ).scalars():
        if play.kind == "ep" and (play.season, play.episode) not in has:
            continue
        there = db.execute(
            select(LocalPlay).where(
                LocalPlay.app_user_id == play.app_user_id,
                LocalPlay.kind == play.kind,
                LocalPlay.canonical_id == new,
                LocalPlay.season == play.season,
                LocalPlay.episode == play.episode,
                LocalPlay.day == play.day,
            )
        ).scalar_one_or_none()
        if there is None:
            play.canonical_id = new
        else:
            names = {n for n in (there.sources + ", " + play.sources).split(", ") if n}
            there.sources = ", ".join(sorted(names))[:255]
            db.delete(play)
    db.commit()
    return moved


# --- reading Local --------------------------------------------------------------


def local_states(db: Session, person: AppUser) -> dict[LKey, Watch]:
    out: dict[LKey, Watch] = {}
    for st in db.execute(select(LocalState).where(LocalState.app_user_id == person.id)).scalars():
        out[(st.kind, st.canonical_id, st.season, st.episode)] = Watch(
            st.played, st.position_seconds, st.duration_seconds, st.last_played_at
        )
    return out
