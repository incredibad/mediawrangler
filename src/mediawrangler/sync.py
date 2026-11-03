"""Running syncs in the background, one at a time per source."""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select, update

from .connectors import Capability, ConnectorConfig, get_connector_class
from .connectors.base import ConnectorError, ItemGone
from .connectors.paths import PathMap
from .crypto import SecretBox
from .db import Database, utcnow
from .groups import ensure_groups, ensure_path_maps, source_order
from .ingest import (
    apply_folder,
    apply_libraries,
    apply_snapshot,
    apply_title,
    previous_records,
    recanonicalise,
    remove_title,
)
from .local import catch_up_quick, move_history, refresh_local
from .matching import run_matching
from .models import (
    AuditLog,
    Library,
    LibraryGroup,
    LibraryGroupMember,
    MediaFile,
    Source,
    SourceAccountKey,
    SourceItem,
    SyncRun,
    TrashItem,
)
from .watch import WatchStats, apply_users, apply_watch, link_users
from .writes import ENABLED_KEY, PAUSED_KEY, WriteRunner, flag

log = logging.getLogger(__name__)

PROGRESS_INTERVAL = 1.0
WATCH_WORKERS = 8  # users whose watch state is read at once


def connector_for(source: Source, box: SecretBox, **extra_options):
    config = ConnectorConfig(
        base_url=source.base_url,
        secret=box.decrypt(source.secret_enc) if source.secret_enc else None,
        path_map=PathMap.from_pairs(source.path_map or []),
        options={**(source.options or {}), **extra_options},
    )
    return get_connector_class(source.type)(config)


def can_sync(source: Source) -> bool:
    try:
        caps = get_connector_class(source.type).capabilities
    except Exception:
        return False
    return Capability.LIST_ITEMS in caps or Capability.LIST_FILES in caps


def _grouped_disk_libraries(session, source: Source) -> list[str]:
    return list(
        session.execute(
            select(Library.external_id)
            .join(LibraryGroupMember, LibraryGroupMember.library_id == Library.id)
            .where(Library.source_id == source.id, Library.removed_at.is_(None))
        ).scalars()
    )


def finish_matching(session) -> dict:
    """Groups, derived path maps and matching, after any source changed."""
    ensure_groups(session)
    for changed in ensure_path_maps(session):
        recanonicalise(session, changed)
    return run_matching(session, source_order(session))


SETTLE_SCALE = 1.0  # multiplies the fixed pauses after a fix (tests set it to 0)
SCAN_TRIGGERS = ("manual", "schedule")  # full reads that ask the app to scan first
SCAN_WAIT = 30 * 60  # how long a full read waits for the app's scan
SCAN_POLL = 5


class SyncManager:
    def __init__(self, db: Database, box: SecretBox, workers: int = 1):
        self.db = db
        self.box = box
        self._pool = ThreadPoolExecutor(workers, thread_name_prefix="sync")
        self._lock = threading.Lock()
        # Single-title reads after a fix: their own worker, so one never waits behind a
        # library read, and their state by job number.
        self._title_pool = ThreadPoolExecutor(3, thread_name_prefix="job")
        self._futures: list = []
        # Paths a delete job is moving to the trash now, so a second request for them is refused.
        self._busy: set[str] = set()
        self._titles: dict[int, dict] = {}
        self._title_seq = 0
        self._version = 0
        self._match_lock = threading.Lock()
        self._local_lock = threading.Lock()
        self._local_dirty = False
        self.writes = WriteRunner(db, box)
        self.writes.on_replaced = lambda sid: self.start(sid, "replaced", "full")
        # Set by the app: the anime ID list, for filling Local from imports.
        self.anime_entries = lambda: {}

    @property
    def version(self) -> int:
        """Moves whenever data that comparison views are built from may have
        changed: after every sync, rematch, or change made through the UI."""
        return self._version

    def bump(self, background: bool = False) -> None:
        """Local first takes in whatever the apps reported, so views built
        after the bump see it. `background`: from the web server's own thread,
        where the first fill of Local (half a minute) mustn't hold up requests."""
        if background:
            threading.Thread(target=self._refresh_local, daemon=True).start()
        else:
            self._refresh_local()
        self._bump_version()

    def apply_now(self) -> None:
        """A webhook's change, carried straight through: Local takes in only the people
        and titles it names, only those titles are planned, and the queue is sent. The
        full refresh and write cycle stay as the safety net after reads. Falls back to
        them when there is more than a webhook's worth to take in."""
        from .writes import plan_titles

        with self._local_lock:  # a full refresh running now finishes first
            try:
                with self.db.sessionmaker() as s:
                    touched = catch_up_quick(s)
                    if touched:
                        plan_titles(s, touched)
            except Exception:
                log.exception("could not apply a change directly")
                touched = None
        if touched is None:
            self.bump()
            return
        self._bump_version()
        self.writes.kick(plan=False)

    def _bump_version(self) -> None:
        with self._lock:
            self._version += 1

    def _refresh_local(self) -> None:
        self._local_dirty = True
        if not self._local_lock.acquire(blocking=False):
            return  # the refresh running now goes round again for this change
        try:
            while self._local_dirty:
                self._local_dirty = False
                with self.db.sessionmaker() as s:
                    changed = refresh_local(s, self.anime_entries())
                if changed:
                    self._bump_version()  # views built meanwhile are stale
        except Exception:
            log.exception("could not update Local")
        finally:
            self._local_lock.release()
        self.writes.kick()  # whatever Local now says is sent on to the apps

    def recover(self) -> None:
        """Runs still marked running were cut off by a restart."""
        with self.db.sessionmaker() as s:
            s.execute(
                update(SyncRun)
                .where(SyncRun.status == "running")
                .values(status="error", error="interrupted by a restart", finished_at=utcnow())
            )
            s.commit()

    def running(self, session, source_id: int) -> SyncRun | None:
        return session.execute(
            select(SyncRun).where(SyncRun.source_id == source_id, SyncRun.status == "running")
        ).scalar()

    def start(self, source_id: int, trigger: str = "manual", mode: str = "full") -> int | None:
        """Queue a sync ("full", or "watch" for users and watch state only);
        returns the run id (an existing one if already running)."""
        with self._lock, self.db.sessionmaker() as s:
            source = s.get(Source, source_id)
            if source is None or not source.enabled or not can_sync(source):
                return None
            current = self.running(s, source_id)
            if current is not None:
                return current.id
            run = SyncRun(
                source_id=source_id,
                mode=mode,
                trigger=trigger,
                status="running",
                progress="Queued",
            )
            s.add(run)
            s.commit()
            run_id = run.id
        self._pool.submit(self._run, run_id)
        return run_id

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._title_pool.shutdown(wait=False, cancel_futures=True)

    def refresh_title(
        self,
        source_id: int,
        external_id: str | None,
        old_canonical: int | None = None,
        replaces: str | None = None,
        locate: dict | None = None,
        label: str = "Updating from the app",
    ) -> int:
        """Read one show or film again (not the whole app), match it, and carry watch
        history over if a fix moved it to another show. `replaces` is the item's old id
        when the app gave it a new one; `locate` says how to find a title the app has just
        picked up (name, kind, library, folder in the app's own path). Returns a job
        number for title_job."""
        job = self._new_job(label)
        spec = {
            "source_id": source_id,
            "external_id": external_id,
            "old_canonical": old_canonical,
            "replaces": replaces,
            "locate": locate,
        }
        self._submit(self._run_title, job, spec)
        return job

    def read_title(
        self,
        source_id: int,
        external_id: str | None = None,
        locate: dict | None = None,
        kind: str | None = None,
    ) -> dict:
        """refresh_title with no toast, run here and now (a webhook's narrow read). `kind`
        lets a title MediaWrangler has not stored yet be read (an arr's new film or show).
        Returns the job state: {"state": "done"} or {"state": "error", "error": ...}."""
        state = self._title_state(
            0,
            {
                "source_id": source_id,
                "external_id": external_id,
                "old_canonical": None,
                "replaces": None,
                "locate": locate,
                "kind": kind,
            },
        )
        self.bump()
        return state

    def _new_job(self, label: str, text: str = "Starting…") -> int:
        """A background job the person can watch (a toast on every page): what it is doing, and
        how it ended. Held in memory, so a restart forgets it."""
        with self._lock:
            self._title_seq += 1
            job = self._title_seq
            self._titles[job] = {
                "id": job,
                "label": label,
                "state": "running",
                "error": "",
                "text": text,
                "started": time.time(),
                "finished": None,
                "dismissed": False,
            }
        return job

    def _finish(self, job: int, state: dict) -> None:
        with self._lock:
            entry = self._titles.setdefault(job, {"id": job, "label": "", "started": time.time()})
            entry.update({"text": "", "dismissed": False, **state})
            entry["finished"] = time.time()
            if state.get("state") == "error":
                for step in entry.get("steps", []):
                    if step["state"] == "running":
                        step["state"] = "failed"

    def _plan_steps(self, job: int, rows: list[tuple[str, str]]) -> None:
        """The stages a toast shows as a list (pending until each starts), added in order."""
        with self._lock:
            entry = self._titles.get(job)
            if entry is None:
                return
            steps = entry.setdefault("steps", [])
            have = {s["key"] for s in steps}
            steps.extend(
                {"key": key, "label": label, "state": "pending", "detail": ""}
                for key, label in rows
                if key not in have
            )

    def _step(self, job: int, key: str, state: str, detail: str = "") -> None:
        """One stage of a toast: "running", "done" or "failed", and a line about it."""
        with self._lock:
            for step in (self._titles.get(job) or {}).get("steps", []):
                if step["key"] == key:
                    step["state"], step["detail"] = state, detail

    def jobs(self, keep: float = 900.0) -> list[dict]:
        """The jobs to show: those running, and those that finished in the last `keep` seconds
        and have not been dismissed. Oldest first."""
        now = time.time()
        with self._lock:
            for job in [
                j
                for j, e in self._titles.items()
                if e.get("finished") and now - e["finished"] > keep
            ]:
                del self._titles[job]
            return [
                {**e, **({"steps": [dict(s) for s in e["steps"]]} if "steps" in e else {})}
                for _j, e in sorted(self._titles.items())
                if e.get("label") and not e.get("dismissed")
            ]

    def set_job_url(self, job: int, url: str) -> None:
        """The page to check once the job is done (the toast links to it)."""
        with self._lock:
            if job in self._titles:
                self._titles[job]["url"] = url

    def dismiss_job(self, job: int) -> None:
        with self._lock:
            if job in self._titles and self._titles[job].get("finished"):
                self._titles[job]["dismissed"] = True

    def _submit(self, fn, *args) -> None:
        self._futures = [f for f in self._futures if not f.done()]
        self._futures.append(self._title_pool.submit(fn, *args))

    def wait_idle(self, timeout: float = 60.0) -> None:
        """Wait for every background job to finish (for tests)."""
        from concurrent.futures import wait

        wait(list(self._futures), timeout=timeout)

    def run_job(self, label: str, work) -> int:
        """Run `work(job)` in the background. It returns a state dict (state, error, note) or
        None for done; raising FixRefused (or anything) ends the job with that message. Returns
        the job number."""
        from .fixes import FixRefused

        job = self._new_job(label)

        def runner() -> None:
            try:
                state = work(job) or {"state": "done", "error": ""}
            except FixRefused as exc:
                state = {"state": "error", "error": str(exc)}
            except Exception as exc:
                log.exception("background job failed: %s", label)
                state = {"state": "error", "error": f"{exc.__class__.__name__}: {exc}"[:300]}
            self._finish(job, state)
            self.bump()

        self._submit(runner)
        return job

    # -- fixes: each runs as a job, then reads the title or folder from the apps -----------

    def busy_paths(self) -> set[str]:
        with self._lock:
            return set(self._busy)

    def _claim(self, paths: list[str]) -> None:
        from .fixes import FixRefused

        with self._lock:
            taken = [p for p in paths if p in self._busy]
            if taken:
                names = ", ".join(p.rsplit("/", 1)[-1] for p in taken[:3])
                raise FixRefused(f"Already being deleted: {names}. Wait for that to finish.")
            self._busy.update(paths)

    def _release(self, paths: list[str]) -> None:
        with self._lock:
            self._busy.difference_update(paths)

    def trash_job(
        self,
        group_id: int,
        folder: str,
        arr_id: int,
        paths: list[str],
        label: str,
        leftover: bool = False,
    ) -> int:
        """Move the files to the trash, then read the folder on the share again and each app
        that has the title until it no longer lists them. With leftover (every file of a
        folder no arr tracks), the emptied folders are removed once the apps are up to date."""
        from . import trash
        from .physical import build_physical_view

        self._claim(paths)  # refuses (FixRefused) files another delete is already moving

        def work(job: int) -> dict:
            try:
                with self.db.sessionmaker() as s:
                    group = s.get(LibraryGroup, group_id)
                    arr = s.get(Source, arr_id) if arr_id else None
                    view = build_physical_view(s, group)
                    n = len(paths)
                    self._text(job, f"Moving {n} {'file' if n == 1 else 'files'} to the trash…")
                    self._plan_steps(
                        job, [("move", f"Move {n} {'file' if n == 1 else 'files'} to the trash")]
                    )
                    self._step(job, "move", "running")
                    moved = trash.move_to_trash(
                        s, self.box, group, view, folder, arr, paths, leftover
                    )
                    self._step(job, "move", "done")
                    removed = [m.original_path for m in moved]
                    # An unusual file is in no arr, but a media server may list it (Silo counts
                    # `._` stubs): only an app that has one of them on record is asked to rescan.
                    only_unusual = all(m.why.startswith("Unusual file") for m in moved)
            finally:
                self._release(paths)
            state = self._removal_state(
                job, group_id, folder, removed, False, True, listed_only=only_unusual
            )
            if leftover:  # after the apps, so each still finds the folder it is asked about
                trash.remove_empty_dirs(folder)
                if not os.path.isdir(folder):  # a media server keeps the empty show until told
                    state = self._forget_folder(job, group_id, folder, state)
            return state

        return self.run_job(label, work)

    def restore_job(self, item_id: int, label: str) -> int:
        """Put a trashed file back where it was, in the background (a copy across disks can
        take minutes). Refuses (FixRefused) a file already being put back or deleted."""
        from . import trash
        from .fixes import FixRefused

        with self.db.sessionmaker() as s:
            item = s.get(TrashItem, item_id)
            if item is None:
                raise FixRefused("That file is not in the trash")
            paths = [item.original_path]
        self._claim(paths)

        def work(job: int) -> dict | None:
            try:
                with self.db.sessionmaker() as s:
                    item = s.get(TrashItem, item_id)
                    self._plan_steps(job, [("move", "Move the file back from the trash")])
                    self._step(job, "move", "running")
                    trash.restore(s, item)
                    self._step(job, "move", "done")
                    folder, original = item.folder, item.original_path
                    group_id = self._group_of(s, folder)
            finally:
                self._release(paths)
            if group_id is None:  # in no library group: nothing to bring up to date
                return None
            return self._removal_state(job, group_id, folder, [], added=[original])

        return self.run_job(label, work)

    @staticmethod
    def _group_of(s, folder: str) -> int | None:
        """The library group whose share folder holds this folder."""
        disk_libs = s.execute(
            select(Library)
            .join(Source, Source.id == Library.source_id)
            .where(Source.type == "disk", Library.removed_at.is_(None))
        ).scalars()
        for lib in disk_libs:
            if any(folder.startswith(p.rstrip("/") + "/") for p in lib.paths or []):
                return s.execute(
                    select(LibraryGroupMember.group_id).where(
                        LibraryGroupMember.library_id == lib.id
                    )
                ).scalar()
        return None

    def match_job(self, item_id: int, candidate: str, label: str) -> int:
        """Change which show an app thinks an item is, then read that title again and carry
        watch history to the new show."""
        from . import fixes

        def work(job: int) -> dict:
            with self.db.sessionmaker() as s:
                item = s.get(SourceItem, item_id)
                source = s.get(Source, item.source_id) if item else None
                if item is None or source is None:
                    raise fixes.FixRefused("that item is not known any more")
                cand = fixes.parse_candidate(candidate)
                old_canonical, old_id, source_id = item.canonical_id, item.external_id, source.id
                self._text(
                    job, f"Asking {source.name} to change the match (it can take a minute or two)…"
                )
                ok, message, new_id = fixes.apply(s, self.box, item, cand)
                if not ok:
                    raise fixes.FixRefused(message)
            self._text(job, "Reading the show again…")
            time.sleep(5 * SETTLE_SCALE)
            spec = {
                "source_id": source_id,
                "external_id": new_id,
                "old_canonical": old_canonical,
                "replaces": old_id if new_id != old_id else None,
                "locate": None,
            }
            return self._title_state(job, spec)

        return self.run_job(label, work)

    def task_job(self, group_id: int, source_id: int, folder: str, kind: str, label: str) -> int:
        """Ask one app to scan a folder or refresh a show, wait until it says it is done (or a
        pause long enough for one show), then read the title again."""
        from . import tasks
        from .fixes import FixRefused
        from .physical import build_physical_view

        def work(job: int) -> dict:
            if kind == "scan":
                return self._scan_one(job, group_id, source_id, folder)
            with self.db.sessionmaker() as s:
                group = s.get(LibraryGroup, group_id)
                source = s.get(Source, source_id)
                view = build_physical_view(s, group)
                p = tasks.plan(s, group, view.roots, source, folder, kind)
                self._text(job, f"{p.title}…")
                ok, message, token = tasks.run(s, self.box, p)
                if not ok:
                    raise FixRefused(message)
                spec = {
                    "source_id": source.id,
                    "external_id": p.item.external_id if p.item else None,
                    "old_canonical": None,
                    "replaces": None,
                    "locate": p.locate,
                }
                kind_of_app = source.type
            self._wait_for_task(
                job, source_id, token, 8 if kind_of_app in ("sonarr", "radarr") else 12
            )
            return self._title_state(job, spec)

        return self.run_job(label, work)

    def _scan_one(self, job: int, group_id: int, source_id: int, folder: str) -> dict:
        """One app looks at the folder; done when it lists what the share has (see
        folder_sync)."""
        from . import folder_sync, tasks
        from .physical import build_physical_view

        with self.db.sessionmaker() as s:
            group = s.get(LibraryGroup, group_id)
            source = s.get(Source, source_id)
            view = build_physical_view(s, group)
            tasks.plan(s, group, view.roots, source, folder, "scan")  # refuses what it cannot do
            present = set(
                s.execute(
                    select(MediaFile.canonical_path).where(
                        MediaFile.source_id == view.disk_id,
                        MediaFile.canonical_path.like(folder + "/%"),
                        MediaFile.removed_at.is_(None),
                        MediaFile.extra.is_(False),
                        MediaFile.flag.is_(None),
                    )
                ).scalars()
            )
            key = f"app{source.id}"
            self._plan_steps(job, [(key, f"Update {source.name}")])
            out = folder_sync.sync_app(
                self, job, key, group_id, source_id, folder, list(view.roots), set(), present
            )
            folder_sync.store(s, source, out)
            with self._match_lock:
                finish_matching(s)
        self._step(job, key, "failed" if out.note else "done", out.note or out.detail)
        return {
            "state": "done",
            "error": "",
            **({"note": out.note, "failed": True} if out.note else {}),
        }

    def identify_job(
        self, group_id: int, source_id: int, folder: str, action: str, candidate: str, label: str
    ) -> int:
        """Add a title to Sonarr or Radarr, or tell Silo what a folder is, then read the title
        again."""
        from . import fixes, identify
        from .connectors.base import Task

        def work(job: int) -> dict:
            with self.db.sessionmaker() as s:
                group = s.get(LibraryGroup, group_id)
                source = s.get(Source, source_id)
                p = identify.plan(s, group, source, folder, action)
                cand = fixes.parse_candidate(candidate)
                self._text(job, f"{p.title}…")
                ok, message, new_id = identify.run(s, self.box, p, cand)
                if not ok:
                    raise fixes.FixRefused(message)
                locate = {
                    "kind": p.kind,
                    "library": p.library_id,
                    "name": cand.title,
                    "path": p.app_path,
                }
                if action != "add":  # Silo: ask it to read the folder, then look the title up
                    try:
                        with connector_for(p.source, self.box) as connector:
                            connector.run_task(
                                Task("scan", path=p.app_path, library_id=p.library_id)
                            )
                    except ConnectorError:
                        pass  # the override is saved; the next scan of the library picks it up
                src_id = p.source.id
            time.sleep((8 if action == "add" else 12) * SETTLE_SCALE)
            spec = {
                "source_id": src_id,
                "external_id": new_id if action == "add" else None,
                "old_canonical": None,
                "replaces": None,
                "locate": locate,
            }
            return self._title_state(job, spec)

        return self.run_job(label, work)

    def import_job(
        self, group_id: int, source_id: int, folder: str, path: str, episode_id: int, label: str
    ) -> int:
        """Tell Sonarr which episode an orphaned file is (its Manual Import moves and renames
        it), wait for its command, then bring every app up to date with the folder."""
        from . import episode_import
        from .physical import build_physical_view

        self._claim([path])

        def work(job: int) -> dict:
            try:
                with self.db.sessionmaker() as s:
                    group = s.get(LibraryGroup, group_id)
                    source = s.get(Source, source_id)
                    view = build_physical_view(s, group)
                    p = episode_import.plan(s, group, source, folder, path, view.roots)
                    self._plan_steps(
                        job,
                        [
                            ("import", f"{source.name} imports the file"),
                            ("rename", f"{source.name} renames it into its season folder"),
                        ],
                    )
                    self._step(job, "import", "running")
                    token = episode_import.run(s, self.box, p, episode_id)
                self._wait_for_task(job, source_id, token, 0)
                self._step(job, "import", "done")
                self._step(job, "rename", "running")
                with self.db.sessionmaker() as s:  # imported in place: Sonarr's rename moves it
                    renamed = episode_import.rename(s, self.box, p, episode_id)
                if renamed:
                    self._step(job, "rename", "running", f"→ {renamed[1]}")
                    self._wait_for_task(job, source_id, renamed[0], 0)
                done = f"→ {renamed[1]}" if renamed else "Already in place"
                self._step(job, "rename", "done", done)
            finally:
                self._release([path])
            return self._removal_state(job, group_id, folder, [path], rescan=True)

        return self.run_job(label, work)

    FORGET_WAIT = 300  # Jellyfin's watcher waits 60 s after the library's last change

    def forget_job(self, item_id: int, label: str) -> int:
        """Tell a media server a show's folder was deleted, and wait until it drops the show."""

        def work(job: int) -> dict | None:
            with self.db.sessionmaker() as s:
                source_id = s.get(SourceItem, item_id).source_id
            note = self._forget(job, source_id, item_id)
            return {"state": "done", "error": "", "note": note, "failed": True} if note else None

        return self.run_job(label, work)

    def _forget_folder(self, job: int, group_id: int, folder: str, state: dict) -> dict:
        """After a show's folder was removed: each media server that still lists the show is
        told the folder was deleted, and waited on until it drops it."""
        from . import forget

        with self.db.sessionmaker() as s:
            group = s.get(LibraryGroup, group_id)
            found = [(src.id, i.id) for src, i in forget.in_folder(s, folder, group.kind)]
        notes = [n for sid, iid in found if (n := self._forget(job, sid, iid))]
        if not notes:
            return state
        note = " ".join([state.get("note", ""), *notes]).strip()
        return {**state, "note": note, "failed": True}

    def _forget(self, job: int, source_id: int, item_id: int) -> str:
        """One media server, one show: send "folder deleted". The app drops the show after its
        file watcher's delay (Jellyfin: 60 s, restarted by every other change in the library,
        so a run of deletes takes longer); the toast does not wait for that. A background
        check drops the show here once the app has. Returns a note when it could not be told."""
        with self.db.sessionmaker() as s:
            source, item = s.get(Source, source_id), s.get(SourceItem, item_id)
            name, ext, kind, path = source.name, item.external_id, item.kind, item.path
        key = f"forget:{source_id}"
        self._plan_steps(job, [(key, f"{name} drops the empty show")])
        try:
            with connector_for(source, self.box) as connector:
                connector.forget_folder(path)
        except ConnectorError as exc:
            self._step(job, key, "failed", str(exc)[:160])
            return f"{name} could not be told the folder was deleted ({exc})."
        self._step(job, key, "done", "Told; it drops the show in about a minute")
        threading.Thread(target=self._await_drop, args=(source_id, ext, kind), daemon=True).start()
        return ""

    def _await_drop(self, source_id: int, ext: str, kind: str) -> bool:
        """Read the show every 10 seconds, for up to FORGET_WAIT, until the app no longer has
        it; then drop it here. Returns whether it went."""
        with self.db.sessionmaker() as s:
            source = s.get(Source, source_id)
        for _ in range(self.FORGET_WAIT // 10 + 1):
            time.sleep(10 * SETTLE_SCALE)
            try:
                with connector_for(source, self.box) as connector:
                    connector.fetch_title(ext, kind, None)
            except ItemGone:
                with self.db.sessionmaker() as s:
                    remove_title(s, s.get(Source, source_id), ext)
                    s.commit()
                self.bump()
                return True
            except ConnectorError:
                pass  # a failed read while waiting is tried again
        log.warning("%s still lists %s after %d s", source.name, ext, self.FORGET_WAIT)
        return False

    def _wait_for_task(self, job: int, source_id: int, token: str | None, pause: int) -> None:
        """Until the app says its task is done (checked every 2 seconds, up to 2 minutes), or a
        fixed pause for an app that gives no id."""
        if not token:
            time.sleep(pause * SETTLE_SCALE)
            return
        deadline = time.monotonic() + 120
        with self.db.sessionmaker() as s:
            source = s.get(Source, source_id)
        while time.monotonic() < deadline:
            time.sleep(2 * SETTLE_SCALE)
            self._text(job, f"Waiting for {source.name} to finish…")
            with connector_for(source, self.box) as connector:
                if connector.task_done(token):
                    return

    def _text(self, job: int, text: str) -> None:
        with self._lock:
            if job in self._titles:
                self._titles[job]["text"] = text

    def refresh_after_removal(
        self,
        group_id: int,
        folder: str,
        removed: list[str],
        apps: bool = True,
        label: str = "Updating after removing files",
        listed_only: bool = False,
    ) -> int:
        """Files were taken off the share: read the folder again and bring each app that has
        it up to date (folder_sync). With listed_only, only an app that has one of the removed
        files on record is asked. Returns a job number for title_job."""
        job = self._new_job(label, "Reading the folder…")
        self._submit(
            self._run_removal, job, group_id, folder, list(removed), False, apps, listed_only
        )
        return job

    def rescan_folder(
        self, group_id: int, folder: str, label: str = "Rescanning in every app"
    ) -> int:
        """Read one show or film's folder on the share again and bring each app that has it
        up to date, until each lists what the share has (folder_sync). For files that arrived
        (or changed) outside any app. Returns a job number for title_job."""
        job = self._new_job(label, "Reading the folder…")
        self._submit(self._run_removal, job, group_id, folder, [], True)
        return job

    def _run_removal(
        self,
        job: int,
        group_id: int,
        folder: str,
        removed: list[str],
        rescan: bool = False,
        apps: bool = True,
        listed_only: bool = False,
    ) -> None:
        self._finish(
            job, self._removal_state(job, group_id, folder, removed, rescan, apps, listed_only)
        )
        self.bump()

    def _removal_state(
        self,
        job: int,
        group_id: int,
        folder: str,
        removed: list[str],
        rescan: bool = False,
        apps: bool = True,
        listed_only: bool = False,
        added: list[str] | None = None,
    ) -> dict:
        """Read the folder on the share again, then bring each app up to date with it: none
        may list a `removed` file; with rescan, each must list every file the share has; each
        must list the `added` files (a put back)."""
        from .physical import build_physical_view

        state: dict = {"state": "done", "error": ""}
        gone = set(removed)
        present = set(added or [])
        try:
            with self.db.sessionmaker() as s:
                group = s.get(LibraryGroup, group_id)
                view = build_physical_view(s, group)
                self._plan_steps(job, [("share", "Read the share again")])
                self._step(job, "share", "running")
                if view.disk_id is not None:
                    disk = s.get(Source, view.disk_id)
                    root = next((r for r in view.roots if folder.startswith(r + "/")), "")
                    with connector_for(disk, self.box) as connector:
                        found = connector.scan_folder(folder, root.rsplit("/", 1)[-1] or None)
                    apply_folder(s, disk, folder, found)
                    if rescan:  # what the share has is what every app should end up with
                        present |= {f.path for f in found if not f.extra and not f.flag}
                self._step(job, "share", "done")
                notes = self._update_apps(
                    s, job, group, view, folder, gone, present, apps, listed_only
                )
                if notes:
                    state["note"] = " ".join(notes)
                    state["failed"] = True
                with self._match_lock:
                    finish_matching(s)
        except Exception as exc:
            log.exception("could not refresh the folder after removing files")
            state = {"state": "error", "error": f"{exc.__class__.__name__}: {exc}"[:300]}
        return state

    def _update_apps(
        self, s, job, group, view, folder, gone, present, apps, listed_only
    ) -> list[str]:
        """Bring every app that has the folder up to date with the share, all at once (see
        folder_sync). One toast line per app; one note per app that does not agree."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from . import folder_sync

        apps_here = [
            source
            for source in view.columns
            if apps
            and source.id != view.disk_id
            and not (listed_only and not folder_sync.lists_any(s, source, gone))
        ]
        self._plan_steps(job, [(f"app{src.id}", f"Update {src.name}") for src in apps_here])
        notes: list[str] = []
        if not apps_here:
            return notes
        roots = list(view.roots)
        with ThreadPoolExecutor(len(apps_here)) as pool:
            futures = {
                pool.submit(
                    folder_sync.sync_app,
                    self,
                    job,
                    f"app{src.id}",
                    group.id,
                    src.id,
                    folder,
                    roots,
                    gone,
                    present,
                ): src
                for src in apps_here
            }
            for future in as_completed(futures):
                source, key = futures[future], f"app{futures[future].id}"
                try:
                    out = future.result()
                except Exception as exc:
                    log.exception("could not bring %s up to date with %s", source.name, folder)
                    note = f"{source.name}: {exc.__class__.__name__}: {exc}"[:240]
                    notes.append(note)
                    self._step(job, key, "failed", note)
                    continue
                folder_sync.store(s, source, out)
                if out.note:
                    notes.append(out.note)
                    self._step(job, key, "failed", out.note)
                else:
                    self._step(job, key, "done", out.detail)
        return notes

    def title_job(self, job: int) -> dict | None:
        with self._lock:
            return dict(self._titles[job]) if job in self._titles else None

    def _run_title(self, job: int, spec: dict) -> None:
        self._finish(job, self._title_state(job, spec))
        self.bump()

    def _title_state(self, job: int, spec: dict) -> dict:
        state = {"state": "done", "error": ""}
        try:
            with self.db.sessionmaker() as s:
                source = s.get(Source, spec["source_id"])
                if source is None:
                    raise ConnectorError("that app is not known any more")
                kind, library = None, None
                external_id, replaces = spec["external_id"], spec["replaces"]
                locate = spec["locate"]
                if external_id or replaces:
                    old = (
                        s.execute(
                            select(SourceItem).where(
                                SourceItem.source_id == source.id,
                                SourceItem.external_id.in_(
                                    [x for x in (external_id, replaces) if x]
                                ),
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if old is not None:
                        lib = s.get(Library, old.library_id) if old.library_id else None
                        kind, library = old.kind, lib.external_id if lib else None
                if locate:
                    kind, library = locate["kind"], locate["library"]
                kind = kind or spec.get("kind")
                if not kind:
                    raise ConnectorError("that item is not known any more")
                snap = None
                with connector_for(source, self.box) as connector:
                    if not external_id and locate:
                        external_id = connector.find_title(
                            locate["name"], kind, library, locate["path"]
                        )
                        if not external_id:
                            raise ConnectorError(
                                f"{source.name} has not picked this folder up yet; "
                                "try again in a minute"
                            )
                    try:
                        snap = connector.fetch_title(external_id, kind, library)
                    except ItemGone:
                        remove_title(s, source, external_id)
                if snap is not None:
                    apply_title(s, source, snap)
                if replaces and replaces != external_id:
                    remove_title(s, source, replaces)
                with self._match_lock:
                    finish_matching(s)
                    if spec["old_canonical"]:
                        move_history(s, source, external_id, spec["old_canonical"])
        except Exception as exc:  # shown in the dialog
            log.exception("could not read one title again")
            state = {"state": "error", "error": f"{exc.__class__.__name__}: {exc}"[:300]}
        return state

    def _run(self, run_id: int) -> None:
        """A read whose own error handling failed (say, its last save) must still end: the pool
        drops the exception, and a row left "running" stops every later read of that app."""
        try:
            self._run_once(run_id)
        except Exception as exc:
            log.exception("sync run %s ended without saving its result", run_id)
            with self.db.sessionmaker() as s:
                s.execute(
                    update(SyncRun)
                    .where(SyncRun.id == run_id, SyncRun.status == "running")
                    .values(
                        status="error",
                        error=f"{exc.__class__.__name__}: {exc}"[:2000],
                        finished_at=utcnow(),
                    )
                )
                s.commit()
            self.bump()

    def _run_once(self, run_id: int) -> None:
        last_progress = 0.0

        def progress(message: str) -> None:
            nonlocal last_progress
            now = time.monotonic()
            if now - last_progress < PROGRESS_INTERVAL:
                return
            last_progress = now
            with self.db.sessionmaker() as ps:
                ps.execute(
                    update(SyncRun).where(SyncRun.id == run_id).values(progress=message[:255])
                )
                ps.commit()

        with self.db.sessionmaker() as s:
            run = s.get(SyncRun, run_id)
            source = s.get(Source, run.source_id)
            started = time.monotonic()
            try:
                if run.mode == "watch":
                    watch_stats = self._sync_watch(s, source, progress)
                    run.stats = {
                        "watch": watch_stats,
                        "seconds": round(time.monotonic() - started, 1),
                    }
                    run.status = "ok"
                    run.progress = None
                    run.finished_at = utcnow()
                    s.commit()
                    self.bump()
                    if watch_stats and watch_stats.get("gone"):
                        # Items were replaced or removed since the last library read:
                        # read the library now so the new ones are known.
                        try:
                            self.start(source.id, "replaced", "full")
                        except Exception:  # never let this fail the read that found it
                            log.exception("could not queue a library read for %s", source.name)
                    return
                scan = None
                if run.trigger in SCAN_TRIGGERS:
                    scan = self._scan_first(s, source, progress)
                previous = previous_records(s, source)
                extra = {}
                if source.type == "disk":
                    # List folders first so only those in a library group are walked.
                    with connector_for(source, self.box) as connector:
                        libraries = connector.libraries()
                    with self._match_lock:
                        apply_libraries(s, source, libraries)
                        ensure_groups(s)
                    extra["scan_libraries"] = _grouped_disk_libraries(s, source)
                read_from = utcnow()
                with connector_for(source, self.box, **extra) as connector:
                    snapshot = connector.fetch_full(progress=progress, previous=previous)
                progress("Saving")
                stats = apply_snapshot(s, source, snapshot, read_from=read_from)
                with self._match_lock:
                    match_stats = finish_matching(s)
                watch_stats = self._sync_watch(s, source, progress)
                # Path maps may have been derived just now; count what's still unmapped.
                stats.files_unmapped = s.execute(
                    select(func.count()).where(
                        MediaFile.source_id == source.id,
                        MediaFile.removed_at.is_(None),
                        MediaFile.canonical_path.is_(None),
                    )
                ).scalar()
                run.stats = {
                    **stats.as_dict(),
                    **({"scan": scan} if scan else {}),
                    **(
                        {"episodes_with_files": snapshot.episodes_with_files}
                        if snapshot.episodes_with_files is not None
                        else {}
                    ),
                    "matching": match_stats,
                    "watch": watch_stats,
                    "seconds": round(time.monotonic() - started, 1),
                }
                run.status = "ok"
                run.progress = None
            except Exception as exc:  # shown to the user on the run
                log.exception("sync of %s failed", source.name)
                s.rollback()
                run = s.get(SyncRun, run_id)
                run.status = "error"
                run.error = f"{exc.__class__.__name__}: {exc}"[:2000]
            run.finished_at = utcnow()
            s.commit()
        self.bump()

    def _scan_first(self, s, source: Source, progress) -> dict | None:
        """Ask the app to scan every library and wait (up to SCAN_WAIT) for it to finish, so
        the read sees files added or deleted on the share. Skipped while writes are off."""
        with connector_for(source, self.box) as connector:
            rows = s.execute(select(Library).where(Library.source_id == source.id)).scalars()
            libs = [lib.external_id for lib in rows]
            if not connector.scan_all_calls(libs):
                return None
            if not flag(s, ENABLED_KEY) or flag(s, PAUSED_KEY):
                return {"note": "not asked to scan: writes are off or paused"}
            progress(f"Asking {source.name} to scan its libraries")
            started = time.monotonic()
            error, tokens = None, []
            try:
                tokens = connector.scan_all(libs)
            except ConnectorError as exc:
                error = str(exc)[:240]
            s.add(
                AuditLog(
                    at=utcnow(),
                    app_user_id=0,
                    source_id=source.id,
                    source_name=source.name,
                    user_name="—",
                    label=source.name,
                    item_id="every library",
                    op="scan",
                    result="error" if error else "applied",
                    detail=error,
                    why=f"Asked {source.name} to scan every library before a full sync",
                )
            )
            s.commit()
            if error:
                return {"note": f"scan request failed: {error}"}
            while tokens and time.monotonic() - started < SCAN_WAIT:
                try:
                    tokens = [t for t in tokens if not connector.scan_all_done(t)]
                except ConnectorError as exc:
                    log.info("could not check %s's scan: %s", source.name, exc)
                if tokens:
                    minutes = int(time.monotonic() - started) // 60
                    progress(f"{source.name} is scanning its libraries… {minutes} min")
                    time.sleep(SCAN_POLL * SETTLE_SCALE)
        seconds = round(time.monotonic() - started, 1)
        return {"seconds": seconds, **({"note": "stopped waiting for the scan"} if tokens else {})}

    def _sync_watch(self, s, source: Source, progress) -> dict | None:
        """Users and every readable user's watch state, after the items."""
        if Capability.READ_WATCH_STATE not in get_connector_class(source.type).capabilities:
            return None
        totals, errors, skipped = WatchStats(), [], 0
        with connector_for(source, self.box) as connector:
            if hasattr(connector, "account_keys"):
                held = list(
                    s.execute(
                        select(SourceAccountKey).where(SourceAccountKey.source_id == source.id)
                    ).scalars()
                )
                try:  # a key someone deleted in Silo no longer works: mark it, don't use it
                    live = {k["id"] for k in connector.list_keys()}
                except ConnectorError:
                    live = None
                for k in held:
                    if live is not None:
                        k.state = "ok" if k.key_id in live else "gone"
                        k.checked_at = utcnow()
                s.commit()
                connector.account_keys = {
                    k.account_id: self.box.decrypt(k.key_enc) for k in held if k.state == "ok"
                }
            progress("Reading users")
            users = connector.users()
            rows = apply_users(s, source, users)
            link_users(s)
            live = {
                connector.watch_key(ext)
                for ext in s.execute(
                    select(SourceItem.external_id).where(
                        SourceItem.source_id == source.id,
                        SourceItem.kind.in_(("episode", "movie")),
                        SourceItem.removed_at.is_(None),
                    )
                ).scalars()
            }
            readable = [rec for rec in users if rec.watch_readable]
            skipped = len(users) - len(readable)

            def checker(rec):
                """Ask the app whether items still exist, as this user. None means
                it could not be asked, which apply_watch treats as "change nothing"."""
                if not connector.checks_items:
                    return None

                def check(ids: list[str]) -> set[str] | None:
                    try:
                        return connector.items_exist(rec, ids)
                    except ConnectorError as exc:
                        log.warning("could not check items for %s: %s", rec.name, exc)
                        return None

                return check

            def read(rec):
                try:
                    return rec, connector.fetch_watch(rec), None
                except ConnectorError as exc:
                    return rec, None, exc

            # Users are read in parallel (the network is the slow part); the
            # results are stored one at a time, here, as they arrive.
            done = 0
            with ThreadPoolExecutor(min(WATCH_WORKERS, len(readable) or 1)) as pool:
                for rec, records, exc in pool.map(read, readable):
                    done += 1
                    progress(f"Reading watch state ({done} of {len(readable)})")
                    if exc is not None:
                        errors.append(f"{rec.name}: {exc}")
                        continue
                    totals.add(apply_watch(s, rows[rec.external_id], records, live, checker(rec)))
        return {
            "users": len(users),
            "readable": len(users) - skipped,
            "played": totals.played,
            "in_progress": totals.in_progress,
            "events": totals.events,
            "gone": totals.gone,
            "unchecked": totals.unchecked,
            "errors": errors,
        }

    def rematch(self) -> dict:
        with self._match_lock, self.db.sessionmaker() as s:
            result = finish_matching(s)
        self.bump()
        return result


DISK_AUTOCREATE_KEY = "disk_source_autocreate"


def ensure_disk_source(db: Database, settings) -> None:
    """Add the media share as a source the first time it is mounted, unless
    the user deleted it before."""
    from .models import AppSetting

    media = settings.media_dir
    try:
        has_folders = media.is_dir() and any(p.is_dir() for p in media.iterdir())
    except OSError:
        has_folders = False
    if not has_folders:
        return
    with db.sessionmaker() as s:
        declined = s.get(AppSetting, DISK_AUTOCREATE_KEY)
        if declined is not None and declined.value is False:
            return
        if s.execute(select(Source).where(Source.type == "disk")).first():
            return
        name = "Media share"
        if s.execute(select(Source).where(Source.name == name)).first():
            name = "Media share (disk)"
        s.add(Source(type="disk", name=name, base_url=str(media)))
        s.commit()
        log.info("Added the media share at %s as a source", media)
