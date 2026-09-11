"""Feature 10.1 — noticing a file in a broker's folder the moment it lands.

sftp_poller used to look in every folder on a five-minute timer, so a file sat
unseen for up to five minutes before anything happened to it. This asks the
operating system to say when something appears instead — FSEvents on macOS,
inotify on Linux, through the `watchdog` library — and wakes the collector.

It only ever WAKES the collector. Whether a file is finished, landing it and
moving it all stay in sftp_poller.collect_route, so a file noticed here and a
file found by the backup sweep go through exactly the same code.

THE ONE THING A WATCHER KNOWS THAT A SWEEP CANNOT: a rename. A careful SFTP
client uploads under a temporary name and renames when the last byte is
written — WinSCP to `name.xlsx.filepart`, scripts to `.name.xlsx`. The rename
IS the "finished" signal, so such a file is collected at once rather than
waiting out SFTP_QUIET_SECONDS. A file written under its real name still waits
for the quiet window, because mid-upload it looks exactly like a finished file
that happens to be shorter.

LIMITS worth knowing:
  * Local disks only. A network mount (NFS, SMB, Azure Files) delivers no
    events, which is why sftp_poller keeps a slow backup sweep.
  * The OS can drop events under a burst. Same answer.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - depends on the environment
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]

log = logging.getLogger("kavachio.sftp_watch")

# Names an upload wears while it is still going. The collector skips these, and
# a rename AWAY from one is how we know the upload finished.
TEMP_SUFFIXES = (".filepart", ".part", ".partial", ".tmp", ".crdownload", ".upload")


def is_temp_name(name: str) -> bool:
    """A dotfile, or a name a client uploads under before renaming it."""
    return name.startswith(".") or name.lower().endswith(TEMP_SUFFIXES)


# Files that arrived by a rename from a temporary name, with the size and mtime
# they had at that moment. Checked again at collection: if either has changed,
# somebody is still writing and the quiet window applies after all.
_finished: dict[str, tuple[int, int, float]] = {}
_finished_lock = threading.Lock()
_FINISHED_TTL_SECONDS = 3600


def _key(path: Path) -> str:
    return os.path.realpath(path)


def note_finished(path: Path) -> None:
    try:
        st = path.stat()
    except OSError:
        return
    now = time.monotonic()
    with _finished_lock:
        _finished[_key(path)] = (st.st_size, st.st_mtime_ns, now)
        for k in [k for k, (_, _, at) in _finished.items()
                  if now - at > _FINISHED_TTL_SECONDS]:
            del _finished[k]


def finished_by_rename(path: Path) -> bool:
    """True when this exact file arrived by a rename from a temporary name."""
    with _finished_lock:
        rec = _finished.get(_key(path))
    if rec is None:
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    return st.st_size > 0 and (st.st_size, st.st_mtime_ns) == rec[:2]


def forget(path: Path) -> None:
    with _finished_lock:
        _finished.pop(_key(path), None)


class _Wake(FileSystemEventHandler):
    """Turns filesystem events into "go and look" — and nothing more."""

    def __init__(self, wake: Callable[[], None]):
        super().__init__()
        self._wake = wake

    def _consider(self, path: str, renamed_from: Optional[str] = None) -> None:
        p = Path(os.fsdecode(path))
        # Only a broker's `incoming` counts. The collector's own moves land in
        # processed / held / rejected / quarantine, so they do not wake it back up.
        if p.parent.name != "incoming" or is_temp_name(p.name):
            return
        if renamed_from is not None and is_temp_name(Path(os.fsdecode(renamed_from)).name):
            note_finished(p)
        self._wake()

    def on_created(self, event):
        if not event.is_directory:
            self._consider(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._consider(event.dest_path, renamed_from=event.src_path)

    def on_closed(self, event):
        # inotify only: the writer closed the file. Still subject to the quiet
        # window, but it is the right moment to look again.
        if not event.is_directory:
            self._consider(event.src_path)


class FolderWatcher:
    """Watches the whole SFTP root, so a route added later needs no setup."""

    def __init__(self, root: Path, wake: Callable[[], None]):
        self.root = root
        self._wake = wake
        self._observer = None

    def start(self) -> bool:
        if Observer is None:
            log.warning("watchdog is not installed — SFTP folders are only checked "
                        "on the timer (pip install watchdog)")
            return False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            observer = Observer()
            observer.schedule(_Wake(self._wake), str(self.root), recursive=True)
            observer.start()
        except Exception as exc:
            log.warning("could not watch %s (%s) — checking on the timer instead",
                        self.root, exc)
            return False
        self._observer = observer
        log.info("sftp watcher: watching %s (%s)", self.root, type(observer).__name__)
        return True

    @property
    def alive(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    def stop(self) -> None:
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
