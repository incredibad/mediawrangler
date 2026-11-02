import pytest

from mediawrangler.connectors.paths import PathMap


def test_longest_prefix_wins():
    pm = PathMap.parse("/data => /media\n/data/Anime => /media/Anime")
    assert pm.apply("/data/Anime/One Piece/S01E01.mkv") == "/media/Anime/One Piece/S01E01.mkv"
    assert pm.apply("/data/TV/Sherlock/S01E01.mkv") == "/media/TV/Sherlock/S01E01.mkv"


def test_prefix_matches_whole_components_only():
    pm = PathMap.parse("/movies => /media/Movies")
    assert pm.apply("/movies4k/Heat (1995)/Heat.mkv") is None
    assert pm.apply("/movies/Heat (1995)/Heat.mkv") == "/media/Movies/Heat (1995)/Heat.mkv"
    assert pm.apply("/movies") == "/media/Movies"


def test_unmatched_is_a_path_map_failure():
    assert PathMap.parse("/movies => /media/Movies").apply("/elsewhere/file.mkv") is None


def test_normalises_slashes_and_dots():
    pm = PathMap.parse("/data/TV/ => /media/TV")
    assert pm.apply("/data//TV/./Show/ep.mkv") == "/media/TV/Show/ep.mkv"
    assert pm.apply("\\data\\TV\\Show\\ep.mkv") == "/media/TV/Show/ep.mkv"


def test_parse_ignores_comments_and_blank_lines():
    pm = PathMap.parse("# Sonarr\n\n/data/TV => /media/TV\n")
    assert pm.to_pairs() == [["/data/TV", "/media/TV"]]


@pytest.mark.parametrize("text", ["/data/TV /media/TV", "data => /media", "/data => media"])
def test_parse_rejects_bad_lines(text):
    with pytest.raises(ValueError):
        PathMap.parse(text)


def test_round_trip_text():
    pm = PathMap.parse("/movies4k => /media/Movies4K\n/movies => /media/Movies")
    assert PathMap.parse(pm.to_text()) == pm
    assert PathMap.from_pairs(pm.to_pairs()) == pm


def test_older_video_formats_count_as_video():
    from mediawrangler.connectors.paths import is_video

    for name in ("a.ogm", "a.OGM", "a.ogv", "a.divx", "a.avi", "a.mkv", "a.mp4", "a.rmvb"):
        assert is_video(f"/m/{name}"), name
    for name in ("a.srt", "a.nfo", "a.png", "a.jpg", "a.db", ".DS_Store", "a.txt"):
        assert not is_video(f"/m/{name}"), name
