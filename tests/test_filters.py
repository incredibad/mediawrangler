"""The filter definitions shared by Watch states, Presence and Physical media."""

from types import SimpleNamespace

from mediawrangler import physical, presence, watchdiff
from mediawrangler.filters import FilterSpec, apply_filter, fold, search_text, specs_for_browser


def row(flags, title="X"):
    return SimpleNamespace(flags=set(flags), search=search_text(title))


def test_spec_wanted_flags():
    spec = FilterSpec("Differs", ("differs", "specials"), per_source=True)
    assert spec.wanted(None) == {"differs", "specials"}
    assert spec.wanted(7) == {"differs:7", "specials:7"}
    plain = FilterSpec("In progress", ("in_progress",))
    assert plain.wanted(7) == {"in_progress"}  # an app can't narrow a plain filter
    assert FilterSpec("All").wanted(7) == frozenset()


def test_matches_any_flag_and_all_titles_match_everything():
    spec = FilterSpec("Differs", ("differs", "specials"), per_source=True)
    # Rows carry the plain flag and the per-app one (differs, differs:2).
    both = {"differs", "differs:2"}
    assert spec.matches(both, None) and spec.matches({"specials", "specials:3"}, None)
    assert not spec.matches({"watched"}, None)
    assert spec.matches(both, 2) and not spec.matches(both, 3)
    assert FilterSpec("All").matches(set(), 5)


def test_apply_filter_combines_filter_and_search():
    rows = [
        row({"differs", "differs:2"}, "Sherlock"),
        row({"differs"}, "The Stand"),
        row(set(), "Fargo"),
    ]
    filters = {"d": FilterSpec("D", ("differs",), True), "all": FilterSpec("All")}
    assert len(apply_filter(rows, filters, "all")) == 3
    assert len(apply_filter(rows, filters, "d")) == 2
    assert [r.search for r in apply_filter(rows, filters, "d", "stand")] == ["the stand stand"]
    assert len(apply_filter(rows, filters, "d", source_id=2)) == 1


def test_fold_matches_what_the_browser_does():
    assert fold("Pokémon") == "pokemon"
    assert fold("Law & Order: SVU") == "law and order svu"
    assert fold("日本 Anime") == "anime"
    assert search_text("The Stand") == "the stand stand"
    assert search_text("Sherlock") == "sherlock"


def test_every_page_has_a_default_first_and_everything_last():
    for module, default in ((watchdiff, "diffs"), (presence, "issues"), (physical, "issues")):
        keys = list(module.FILTERS)
        assert module.DEFAULT_FILTER == default and keys[0] == default
        assert keys[-1] == "all" and not module.FILTERS["all"].flags
        # Everything but "all" is a kind of issue (or, for Watch, a view): an
        # empty issue list is good news, which the page says so.
    for module in (presence, physical):
        assert all(s.good_when_empty for k, s in module.FILTERS.items() if k != "all")


def test_specs_for_browser_shape():
    specs = specs_for_browser(watchdiff.FILTERS)
    assert specs["diffs"] == {"flags": ["differs"], "perSource": True, "good": True}
    assert specs["all"] == {"flags": [], "perSource": False, "good": False}
