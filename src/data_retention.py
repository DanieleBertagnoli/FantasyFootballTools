"""Expire auction/note JSON documents 72 hours after creation.

The standalone worker shares only the two document directories with the web
app. It has no database connection and does not run inside Gunicorn workers.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import stat
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any


RETENTION_SECONDS = 72 * 60 * 60
SWEEP_INTERVAL_SECONDS = 60 * 60
DOCUMENT_DIRECTORIES = ("bid_manager_auctions", "notes_manager_notes")
HEARTBEAT_PATH = Path("/tmp/retention-worker-heartbeat")
logger = logging.getLogger(__name__)


def is_expired(document: Any, *, now: float | None = None, fallback_mtime: float | None = None) -> bool:
    """Use the stored creation timestamp; legacy/corrupt files fall back to mtime."""

    created = document.get("created_at") if isinstance(document, Mapping) else None
    try:
        parsed = datetime.fromisoformat(created)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        created_at = parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        created_at = fallback_mtime
    if created_at is None:
        return False
    return (time.time() if now is None else now) >= created_at + RETENTION_SECONDS


def cleanup_expired_files(data_root: Path, *, now: float | None = None) -> tuple[int, int]:
    """Delete only expired documents/orphaned temp files, without following links.

    Return (deleted, errors). Atomic replacements that happen during a scan
    are left for the next pass; one bad file does not stop the entire cleanup.
    """

    now = time.time() if now is None else now
    deleted = errors = 0
    for name in DOCUMENT_DIRECTORIES:
        directory = data_root / name
        if directory.is_symlink():
            logger.error("Refusing linked document directory: %s", name)
            errors += 1
            continue
        try:
            paths = list(directory.iterdir())
        except FileNotFoundError:
            continue
        except OSError:
            logger.exception("Cannot scan document directory: %s", name)
            errors += 1
            continue
        for path in paths:
            if path.suffix != ".json" and not (path.name.startswith(".") and path.suffix == ".tmp"):
                continue
            try:
                original = path.lstat()
                if not stat.S_ISREG(original.st_mode):
                    continue
                document = None
                if path.suffix == ".json":
                    # O_NOFOLLOW also protects against a symlink substituted
                    # after lstat. Application saves use atomic replacements.
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                        original = os.fstat(source.fileno())
                        try:
                            document = json.load(source)
                        except (ValueError, UnicodeError):
                            pass
                if not is_expired(document, now=now, fallback_mtime=original.st_mtime):
                    continue
                latest = path.lstat()
                if (original.st_ino, original.st_mtime_ns, original.st_size) != (
                    latest.st_ino, latest.st_mtime_ns, latest.st_size
                ):
                    continue
                path.unlink()
                deleted += 1
            except FileNotFoundError:
                continue
            except OSError:
                logger.exception("Cannot clean document: %s/%s", name, path.name)
                errors += 1
    return deleted, errors


def run_worker(data_root: Path) -> None:
    stopped = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stopped.set())
    logger.info("Retention worker started: 72 hours from creation; scan every 60 seconds")
    while not stopped.is_set():
        deleted, errors = cleanup_expired_files(data_root)
        if deleted or errors:
            logger.info("Retention scan: deleted=%s errors=%s", deleted, errors)
        if not errors:
            HEARTBEAT_PATH.write_text(str(time.time()), encoding="ascii")
        stopped.wait(SWEEP_INTERVAL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path(__file__).resolve().parent.parent / "persistent_data")
    parser.add_argument("--once", action="store_true", help="Run a single cleanup pass and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.once:
        deleted, errors = cleanup_expired_files(args.data_root)
        logger.info("Retention scan: deleted=%s errors=%s", deleted, errors)
        raise SystemExit(1 if errors else 0)
    run_worker(args.data_root)


if __name__ == "__main__":
    main()
