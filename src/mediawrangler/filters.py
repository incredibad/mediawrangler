"""Filters shared by the comparison tables (Watch states, Presence, Physical media).

Every row carries a set of flags saying what is true of it. A filter is "the
row has any of these flags", so the same definition runs on the server (to
pick the rows shown at first) and in the browser (static/table-filter.js),
where filtering is instant. Search uses the same folding in both places.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .matching import normalise_title


@dataclass(frozen=True)
class FilterSpec:
    """`flags`: a row matches if it has any of them; none means every row.
    `per_source`: choosing an app narrows each flag to that app
    (`differs` -> `differs:<source id>`).
    `good_when_empty`: an empty result is good news ("every app agrees")."""

    label: str
    flags: tuple[str, ...] = ()
    per_source: bool = False
    good_when_empty: bool = False

    def wanted(self, source_id: int | None) -> frozenset[str]:
        if source_id and self.per_source:
            return frozenset(f"{flag}:{source_id}" for flag in self.flags)
        return frozenset(self.flags)

    def matches(self, row_flags: set[str], source_id: int | None = None) -> bool:
        wanted = self.wanted(source_id)
        return not wanted or bool(row_flags & wanted)


def specs_for_browser(filters: dict[str, FilterSpec]) -> dict[str, dict]:
    return {
        key: {"flags": list(s.flags), "perSource": s.per_source, "good": s.good_when_empty}
        for key, s in filters.items()
    }


def fold(text: str) -> str:
    """Search form of a title: ASCII, lower case, words separated by single spaces.
    The browser applies the same folding (see static/table-filter.js)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def search_text(title: str) -> str:
    """What a row is searched against: the title as written and without a leading article."""
    folded = fold(title)
    plain = normalise_title(title)
    return folded if folded == plain else f"{folded} {plain}"


def apply_filter(rows, filters: dict[str, FilterSpec], flt: str, q: str = "", source_id=None):
    """Rows (anything with .flags and .search) matching a filter and a search."""
    spec = filters[flt]
    out = [r for r in rows if spec.matches(r.flags, source_id)]
    if q:
        needle = fold(q)
        out = [r for r in out if needle in r.search]
    return out
