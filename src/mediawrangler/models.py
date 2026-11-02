"""Database models.

Auth and connector instances (0.1); libraries, items, library groups,
matching and sync runs (0.2); files and scanned folders (0.3); users and
watch state (0.4).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow


class AdminUser(Base):
    """The single admin account. Only one row is ever created."""

    __tablename__ = "admin_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Time-step of the last accepted code, so a code can't be replayed.
    totp_last_counter: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RecoveryCode(Base):
    __tablename__ = "recovery_code"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("admin_user.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(String(255))
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


class WebSession(Base):
    """A browser session. Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "web_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("admin_user.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # False while waiting for the TOTP step of a login.
    fully_authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))


class SigninEvent(Base):
    """Every login attempt, step and outcome. Also drives lockout."""

    __tablename__ = "signin_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ip: Mapped[str] = mapped_column(String(64), index=True)
    username: Mapped[str] = mapped_column(String(64))
    # "password", "totp", "recovery_code"
    step: Mapped[str] = mapped_column(String(16))
    success: Mapped[bool] = mapped_column(Boolean)
    # e.g. "bad_password", "locked_ip", "locked_account", "bad_code"
    reason: Mapped[str | None] = mapped_column(String(32))
    user_agent: Mapped[str | None] = mapped_column(String(255))


class ApiKey(Base):
    """Keys for MediaWrangler's own API. Shown once, stored hashed."""

    __tablename__ = "api_key"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


class Source(Base):
    """A connector instance, e.g. one Sonarr. Secrets are encrypted with the master key."""

    __tablename__ = "source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    base_url: Mapped[str] = mapped_column(String(255))
    secret_enc: Mapped[str | None] = mapped_column(Text)
    # Shared secret that signs the webhooks an app sends to MediaWrangler (0.8).
    webhook_secret_enc: Mapped[str | None] = mapped_column(Text)
    # [[source_prefix, canonical_prefix], ...] longest prefix wins
    path_map: Mapped[list[Any]] = mapped_column(JSON, default=list)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Full sync: every `schedule_minutes`, or daily at `schedule_at` ("HH:MM", local
    # time); neither means manual only. `quick_*` is the watch-state-only sync.
    schedule_minutes: Mapped[int | None] = mapped_column(Integer)
    schedule_at: Mapped[str | None] = mapped_column(String(5))
    quick_minutes: Mapped[int | None] = mapped_column(Integer)
    quick_at: Mapped[str | None] = mapped_column(String(5))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_message: Mapped[str | None] = mapped_column(String(255))


class AppSetting(Base):
    """Key/value settings that belong in the export (e.g. the conflict-policy master)."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


# --- media model (0.2) ------------------------------------------------------


class Library(Base):
    """A library as one source sees it (a Jellyfin or Silo library)."""

    __tablename__ = "library"
    __table_args__ = (UniqueConstraint("source_id", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(255))
    # "series", "movie" or "other" (collections, music...); "other" is not ingested
    kind: Mapped[str] = mapped_column(String(16))
    paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    # True once the library was put in a group (or deliberately left out), so
    # auto-mapping on later syncs never undoes a choice.
    group_assigned: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)


class LibraryGroup(Base):
    """The organising unit: e.g. "TV" = Jellyfin TV + Silo Shows (+ arr roots, disk later).
    Groups start from the master's libraries."""

    __tablename__ = "library_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # "series" or "movie"
    # Sport has no external IDs, so it stays out of the presence matrix in v1.
    presence_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    # Where imported titles that no app has are listed: "show", "anime", "movie",
    # "none", or blank to guess from the group's name and kind.
    import_landing: Mapped[str | None] = mapped_column(String(8))


class LibraryGroupMember(Base):
    __tablename__ = "library_group_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("library_group.id", ondelete="CASCADE"), index=True
    )
    # A library belongs to at most one group.
    library_id: Mapped[int] = mapped_column(
        ForeignKey("library.id", ondelete="CASCADE"), unique=True
    )


class CanonicalItem(Base):
    """One real-world series or movie, independent of any app.

    `key` keeps the row stable across re-matching, e.g. "series:tvdb:81797".
    """

    __tablename__ = "canonical_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(160), unique=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(512))
    year: Mapped[int | None] = mapped_column(Integer)
    tvdb: Mapped[str | None] = mapped_column(String(32))
    tmdb: Mapped[str | None] = mapped_column(String(32))
    imdb: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SourceItem(Base):
    """A series, movie or episode as one source reports it."""

    __tablename__ = "source_item"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        Index("ix_source_item_source_kind", "source_id", "kind"),
        Index("ix_source_item_parent", "source_id", "parent_external_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(160))
    library_id: Mapped[int | None] = mapped_column(
        ForeignKey("library.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # "series", "movie", "episode"
    title: Mapped[str] = mapped_column(String(512))
    year: Mapped[int | None] = mapped_column(Integer)
    tvdb: Mapped[str | None] = mapped_column(String(32))
    tmdb: Mapped[str | None] = mapped_column(String(32))
    imdb: Mapped[str | None] = mapped_column(String(32))
    path: Mapped[str | None] = mapped_column(Text)
    # Episodes: their series' external_id and numbering.
    parent_external_id: Mapped[str | None] = mapped_column(String(160))
    season: Mapped[int | None] = mapped_column(Integer)
    episode: Mapped[int | None] = mapped_column(Integer)
    episode_end: Mapped[int | None] = mapped_column(Integer)
    # The source's own match state where it has one (Silo: "matched", "unmatched").
    source_status: Mapped[str | None] = mapped_column(String(32))
    details_fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Matching result (series and movies).
    canonical_id: Mapped[int | None] = mapped_column(
        ForeignKey("canonical_item.id", ondelete="SET NULL"), index=True
    )
    # "tvdb", "tmdb", "imdb", "title", "manual", or "new" (created the canonical item)
    match_method: Mapped[str | None] = mapped_column(String(16))
    # "high" or "low"; low never counts as present until confirmed.
    match_confidence: Mapped[str | None] = mapped_column(String(8))


class MatchDecision(Base):
    """A manual decision about pairing two source items. Survives re-ingest and
    is part of the settings export. Pairs are stored master-side first."""

    __tablename__ = "match_decision"
    __table_args__ = (UniqueConstraint("source_a_id", "external_a", "source_b_id", "external_b"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    source_a_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    external_a: Mapped[str] = mapped_column(String(160))
    source_b_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    external_b: Mapped[str] = mapped_column(String(160))
    decision: Mapped[str] = mapped_column(String(8))  # "confirm" or "reject"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncRun(Base):
    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(8))  # "full" or "watch"
    trigger: Mapped[str] = mapped_column(String(16))  # "manual", "schedule"
    status: Mapped[str] = mapped_column(String(16))  # "running", "ok", "error"
    progress: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


# --- physical media (0.3) ----------------------------------------------------


class MediaFile(Base):
    """A video file as one source reports it (arr, Jellyfin, or the disk scan)."""

    __tablename__ = "media_file"
    __table_args__ = (
        UniqueConstraint("source_id", "path"),
        Index("ix_media_file_library", "library_id", "removed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    library_id: Mapped[int | None] = mapped_column(ForeignKey("library.id", ondelete="SET NULL"))
    path: Mapped[str] = mapped_column(Text)
    # The path after the source's path map; None means no rule matched.
    canonical_path: Mapped[str | None] = mapped_column(Text, index=True)
    size: Mapped[int | None] = mapped_column(BigInteger)
    quality: Mapped[str | None] = mapped_column(String(64))
    item_external_id: Mapped[str | None] = mapped_column(String(160))
    season: Mapped[int | None] = mapped_column(Integer)
    episode: Mapped[int | None] = mapped_column(Integer)
    # Trailers, featurettes, samples: kept, but left out of comparisons.
    extra: Mapped[bool] = mapped_column(Boolean, default=False)
    # Why the file does not belong in a library: "archive", "executable", "incomplete"
    # or "macos" (see connectors.paths.odd_kind). Such a file is also an extra.
    flag: Mapped[str | None] = mapped_column(String(12))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)


class MediaRoot(Base):
    """A content folder a source scanned (or skipped), for sources that report
    folders rather than files (Silo)."""

    __tablename__ = "media_root"
    __table_args__ = (UniqueConstraint("source_id", "path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    library_id: Mapped[int | None] = mapped_column(
        ForeignKey("library.id", ondelete="SET NULL"), index=True
    )
    path: Mapped[str] = mapped_column(Text)
    canonical_path: Mapped[str | None] = mapped_column(Text)
    # Silo: "resolved", "unmatched", ... or "skipped" (with a reason)
    state: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(64))
    file_count: Mapped[int | None] = mapped_column(Integer)
    item_external_id: Mapped[str | None] = mapped_column(String(160))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- users and watch state (0.4) ----------------------------------------------


class AppUser(Base):
    """A person, independent of any app. Source users link to one."""

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # When Local was first filled from this person's sources (0.8.3).
    local_seeded_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Writing Local's state out to the apps (0.9). writes_mode and writes_since are
    # legacy since 0.10: the switches now live per app in UserSync. Only the optional
    # title filter, one show per line, that limits writes to matching titles, is current.
    writes_mode: Mapped[str] = mapped_column(String(4), default="off")
    writes_since: Mapped[datetime | None] = mapped_column(DateTime)
    writes_filter: Mapped[str | None] = mapped_column(Text)


class UserSync(Base):
    """What MediaWrangler may do for one person in one app (0.10). Both switches
    start off: an account is created only when asked, and watch history is written
    only while sync_history is on. `since` is when history was switched on; an
    unwatch is written only if it happened after that."""

    __tablename__ = "user_sync"
    __table_args__ = (UniqueConstraint("app_user_id", "source_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"), index=True)
    sync_account: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_history: Mapped[bool] = mapped_column(Boolean, default=False)
    since: Mapped[datetime | None] = mapped_column(DateTime)


class SourceUser(Base):
    """A user as one source knows it (a Jellyfin user, a Silo profile)."""

    __tablename__ = "source_user"
    __table_args__ = (UniqueConstraint("source_id", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    # The login account a profile belongs to (Silo); None where users are flat.
    account_id: Mapped[str | None] = mapped_column(String(128))
    # Whether MediaWrangler can read this user's watch state with its credentials
    # (Silo: only profiles of the key's own account).
    watch_readable: Mapped[bool] = mapped_column(Boolean, default=False)
    app_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"), index=True
    )
    # Libraries this user may open (Jellyfin); None means no restriction or unknown.
    library_ids: Mapped[list[str] | None] = mapped_column(JSON)
    # True once linked by hand (or deliberately unlinked): auto-linking leaves it alone.
    link_assigned: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set after the first watch-state read: later reads log changes against it.
    watch_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)


class SourceAccountKey(Base):
    """A key made in Silo for one login account, so its profiles' watch state can
    be read (Silo refuses another account's profile to any other key). Encrypted
    like every secret here; `key_id` is Silo's id for it, to revoke it later."""

    __tablename__ = "source_account_key"
    __table_args__ = (UniqueConstraint("source_id", "account_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(String(128))
    account_name: Mapped[str] = mapped_column(String(128))
    key_id: Mapped[str] = mapped_column(String(64))
    key_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # "ok", or "gone" when Silo no longer lists the key (someone deleted it): the
    # account then needs a new one. Checked at the start of every watch read.
    state: Mapped[str] = mapped_column(String(8), default="ok")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)


class WatchState(Base):
    """Latest watch state of one item for one source user. Only played or
    in-progress items are stored; no row means unplayed."""

    __tablename__ = "watch_state"
    __table_args__ = (UniqueConstraint("source_user_id", "item_external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_user_id: Mapped[int] = mapped_column(
        ForeignKey("source_user.id", ondelete="CASCADE"), index=True
    )
    # As the source reports it (Jellyfin item id; Silo content id).
    item_external_id: Mapped[str] = mapped_column(String(160))
    played: Mapped[bool] = mapped_column(Boolean, default=False)
    play_count: Mapped[int | None] = mapped_column(Integer)
    position_seconds: Mapped[float | None] = mapped_column()
    duration_seconds: Mapped[float | None] = mapped_column()
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Set when a full read first left this played item out. It counts as unwatched only
    # if a later read, a while on, leaves it out again (see watch.CONFIRM_AFTER).
    missing_since: Mapped[datetime | None] = mapped_column(DateTime)


class WatchEvent(Base):
    """Append-only change log: what changed between two reads. This is how an
    unwatch is told apart from an item that was never watched."""

    __tablename__ = "watch_event"
    __table_args__ = (Index("ix_watch_event_user_at", "source_user_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_user_id: Mapped[int] = mapped_column(ForeignKey("source_user.id", ondelete="CASCADE"))
    item_external_id: Mapped[str] = mapped_column(String(160))
    # "played", "unplayed", "progress"
    kind: Mapped[str] = mapped_column(String(16))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # The source's own time for the change, when it gives one.
    source_time: Mapped[datetime | None] = mapped_column(DateTime)
    position_seconds: Mapped[float | None] = mapped_column()
    # How MediaWrangler learned of it: "webhook" (the app said so as it happened), "poll"
    # (a read found it) or "write" (our own write, noted as it was sent). Older rows: None.
    via: Mapped[str | None] = mapped_column(String(8))


# --- history imports (0.6) ---------------------------------------------------


class WatchImport(Base):
    """A person's watch history from a file (Yamtrack CSV, Trakt export). It is
    compared like another app but is never written back to anything."""

    __tablename__ = "watch_import"
    __table_args__ = (UniqueConstraint("app_user_id", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # "yamtrack" or "trakt"
    filename: Mapped[str] = mapped_column(String(255))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WatchImportItem(Base):
    """One thing the file says was watched: a film, or one episode of a show."""

    __tablename__ = "watch_import_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("watch_import.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # "movie", "episode", "anime"
    title: Mapped[str] = mapped_column(String(512))  # the film, or the episode's show
    year: Mapped[int | None] = mapped_column(Integer)
    tvdb: Mapped[str | None] = mapped_column(String(32))
    tmdb: Mapped[str | None] = mapped_column(String(32))
    imdb: Mapped[str | None] = mapped_column(String(32))
    season: Mapped[int | None] = mapped_column(Integer)
    episode: Mapped[int | None] = mapped_column(Integer)
    watched_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Yamtrack anime: its MyAnimeList id and episodes watched (no episode list).
    mal: Mapped[str | None] = mapped_column(String(16))
    progress: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(255))


class WatchImportPlay(Base):
    """One time a file says an item was played. An item the file lists once can
    have many plays (Trakt records every rewatch); unwatching never deletes them."""

    __tablename__ = "watch_import_play"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("watch_import_item.id", ondelete="CASCADE"), index=True
    )
    played_at: Mapped[datetime | None] = mapped_column(DateTime)
    # What the file called it: Trakt "watch" / "scrobble" / "checkin", or "yamtrack".
    action: Mapped[str | None] = mapped_column(String(16))


# --- webhooks (0.8) -----------------------------------------------------------


class WebhookEvent(Base):
    """An event an app sent MediaWrangler (or one MediaWrangler derived from
    it), and what was done about it. Kept for the Activity page."""

    __tablename__ = "webhook_event"
    __table_args__ = (UniqueConstraint("source_id", "event_id"), Index("ix_webhook_event_at", "id"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    event: Mapped[str] = mapped_column(String(64))
    # The sender's id for the event; a repeat of one already stored is ignored.
    event_id: Mapped[str | None] = mapped_column(String(80))
    profile_external_id: Mapped[str | None] = mapped_column(String(128))
    profile_name: Mapped[str | None] = mapped_column(String(128))
    summary: Mapped[str] = mapped_column(String(255), default="")
    # "applied" (MediaWrangler's stored state changed), "logged", "ignored", "error"
    status: Mapped[str] = mapped_column(String(16), default="logged")
    note: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --- Local: MediaWrangler's own record of what each person has watched (0.8.3) --
#
# Keyed by the canonical title, not by any app's ids: ("ep", series, season,
# episode) for an episode, ("mv", film) for a film (season and episode 0).


class _LocalKey:
    app_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(2))  # "ep" or "mv"
    canonical_id: Mapped[int] = mapped_column(Integer)
    season: Mapped[int] = mapped_column(Integer, default=0)
    episode: Mapped[int] = mapped_column(Integer, default=0)


class LocalState(_LocalKey, Base):
    """Where a person is with one title now. Only played or in-progress titles
    have a row, as with WatchState."""

    __tablename__ = "local_state"
    __table_args__ = (UniqueConstraint("app_user_id", "kind", "canonical_id", "season", "episode"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    played: Mapped[bool] = mapped_column(Boolean, default=False)
    position_seconds: Mapped[float | None] = mapped_column()
    duration_seconds: Mapped[float | None] = mapped_column()
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LocalPlay(_LocalKey, Base):
    """A dated play. Unwatching never deletes these. Plays of one title on one
    day from different apps are one play, with every app named."""

    __tablename__ = "local_play"
    __table_args__ = (
        UniqueConstraint("app_user_id", "kind", "canonical_id", "season", "episode", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[str] = mapped_column(String(10), default="")  # YYYY-MM-DD; "" if undated
    played_at: Mapped[datetime | None] = mapped_column(DateTime)
    sources: Mapped[str] = mapped_column(String(255), default="")  # comma-separated names


class LocalEvent(_LocalKey, Base):
    """Every change to Local, and why."""

    __tablename__ = "local_event"
    __table_args__ = (Index("ix_local_event_user_at", "app_user_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # "played", "unplayed", "progress"
    change: Mapped[str] = mapped_column(String(16))
    # "seed" or the app the change came from
    reason: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LocalCursor(Base):
    """How far through one app user's change log (WatchEvent) Local has read.
    (`held_*` are unused: a hold was tried in 0.8.3 and removed in 0.8.4.)"""

    __tablename__ = "local_cursor"

    source_user_id: Mapped[int] = mapped_column(
        ForeignKey("source_user.id", ondelete="CASCADE"), primary_key=True
    )
    last_event_id: Mapped[int] = mapped_column(Integer, default=0)
    held_count: Mapped[int] = mapped_column(Integer, default=0)
    held_at: Mapped[datetime | None] = mapped_column(DateTime)


class WriteJob(Base):
    """One change waiting to be written to an app for one person: the outbox. It
    is drained straight away; a pause leaves the rest here. Sent or failed rows
    stay as the record, with the audit entry beside them."""

    __tablename__ = "write_job"
    __table_args__ = (Index("ix_write_job_status", "status", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    source_user_id: Mapped[int] = mapped_column(ForeignKey("source_user.id", ondelete="CASCADE"))
    # The app's own id for the item, and the Local key it stands for.
    item_id: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(2))
    canonical_id: Mapped[int] = mapped_column(Integer)
    season: Mapped[int] = mapped_column(Integer, default=0)
    episode: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str] = mapped_column(String(255), default="")
    op: Mapped[str] = mapped_column(String(8))  # "played" or "unplayed"
    played_at: Mapped[datetime | None] = mapped_column(DateTime)
    before: Mapped[str] = mapped_column(String(16), default="")
    # Why it was queued, in words ("Watched 13 Jan 2015, seen in Jellyfin"), for the audit log.
    why: Mapped[str | None] = mapped_column(String(200))
    # pending, sent, failed, or skipped (the target went away before it could be written)
    status: Mapped[str] = mapped_column(String(8), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditLog(Base):
    """Every write MediaWrangler has made (or tried) to an app: what it was,
    what the app said before, and the outcome."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    app_user_id: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[int] = mapped_column(Integer)
    source_name: Mapped[str] = mapped_column(String(64))
    user_name: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(255))
    item_id: Mapped[str] = mapped_column(String(160))
    op: Mapped[str] = mapped_column(String(8))
    before: Mapped[str] = mapped_column(String(16), default="")
    after: Mapped[str] = mapped_column(String(16), default="")
    # "applied", "skipped", "replaced" (the app's item was swapped for a new one) or "error"
    result: Mapped[str] = mapped_column(String(8))
    why: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(String(255))
    job_id: Mapped[int | None] = mapped_column(Integer)


class TrashItem(Base):
    """A file MediaWrangler moved to a trash folder on the share (never deleted outright).
    It can be restored until it is emptied by hand or its retention ends."""

    __tablename__ = "trash_item"
    __table_args__ = (Index("ix_trash_item_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_path: Mapped[str] = mapped_column(String(1024))
    trash_path: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int | None] = mapped_column(BigInteger)
    label: Mapped[str] = mapped_column(String(255))  # the file's name
    folder: Mapped[str] = mapped_column(String(1024))  # the show or film folder it was in
    why: Mapped[str] = mapped_column(String(200), default="")
    # The trash folder it was moved into (its own library's, or the one that was named).
    trash_root: Mapped[str | None] = mapped_column(String(1024))
    # "trashed", "restored" or "deleted" (the trash was emptied or retention ended)
    status: Mapped[str] = mapped_column(String(8), default="trashed")
    trashed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
