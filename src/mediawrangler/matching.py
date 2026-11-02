"""Matching source items (series and movies) to canonical items.

Rules (defaults):
- Series: TVDB, then TMDB, then IMDb, then title + year (low confidence).
- Movies: TMDB, then IMDb, then title + year (low confidence).
- Two items match on the highest-precedence ID type they both have. If they
  both have a higher-precedence type and it differs, they are different
  titles even when a lower one agrees.
- Title matches are low confidence and never count as present until
  confirmed, unless the series has exactly the seasons and episodes the same
  title has in another app. Manual decisions (confirm/reject a pair) always win.

The master source is processed first, so its items define canonical items.
Canonical items have a stable `key` derived from the item that created them.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .models import CanonicalItem, MatchDecision, SourceItem

PRECEDENCE = {
    "series": ("tvdb", "tmdb", "imdb"),
    "movie": ("tmdb", "imdb", "tvdb"),
}
MATCHED_KINDS = tuple(PRECEDENCE)
TITLE_YEAR_TOLERANCE = 1
EPISODE_CONFIRM_MIN = 2  # a series needs this many episodes to be confirmed by them


def normalise_title(title: str) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ")
    text = re.sub(r"\(\s*\d{4}\s*\)\s*$", "", text)  # trailing "(2004)"
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    text = re.sub(r"^(the|a|an) ", "", text)
    return text


@dataclass
class MatchItem:
    id: int
    source_id: int
    external_id: str
    kind: str
    title: str
    year: int | None = None
    tvdb: str | None = None
    tmdb: str | None = None
    imdb: str | None = None

    def ids(self) -> dict[str, str]:
        return {t: v for t in ("tvdb", "tmdb", "imdb") if (v := getattr(self, t))}


@dataclass
class Canon:
    key: str
    kind: str
    title: str
    year: int | None
    ids: dict[str, str] = field(default_factory=dict)
    members: list[MatchItem] = field(default_factory=list)

    @property
    def sources(self) -> set[int]:
        return {m.source_id for m in self.members}


@dataclass
class MatchResult:
    canon_key: str
    method: str
    confidence: str


Pair = frozenset[tuple[int, str]]


def _conflicts(order: tuple[str, ...], a: dict[str, str], b: dict[str, str], upto: str) -> bool:
    """Do a and b disagree on any ID type ranked above `upto` that both have?"""
    for id_type in order:
        if id_type == upto:
            return False
        if id_type in a and id_type in b and a[id_type] != b[id_type]:
            return True
    return False


def _conflicts_any(a: dict[str, str], b: dict[str, str]) -> bool:
    return any(t in a and t in b and a[t] != b[t] for t in a)


def match_items(
    items: Iterable[MatchItem],
    source_order: list[int],
    confirmed: set[Pair] | None = None,
    rejected: set[Pair] | None = None,
) -> tuple[dict[int, MatchResult], dict[str, Canon]]:
    confirmed = confirmed or set()
    rejected = rejected or set()
    rank = {sid: i for i, sid in enumerate(source_order)}
    ordered = sorted(
        items, key=lambda m: (rank.get(m.source_id, len(rank)), m.source_id, m.external_id)
    )

    canons: dict[str, Canon] = {}
    by_id: dict[tuple[str, str, str], Canon] = {}  # (kind, id_type, value)
    by_title: dict[tuple[str, str], list[Canon]] = defaultdict(list)
    canon_of: dict[tuple[int, str], Canon] = {}
    results: dict[int, MatchResult] = {}

    def item_key(m: MatchItem) -> tuple[int, str]:
        return (m.source_id, m.external_id)

    def is_rejected(m: MatchItem, canon: Canon) -> bool:
        return any(frozenset({item_key(m), item_key(o)}) in rejected for o in canon.members)

    def attach(m: MatchItem, canon: Canon, method: str, confidence: str) -> None:
        canon.members.append(m)
        if canon.year is None and m.year:
            canon.year = m.year
        for id_type, value in m.ids().items():
            if id_type not in canon.ids:
                canon.ids[id_type] = value
                by_id.setdefault((m.kind, id_type, value), canon)
        canon_of[item_key(m)] = canon
        results[m.id] = MatchResult(canon.key, method, confidence)

    for m in ordered:
        order = PRECEDENCE[m.kind]
        mids = m.ids()

        # 1. A confirmed pair wins over everything.
        partner = next(
            (
                canon_of[k]
                for pair in confirmed
                if item_key(m) in pair
                for k in pair
                if k != item_key(m) and k in canon_of
            ),
            None,
        )
        if partner is not None and partner.kind == m.kind:
            attach(m, partner, "manual", "high")
            continue

        # 2. IDs in precedence order.
        found = None
        for id_type in order:
            if id_type not in mids:
                continue
            canon = by_id.get((m.kind, id_type, mids[id_type]))
            if canon is None or is_rejected(m, canon):
                continue
            if _conflicts(order, mids, canon.ids, id_type):
                continue
            found = (canon, id_type)
            break
        if found:
            attach(m, found[0], found[1], "high")
            continue

        # 3. Title + year, low confidence.
        norm = normalise_title(m.title)
        candidate = None
        if norm:
            for canon in by_title[(m.kind, norm)]:
                if m.source_id in canon.sources or is_rejected(m, canon):
                    continue
                if _conflicts_any(mids, canon.ids):
                    continue
                if m.year and canon.year and abs(m.year - canon.year) > TITLE_YEAR_TOLERANCE:
                    continue
                candidate = canon
                break
        if candidate is not None:
            attach(m, candidate, "title", "low")
            continue

        # 4. Nothing matched: this item starts a new canonical item.
        first = next(((t, mids[t]) for t in order if t in mids), None)
        key = (
            f"{m.kind}:{first[0]}:{first[1]}"
            if first
            else f"{m.kind}:src{m.source_id}:{m.external_id}"
        )
        if key in canons:  # same ID, but blocked above (conflict/reject): keep apart
            key = f"{m.kind}:src{m.source_id}:{m.external_id}"
        canon = Canon(key=key, kind=m.kind, title=m.title, year=m.year)
        canons[key] = canon
        if norm:
            by_title[(m.kind, norm)].append(canon)
        attach(m, canon, "new", "high")

    return results, canons


def _decisions(db: Session) -> tuple[set[Pair], set[Pair]]:
    confirmed, rejected = set(), set()
    for d in db.execute(select(MatchDecision)).scalars():
        pair = frozenset({(d.source_a_id, d.external_a), (d.source_b_id, d.external_b)})
        (confirmed if d.decision == "confirm" else rejected).add(pair)
    return confirmed, rejected


def run_matching(db: Session, source_order: list[int]) -> dict[str, int]:
    """Re-match every live series and movie and store the result."""
    rows = (
        db.execute(
            select(SourceItem).where(
                SourceItem.kind.in_(MATCHED_KINDS), SourceItem.removed_at.is_(None)
            )
        )
        .scalars()
        .all()
    )
    items = [
        MatchItem(r.id, r.source_id, r.external_id, r.kind, r.title, r.year, r.tvdb, r.tmdb, r.imdb)
        for r in rows
    ]
    confirmed, rejected = _decisions(db)
    results, canons = match_items(items, source_order, confirmed, rejected)

    existing = {c.key: c for c in db.execute(select(CanonicalItem)).scalars()}
    for key, canon in canons.items():
        row = existing.get(key)
        if row is None:
            row = CanonicalItem(key=key, kind=canon.kind)
            db.add(row)
            existing[key] = row
        row.title, row.year = canon.title, canon.year
        row.tvdb, row.tmdb, row.imdb = (canon.ids.get(t) for t in ("tvdb", "tmdb", "imdb"))
    db.flush()

    by_row = {r.id: r for r in rows}
    for item_id, result in results.items():
        row = by_row[item_id]
        row.canonical_id = existing[result.canon_key].id
        row.match_method = result.method
        row.match_confidence = result.confidence

    confirmed_by_episodes = _confirm_by_episodes(db, rows)

    # Removed items keep no link; canonical items nobody points at go away.
    db.execute(
        update(SourceItem)
        .where(SourceItem.kind.in_(MATCHED_KINDS), SourceItem.removed_at.is_not(None))
        .values(canonical_id=None, match_method=None, match_confidence=None)
    )
    from .unfiled import referenced_ids, reunite

    reunite(db)  # a title an import knew first, now in an app under another key
    held = referenced_ids(db)  # what Local holds plays for is never cleaned away
    stale = [row.id for key, row in existing.items() if key not in canons and row.id not in held]
    if stale:
        db.execute(delete(CanonicalItem).where(CanonicalItem.id.in_(stale)))
    db.commit()
    low = sum(1 for r in results.values() if r.confidence == "low")
    return {
        "items": len(items),
        "canonical": len(canons),
        "low_confidence": low - confirmed_by_episodes,
    }


def _confirm_by_episodes(db: Session, rows: list[SourceItem]) -> int:
    """A series matched by title alone counts as confirmed when another app's series under
    the same title has exactly the same seasons and episodes (at least EPISODE_CONFIRM_MIN):
    two shows that merely share a name do not number their episodes alike."""
    series = [r for r in rows if r.kind == "series" and r.canonical_id is not None]
    low = [r for r in series if r.match_confidence == "low"]
    if not low:
        return 0
    canons = {r.canonical_id for r in low}
    group = [r for r in series if r.canonical_id in canons]
    wanted = {(r.source_id, r.external_id) for r in group}
    numbers: dict[tuple[int, str], set[tuple[int, int]]] = defaultdict(set)
    for source_id, parent, season, episode in db.execute(
        select(
            SourceItem.source_id,
            SourceItem.parent_external_id,
            SourceItem.season,
            SourceItem.episode,
        ).where(
            SourceItem.kind == "episode",
            SourceItem.removed_at.is_(None),
            SourceItem.season.is_not(None),
            SourceItem.episode.is_not(None),
            SourceItem.source_id.in_({s for s, _ in wanted}),
        )
    ):
        if (source_id, parent) in wanted:
            numbers[(source_id, parent)].add((season, episode))
    confirmed = 0
    for row in low:
        mine = numbers.get((row.source_id, row.external_id), set())
        if len(mine) < EPISODE_CONFIRM_MIN:
            continue
        if any(
            o.canonical_id == row.canonical_id
            and o.source_id != row.source_id
            and numbers.get((o.source_id, o.external_id)) == mine
            for o in group
        ):
            row.match_method, row.match_confidence = "episodes", "high"
            confirmed += 1
    db.flush()
    return confirmed
