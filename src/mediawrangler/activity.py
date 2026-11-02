"""Activity: sync runs and the audit log (what apps told us, what we told them), as
rows for the filter tables."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import get_connector_class
from .db import utcnow
from .filters import FilterSpec, search_text
from .models import AppUser, AuditLog, Source, SourceItem, SourceUser, SyncRun, WatchEvent
from .writes import RETRIES

# Newest first. Sync runs are one per sync, so this is weeks of history; the
# change log is capped so a very busy log can't make the page unusable.
RUN_LIMIT = 500
EVENT_LIMIT = 2000

RUN_FILTERS = {
    "all": FilterSpec("All runs", ("any",), per_source=True),
    "failed": FilterSpec("Failed", ("error",), per_source=True),
    "full": FilterSpec("Full syncs", ("full",), per_source=True),
    "watch": FilterSpec("Watch-state syncs", ("watch",), per_source=True),
    "manual": FilterSpec("Started by hand", ("manual",), per_source=True),
    "schedule": FilterSpec("Scheduled", ("schedule",), per_source=True),
}
AUDIT_FILTERS = {
    "all": FilterSpec("Everything", ("any",), per_source=True),
    "in": FilterSpec("Received from apps", ("in",), per_source=True),
    "out": FilterSpec("Sent to apps", ("out",), per_source=True),
    "played": FilterSpec("Marked played", ("played",), per_source=True),
    "unplayed": FilterSpec("Marked unplayed", ("unplayed",), per_source=True),
    "progress": FilterSpec("Progress moved", ("progress",), per_source=True),
    "accounts": FilterSpec("Accounts created", ("account",), per_source=True),
    "matches": FilterSpec("Matches changed", ("match",), per_source=True),
    "requests": FilterSpec(
        "Scans, refreshes and folder fixes",
        ("scan", "refresh", "add", "override", "import", "forget"),
        per_source=True,
    ),
    "files": FilterSpec("File changes", ("trash", "restore", "purge"), per_source=True),
    "ignored": FilterSpec("Ignored echoes", ("ignored",), per_source=True),
    "problems": FilterSpec("Errors", ("error",), per_source=True),
    "replaced": FilterSpec("Replaced files", ("replaced",), per_source=True),
    "skipped": FilterSpec("Skipped", ("skipped",), per_source=True),
}


@dataclass
class ActivityRow:
    flags: set[str]
    search: str
    data: object
    extra: dict = field(default_factory=dict)


def _flags(source_id: int, *names: str) -> set[str]:
    return {f"{n}:{source_id}" for n in ("any", *names)} | {"any", *names}


@dataclass
class ActivityView:
    rows: list[ActivityRow]
    sources: list[tuple[int, str]]
    truncated: bool
    summary: dict[str, int] = field(default_factory=dict)  # the audit log: the last day


def build_runs(db: Session) -> ActivityView:
    found = db.execute(
        select(SyncRun, Source)
        .join(Source, Source.id == SyncRun.source_id)
        .order_by(SyncRun.id.desc())
        .limit(RUN_LIMIT + 1)
    ).all()
    rows, sources = [], {}
    for run, source in found[:RUN_LIMIT]:
        sources[source.id] = source.name
        rows.append(
            ActivityRow(
                _flags(source.id, run.status, run.mode, run.trigger),
                search_text(f"{source.name} {run.error or ''}"),
                run,
                {"source": source},
            )
        )
    return ActivityView(
        rows, sorted(sources.items(), key=lambda i: i[1].lower()), len(found) > RUN_LIMIT
    )


def _titles(db: Session, source_ids: set[int]) -> dict[tuple[int, str], str]:
    """Display titles for watch keys: 'Series S01E02 Title' for episodes."""
    out: dict[tuple[int, str], str] = {}
    for source in db.execute(select(Source).where(Source.id.in_(source_ids))).scalars():
        key_of = get_connector_class(source.type).watch_key
        items = db.execute(
            select(
                SourceItem.external_id,
                SourceItem.kind,
                SourceItem.title,
                SourceItem.parent_external_id,
                SourceItem.season,
                SourceItem.episode,
            ).where(SourceItem.source_id == source.id, SourceItem.removed_at.is_(None))
        ).all()
        series = {i.external_id: i.title for i in items if i.kind == "series"}
        for item in items:
            if item.kind == "series":
                continue
            title = item.title
            if item.kind == "episode":
                num = f"S{item.season or 0:02d}E{item.episode or 0:02d}"
                title = f"{series.get(item.parent_external_id, '?')} {num} {item.title}".strip()
            out[(source.id, key_of(item.external_id))] = title
    return out


def build_changes(db: Session) -> ActivityView:
    found = db.execute(
        select(WatchEvent, SourceUser, Source, AppUser)
        .join(SourceUser, SourceUser.id == WatchEvent.source_user_id)
        .join(Source, Source.id == SourceUser.source_id)
        .outerjoin(AppUser, AppUser.id == SourceUser.app_user_id)
        .order_by(WatchEvent.id.desc())
        .limit(EVENT_LIMIT + 1)
    ).all()
    found, truncated = found[:EVENT_LIMIT], len(found) > EVENT_LIMIT
    titles = _titles(db, {source.id for _, _, source, _ in found})
    rows, sources = [], {}
    for event, su, source, person in found:
        sources[source.id] = source.name
        who = person.name if person else su.name
        title = titles.get((source.id, event.item_external_id), "(item no longer in the app)")
        rows.append(
            ActivityRow(
                _flags(source.id, event.kind, "in"),
                search_text(f"{who} {title} {source.name}"),
                event,
                {
                    "source": source,
                    "who": who,
                    "title": title,
                    "at": event.at,
                    "person_id": su.app_user_id,
                },
            )
        )
    return ActivityView(rows, sorted(sources.items(), key=lambda i: i[1].lower()), truncated)


def build_audit(db: Session) -> ActivityView:
    """Everything MediaWrangler heard from the apps (the watch change log) and
    everything it did to them (the write audit log), newest first."""
    received = build_changes(db)
    rows, sources = list(received.rows), dict(received.sources)
    found = db.execute(
        select(AuditLog).order_by(AuditLog.id.desc()).limit(EVENT_LIMIT + 1)
    ).scalars()
    sent = list(found)
    _mark_echoes(received.rows, sent)
    truncated = received.truncated or len(sent) > EVENT_LIMIT
    for entry in sent[:EVENT_LIMIT]:
        sources[entry.source_id] = entry.source_name
        rows.append(
            ActivityRow(
                _flags(entry.source_id, "out", entry.result, entry.op),
                search_text(f"{entry.user_name} {entry.label} {entry.source_name}"),
                entry,
                {"at": entry.at, "why": sent_why(entry)},
            )
        )
    rows.sort(key=lambda r: r.extra["at"], reverse=True)
    if len(rows) > EVENT_LIMIT:
        rows, truncated = rows[:EVENT_LIMIT], True
    since = utcnow() - timedelta(hours=24)
    counts: dict[str, int] = {"in": 0, "out": 0}
    for row in rows:
        if row.extra["at"] < since:
            continue
        counts["in" if "in" in row.flags else "out"] += 1
        if "out" in row.flags:
            for result in ("applied", "replaced", "skipped", "error"):
                if result in row.flags:
                    counts[result] = counts.get(result, 0) + 1
    return ActivityView(
        rows, sorted(sources.items(), key=lambda i: i[1].lower()), truncated, counts
    )


ECHO_WINDOW = timedelta(seconds=30)


def sent_why(entry: AuditLog) -> str:
    """Why a write was made, or, if it was not made, what happened and what that means."""
    app, detail = entry.source_name, entry.detail or ""
    low = detail.lower()
    if entry.result == "applied":
        return entry.why or ""
    if entry.result == "replaced" or (entry.result == "error" and "http 404" in low):
        return (
            f"{app} no longer has this item: its file was most likely replaced (an upgrade), "
            "so the write was dropped and the library re-read. Local still has the watch."
        )
    if entry.result == "skipped":
        if "user" in low and "no longer in the app" in low:
            return f"Skipped before sending: this user is no longer in {app}."
        if "no longer lists this item" in low or "no longer in the app" in low:
            return (
                f"Skipped before sending: {app} no longer lists this item (replaced or "
                "removed), so there was nothing to write to."
            )
        if "cannot open" in low:
            return (
                f"Skipped before sending: this user cannot open the item's library in {app}, "
                "so the app would refuse the write."
            )
        if "local no longer says" in low:
            return (
                "Skipped before sending: Local changed its mind while this was queued "
                "(for example it was unwatched again), so there was nothing left to write."
            )
        return f"Skipped before sending: {detail}"
    if entry.result == "error":
        return (
            f"Could not write to {app} after {RETRIES} tries ({detail}). It is tried again "
            f"in about an hour if Local and {app} still disagree."
        )
    return entry.why or detail


def received_why(app: str, who: str, event: WatchEvent, echo: bool) -> str:
    """What a received change was and how it came about, in words."""
    if echo:
        return (
            f"{app} reported it back after MediaWrangler's own write, "
            "so there was nothing new to apply"
        )
    if event.kind == "played":
        did = "played it"
    elif event.kind == "unplayed":
        did = "unwatched it"
    else:
        did = f"stopped at {int((event.position_seconds or 0) // 60)} min"
    if event.via == "webhook":
        return f"Reported by webhook via {app}: {who} {did}"
    if event.via == "poll":
        return f"Found by a read of {app}: {who} {did}"
    return f"Reported by {app}: {who} {did}"


def _mark_echoes(received: list[ActivityRow], sent: list[AuditLog]) -> None:
    """A change an app reports within moments of a write we made to it is that write
    coming back: it changes nothing, and the log says so."""
    written: dict[tuple, list] = {}
    for entry in sent:
        if entry.result == "applied" and entry.op in ("played", "unplayed"):
            key = (entry.app_user_id, entry.source_id, entry.item_id, entry.op)
            written.setdefault(key, []).append(entry.at)
    for row in received:
        event = row.data
        key = (row.extra["person_id"], row.extra["source"].id, event.item_external_id, event.kind)
        echo = event.via == "write" or any(
            abs(event.at - at) <= ECHO_WINDOW for at in written.get(key, ())
        )
        row.extra["echo"] = echo
        row.extra["why"] = received_why(row.extra["source"].name, row.extra["who"], event, echo)
        if echo:
            row.flags |= {"ignored", f"ignored:{row.extra['source'].id}"}
