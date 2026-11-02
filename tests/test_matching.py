"""The matching engine, with known disparities as fixtures."""

from mediawrangler.matching import MatchItem, match_items, normalise_title

JF, SILO = 1, 2
_next_id = iter(range(1, 10_000))


def item(source, ext, title, year=None, kind="series", **ids) -> MatchItem:
    return MatchItem(next(_next_id), source, ext, kind, title, year, **ids)


def run(items, confirmed=None, rejected=None):
    results, canons = match_items(items, [JF, SILO], confirmed, rejected)
    return results, canons


def same(results, a, b) -> bool:
    return results[a.id].canon_key == results[b.id].canon_key


def test_title_normalisation():
    assert normalise_title("The Office (US)") == normalise_title("the office us")
    assert normalise_title("Law & Order") == normalise_title("Law and Order")
    assert normalise_title("Pokémon") == "pokemon"
    assert normalise_title("Heat (1995)") == "heat"


def test_match_on_highest_shared_id():
    a = item(JF, "j1", "3 Body Problem", 2024, tvdb="411959", tmdb="108545", imdb="tt13016388")
    b = item(SILO, "s1", "3 Body Problem", 2024, tvdb="411959")
    results, canons = run([a, b])
    assert same(results, a, b)
    assert results[b.id].method == "tvdb" and results[b.id].confidence == "high"
    assert len(canons) == 1


def test_lower_id_used_when_higher_missing():
    a = item(JF, "j1", "Some Show", 2020, tvdb="1", tmdb="500")
    b = item(SILO, "s1", "Some Show", 2020, tmdb="500")
    results, _ = run([a, b])
    assert same(results, a, b) and results[b.id].method == "tmdb"


def test_the_stand_wrong_tvdb_is_not_matched():
    """Headline case: Silo matched The Stand to the 2020 miniseries. Both have
    a TVDB ID and they differ, so a shared IMDb or title doesn't join them."""
    jf = item(JF, "j1", "The Stand", 1994, tvdb="71267", imdb="tt0108941")
    silo = item(SILO, "s1", "The Stand", 2020, tvdb="372051", imdb="tt0108941")
    results, canons = run([jf, silo])
    assert not same(results, jf, silo)
    assert len(canons) == 2


def test_one_piece_anime_vs_live_action_by_title_is_not_matched():
    """Name-only matching must not pair the 1999 anime with the 2023 live action."""
    anime = item(JF, "j1", "One Piece", 1999, tvdb="81797")
    live = item(SILO, "s1", "One Piece", 2023)
    results, _ = run([anime, live])
    assert not same(results, anime, live)


def test_title_match_is_low_confidence():
    a = item(JF, "j1", "Prison School", 2015, tvdb="295068")
    b = item(SILO, "s1", "Prison School", 2015)
    results, _ = run([a, b])
    assert same(results, a, b)
    assert results[b.id].method == "title" and results[b.id].confidence == "low"


def test_title_match_allows_one_year_drift_only():
    a = item(JF, "j1", "Heat", 1995, kind="movie", tmdb="949")
    b = item(SILO, "s1", "Heat", 1996, kind="movie")
    c = item(SILO, "s2", "Heat", 2010, kind="movie")
    results, _ = run([a, b, c])
    assert same(results, a, b) and not same(results, a, c)


def test_title_match_refused_when_ids_conflict():
    a = item(JF, "j1", "Avatar", 2009, kind="movie", tmdb="19995")
    b = item(SILO, "s1", "Avatar", 2009, kind="movie", tmdb="76600")
    results, _ = run([a, b])
    assert not same(results, a, b)


def test_one_pace_local_folders_stay_separate():
    """Silo's 37 unmatched One Pace arc folders have no IDs and titles that
    don't look like 'One Piece', so they are 'not in master' items."""
    op = item(JF, "j1", "One Piece", 1999, tvdb="81797")
    arcs = [item(SILO, f"local-{i}", f"[One Pace][{i}] Arc {i} [1080p]") for i in range(3)]
    results, canons = run([op, *arcs])
    assert len(canons) == 4
    assert all(not same(results, op, arc) for arc in arcs)


def test_movie_precedence_prefers_tmdb():
    a = item(JF, "j1", "Film", 2001, kind="movie", tmdb="1", imdb="tt1")
    b = item(SILO, "s1", "Film", 2001, kind="movie", tmdb="2", imdb="tt1")
    results, _ = run([a, b])
    assert not same(results, a, b)  # TMDB is the top movie ID and it differs


def test_series_and_movies_never_mix():
    a = item(JF, "j1", "Fargo", 2014, kind="series", imdb="tt2802850")
    b = item(SILO, "s1", "Fargo", 2014, kind="movie", imdb="tt2802850")
    results, _ = run([a, b])
    assert not same(results, a, b)


def test_master_defines_canonical_key_and_order_independent():
    a = item(JF, "j1", "Show", 2000, tvdb="10")
    b = item(SILO, "s1", "Show", 2000, tvdb="10", tmdb="20")
    r1, c1 = run([b, a])
    assert list(c1) == ["series:tvdb:10"]
    assert c1["series:tvdb:10"].ids == {"tvdb": "10", "tmdb": "20"}


def test_duplicates_within_a_source_share_one_canonical():
    a = item(JF, "j1", "Show", 2000, tvdb="10")
    b = item(JF, "j2", "Show", 2000, tvdb="10")
    results, canons = run([a, b])
    assert same(results, a, b) and len(canons) == 1


def test_confirm_decision_forces_match():
    a = item(JF, "j1", "Spirited Away", 2001, kind="movie", tmdb="129")
    b = item(SILO, "s1", "Sen to Chihiro no Kamikakushi", 2001, kind="movie")
    pair = frozenset({(JF, "j1"), (SILO, "s1")})
    results, _ = run([a, b], confirmed={pair})
    assert same(results, a, b) and results[b.id].method == "manual"
    assert results[b.id].confidence == "high"


def test_reject_decision_blocks_title_and_id_matches():
    a = item(JF, "j1", "Prison School", 2015, tvdb="295068")
    b = item(SILO, "s1", "Prison School", 2015)
    pair = frozenset({(JF, "j1"), (SILO, "s1")})
    results, _ = run([a, b], rejected={pair})
    assert not same(results, a, b)

    c = item(JF, "j2", "X", 2000, tvdb="5")
    d = item(SILO, "s2", "X", 2000, tvdb="5")
    results, _ = run([c, d], rejected={frozenset({(JF, "j2"), (SILO, "s2")})})
    assert not same(results, c, d)
