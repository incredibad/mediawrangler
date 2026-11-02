"""The media share as it is on disk (the container mounts the whole share at /media).

Top-level folders are libraries; with MW_MEDIA_FOLDERS set, only those named there. Only
folders that belong to a library group
are walked (set by the sync manager in `options["scan_libraries"]`), so Music
or camera folders on the same share are never read.
"""

import os
from pathlib import Path

from .base import (
    Capability,
    ConnectionTest,
    Connector,
    FileRecord,
    LibraryRecord,
    ProgressFn,
    Snapshot,
    _no_progress,
    register,
)
from .paths import is_extra, is_video, odd_kind


@register
class DiskConnector(Connector):
    type = "disk"
    display_name = "Media share (disk)"
    role = "disk"
    capabilities = frozenset({Capability.LIST_FILES})
    needs_secret = False
    uses_path_map = False
    location_kind = "path"
    url_placeholder = "/media"
    key_help = ""

    @property
    def root(self) -> Path:
        return Path(self.config.base_url)

    def test_connection(self) -> ConnectionTest:
        try:
            if not self.root.is_dir():
                return ConnectionTest(False, f"{self.root} is not a directory in the container")
            folders = sum(
                1 for e in os.scandir(self.root) if e.is_dir() and not e.name.startswith(".")
            )
        except OSError as exc:
            return ConnectionTest(False, f"cannot read {self.root}: {exc.strerror}")
        writable = os.access(self.root, os.W_OK)
        note = " (read-write: file changes can be switched on)" if writable else " (read-only)"
        return ConnectionTest(True, f"{folders} folders under {self.root}{note}")

    def libraries(self) -> list[LibraryRecord]:
        from ..config import get_settings

        allowed = get_settings().media_folder_names
        out = []
        with os.scandir(self.root) as entries:
            for e in sorted(entries, key=lambda e: e.name.lower()):
                if allowed and e.name not in allowed:
                    continue
                if e.is_dir() and not e.name.startswith((".", "@", "#")):
                    out.append(
                        LibraryRecord(
                            external_id=e.name,
                            name=e.name,
                            kind="folder",
                            paths=[str(self.root / e.name)],
                        )
                    )
        return out

    @staticmethod
    def _walk(root: str, library_external_id: str | None) -> list[FileRecord]:
        """The video files under a folder, and any unusual files (archives, programs,
        unfinished downloads, macOS stubs), which are kept but flagged and never counted."""
        files: list[FileRecord] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "@", "#"))]
            for name in filenames:
                path = os.path.join(dirpath, name)
                kind = odd_kind(path)
                if kind is None and not is_video(path):
                    continue
                try:
                    size = os.stat(path).st_size
                except OSError:
                    size = None
                files.append(
                    FileRecord(
                        path=path,
                        library_external_id=library_external_id,
                        size=size,
                        extra=kind is not None or is_extra(path),
                        flag=kind,
                    )
                )
        return files

    def scan_folder(self, folder: str, library_external_id: str | None) -> list[FileRecord]:
        """The files under one folder (a show or film), as a full scan would list them."""
        return self._walk(folder, library_external_id)

    def fetch_full(self, progress: ProgressFn = _no_progress, previous=None) -> Snapshot:
        libraries = self.libraries()
        wanted = set(self.config.options.get("scan_libraries") or [])
        files: list[FileRecord] = []
        for lib in libraries:
            if lib.external_id not in wanted:
                continue
            progress(f"Scanning {lib.name}")
            files.extend(self._walk(lib.paths[0], lib.external_id))
        return Snapshot(libraries=libraries, items=[], files=files)
