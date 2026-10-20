"""Mutable runtime state: caches, counters, toggle flags and the API clients."""

import logging
import os
import time
from collections import OrderedDict

import config
from nio import AsyncClient
from telethon import TelegramClient

logger = logging.getLogger("tgmabr")


# Guard against handling the same album twice: album_id -> monotonic timestamp of the first
# message we saw. Entries live for ALBUM_GUARD_TTL_SECONDS, long enough that a lagging album
# message cannot start a second pass. This replaces the old "sleep(10) after every album"
# trick, which kept the guard open by blocking the whole update loop.
ALBUM_GUARD_TTL_SECONDS = 60
PROCESSED_ALBUMS = {}


def album_already_processed(album_id) -> bool:
    """True while this album is still inside its guard window. Also drops stale entries."""
    now = time.monotonic()
    for key, seen_at in list(PROCESSED_ALBUMS.items()):
        if now - seen_at > ALBUM_GUARD_TTL_SECONDS:
            del PROCESSED_ALBUMS[key]
    return album_id in PROCESSED_ALBUMS


class MediaDeduplicator:
    """In-memory deduplication cache with TTL and maximum size limit."""

    def __init__(self, enabled: bool = True, ttl_minutes: int = 15, max_size: int = 2000):
        self.enabled = enabled
        self.ttl_seconds = ttl_minutes * 60
        self.max_size = max_size
        self._cache = OrderedDict()  # key -> float (timestamp)

    def _cleanup(self, now: float):
        # Evict expired entries from the left (oldest inserted)
        while self._cache:
            oldest_ts = next(iter(self._cache.values()))
            if now - oldest_ts > self.ttl_seconds:
                self._cache.popitem(last=False)
            else:
                break
        # Evict oldest entries if capacity exceeded
        while len(self._cache) > self.max_size:
            self._cache.popitem(last=False)

    def is_duplicate(self, key: str | None) -> bool:
        if not self.enabled or not key:
            return False
        now = time.time()
        if key in self._cache:
            if now - self._cache[key] <= self.ttl_seconds:
                return True
            else:
                del self._cache[key]
        return False

    def mark_seen(self, key: str | None):
        if not self.enabled or not key:
            return
        now = time.time()
        self._cache[key] = now
        self._cache.move_to_end(key)
        self._cleanup(now)

    def unmark(self, key: str | None):
        if not self.enabled or not key:
            return
        self._cache.pop(key, None)


media_deduplicator = MediaDeduplicator(
    enabled=config.DEDUPLICATION_ENABLED,
    ttl_minutes=config.DEDUPLICATION_TTL_MINUTES,
    max_size=config.DEDUPLICATION_CACHE_SIZE
)

# Cache for resolved topic names, bounded like the deduplication cache: a long-running bot
# must not accumulate one entry per topic it has ever seen.
TOPIC_NAMES_CACHE_SIZE = 500
TOPIC_NAMES_CACHE = OrderedDict()


# Counters since start-up, surfaced by "!tmmb status" so that a classifier outage does not
# stay invisible between the individual error lines.
SAFETY_STATS = {"blocked": 0, "unavailable": 0}

# Counters for media that did not reach (all|some) destination rooms, also shown by "!tmmb status".
DELIVERY_STATS = {"failed": 0, "partial": 0}


SESSION_PATH = "session/tgmabr"


def restrict_session_permissions(session_base: str = SESSION_PATH) -> None:
    """Telethon's session file holds the account auth key, so only its owner may read it.

    The file is created by SQLiteSession when the client is constructed; the directory is
    usually a mounted volume, where the default umask (022) would leave it world-readable.
    """
    session_file = f"{session_base}.session"
    if not os.path.exists(session_file):
        return
    try:
        os.chmod(session_file, 0o600)
    except OSError as exc:
        logger.warning(f"Could not restrict permissions on {session_file}: {exc}")


tg_client = TelegramClient(
    SESSION_PATH, 
    config.TG_API_ID, 
    config.TG_API_HASH,
    device_model="Desktop",
    system_version="Windows 10",
    app_version="4.8.4"
)
restrict_session_permissions()

# Initialize global Matrix client session
matrix_client = AsyncClient(config.MATRIX_HOMESERVER)
matrix_client.access_token = config.MATRIX_ACCESS_TOKEN

