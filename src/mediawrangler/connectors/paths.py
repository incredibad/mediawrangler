"""Path prefix maps.

Each app sees the media share under its own container path (Sonarr /data/TV,
Radarr /movies, Silo /mnt/media...). A source's path map rewrites its paths to
one canonical form so files can be compared across apps. The longest matching
prefix wins, and prefixes only match on whole path components.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath


def _clean(path: str) -> str:
    path = path.replace("\\", "/")
    cleaned = str(PurePosixPath(path))
    return "/" if cleaned in ("", ".") else cleaned


@dataclass(frozen=True)
class PathMap:
    rules: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @classmethod
    def from_pairs(cls, pairs: Iterable[Iterable[str]]) -> PathMap:
        rules = []
        for pair in pairs or []:
            src, dst = list(pair)
            rules.append((_clean(src), _clean(dst)))
        rules.sort(key=lambda r: len(r[0]), reverse=True)
        return cls(tuple(rules))

    @classmethod
    def parse(cls, text: str) -> PathMap:
        """Parse 'source_prefix => canonical_prefix' lines (blank and # lines ignored)."""
        pairs = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=>" not in line:
                raise ValueError(f"line {lineno}: expected 'source => canonical'")
            src, dst = (part.strip() for part in line.split("=>", 1))
            if not src.startswith("/") or not dst.startswith("/"):
                raise ValueError(f"line {lineno}: both sides must be absolute paths")
            pairs.append((src, dst))
        return cls.from_pairs(pairs)

    def to_pairs(self) -> list[list[str]]:
        return [[src, dst] for src, dst in self.rules]

    def to_text(self) -> str:
        return "\n".join(f"{src} => {dst}" for src, dst in self.rules)

    def apply(self, path: str) -> str | None:
        """Canonical path, or None when no rule matches (a path-map failure)."""
        path = _clean(path)
        for src, dst in self.rules:
            if path == src:
                return dst
            prefix = src if src.endswith("/") else src + "/"
            if path.startswith(prefix):
                rest = path[len(prefix) :]
                return _clean(f"{dst}/{rest}")
        return None

    def reverse(self, canonical: str) -> str | None:
        """The app's own path for a canonical one (the first rule that fits), or None."""
        canonical = _clean(canonical)
        for src, dst in sorted(self.rules, key=lambda r: len(r[1]), reverse=True):
            prefix = dst if dst.endswith("/") else dst + "/"
            if canonical == dst:
                return src
            if canonical.startswith(prefix):
                return _clean(f"{src}/{canonical[len(prefix) :]}")
        return None


# What the disk scan counts as a video file. Anything the apps treat as video
# belongs here, or its folder shows up as "not on disk" (.ogm did, for early
# anime releases).
VIDEO_EXTENSIONS = frozenset(
    ".mkv .mp4 .m4v .avi .ts .m2ts .mts .mov .wmv .asf .mpg .mpeg .webm .flv .iso "
    ".ogm .ogv .divx .rmvb .3gp .vob".split()
)
# Folder names Jellyfin/Plex treat as extras, not as episodes or movies.
EXTRA_DIRS = frozenset(
    {
        "extras",
        "featurettes",
        "behind the scenes",
        "deleted scenes",
        "interviews",
        "scenes",
        "shorts",
        "trailers",
        "other",
        "clips",
        "samples",
        "sample",
    }
)
EXTRA_SUFFIXES = (
    "-trailer",
    "-sample",
    "-featurette",
    "-behindthescenes",
    "-deleted",
    "-interview",
    "-scene",
    "-short",
    "-extra",
    "-other",
)


# Files that should not sit in a library: a release is a video, not an archive or a program.
# A "WEB-DL" delivered as a .zipx is a common fake. Also unfinished downloads and the hidden
# "._" stubs macOS leaves beside a file copied to a network share.
ARCHIVE_EXTENSIONS = frozenset(".zip .zipx .rar .7z .tar .gz .tgz .bz2 .xz .cab".split())
EXECUTABLE_EXTENSIONS = frozenset(
    ".exe .msi .bat .cmd .com .scr .pif .lnk .vbs .vbe .js .jse .wsf .ps1 .jar .hta .chm .dll "
    ".apk .dmg .app .sh".split()
)
INCOMPLETE_EXTENSIONS = frozenset(
    ".part .partial .crdownload .!qb .!ut .tmp .aria2 .download".split()
)
ODD_LABELS = {
    "archive": "Archive",
    "executable": "Program or script",
    "incomplete": "Unfinished download",
    "macos": "macOS metadata stub",
}


def odd_kind(path: str) -> str | None:
    """Why a file does not belong in a library, or None: "macos", "archive", "executable"
    or "incomplete"."""
    p = PurePosixPath(path)
    ext = p.suffix.lower()
    if p.name.startswith("._"):
        # Only a stub that looks like a video is worth a flag; "._.DS_Store" and the like sit
        # in nearly every folder and mean nothing.
        return "macos" if ext in VIDEO_EXTENSIONS else None
    if ext in ARCHIVE_EXTENSIONS:
        return "archive"
    if ext in EXECUTABLE_EXTENSIONS:
        return "executable"
    if ext in INCOMPLETE_EXTENSIONS:
        return "incomplete"
    return None


def is_video(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in VIDEO_EXTENSIONS


def is_extra(path: str) -> bool:
    p = PurePosixPath(path.replace("\\", "/"))
    if any(part.lower() in EXTRA_DIRS for part in p.parts[:-1]):
        return True
    stem = p.stem.lower()
    return stem == "sample" or stem.endswith(EXTRA_SUFFIXES)
