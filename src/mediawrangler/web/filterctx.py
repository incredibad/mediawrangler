"""Template context shared by the comparison tables (see templates/_filterbar.html)."""

from typing import Annotated
from urllib.parse import urlencode

from pydantic import BeforeValidator

from ..filters import FilterSpec, specs_for_browser


def _blank_is_unset(value):
    """A form sends `source=` for "Any app"; that, or anything that isn't a
    whole number, just means the parameter isn't set. (Without this the page
    answered with a raw JSON validation error.)"""
    if value is None or isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


# Optional numeric query parameter for the comparison pages (group, user, source).
OptionalId = Annotated[int | None, BeforeValidator(_blank_is_unset)]


def filter_context(
    *,
    action: str,
    table_id: str,
    filters: dict[str, FilterSpec],
    default: str,
    flt: str,
    q: str,
    source: int | None,
    total: int,
    visible: int,
    hidden_fields: dict,
    source_columns: list[tuple[int, str]],
    noun_one: str,
    noun_many: str,
    agree_text: str,
) -> dict:
    """Everything the filter bar and empty-row partials need. Rows the starting
    filter excludes are marked hidden by the template, so nothing flashes before
    the script runs."""
    spec = filters[flt]
    return {
        "action": action,
        "table_id": table_id,
        "filters": filters,
        "filter": flt,
        "filter_specs": specs_for_browser(filters),
        "default_filter": default,
        "q": q,
        "source": source,
        "source_columns": source_columns,
        "hidden_fields": hidden_fields,
        "visible_count": visible,
        "total_rows": total,
        "noun_one": noun_one,
        "noun_many": noun_many,
        "agree_text": agree_text,
        "agree_empty": spec.good_when_empty and not q.strip(),
    }


def make_link(path: str, base: dict, default_filter: str):
    """link(**changes): this page's URL with some parameters changed. The
    default filter is left out so addresses stay short."""

    def link(**changes) -> str:
        params = {**base, **changes}
        params = {k: v for k, v in params.items() if v not in ("", None)}
        if params.get("filter") == default_filter:
            del params["filter"]
        return f"{path}?{urlencode(params)}"

    return link
