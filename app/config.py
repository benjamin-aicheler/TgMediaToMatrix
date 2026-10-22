"""Configuration: logging, environment parsing and every tunable value."""

import logging
import os
import shutil
import time

logger = logging.getLogger("tgmabr")


# Set up professional logging format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Module-level logger: records carry a name instead of landing anonymously on the root
# logger, so they stay attributable when handlers are added around it.
logger = logging.getLogger("tgmabr")

STARTUP_TIMESTAMP_MS = int(time.time() * 1000)

# Load and validate environment variables
def get_env_or_raise(key: str) -> str:
    val = os.environ.get(key)
    if not val or not val.strip():
        raise ValueError(f"Required environment variable '{key}' is missing or empty.")
    return val.strip()

def get_env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes", "on")

try:
    TG_API_ID = int(get_env_or_raise("TG_API_ID"))
    TG_API_HASH = get_env_or_raise("TG_API_HASH")
    MATRIX_HOMESERVER = get_env_or_raise("MATRIX_HOMESERVER").rstrip('/')
    MATRIX_ACCESS_TOKEN = get_env_or_raise("MATRIX_ACCESS_TOKEN")

    raw_room_ids = os.environ.get("MATRIX_ROOM_IDS", "").strip()
    if raw_room_ids:
        MATRIX_ROOM_IDS = [r.strip() for r in raw_room_ids.split(",") if r.strip()]
        if not MATRIX_ROOM_IDS:
            raise ValueError("MATRIX_ROOM_IDS was provided but contains no valid room IDs.")
        if os.environ.get("MATRIX_ROOM_ID"):
            logger.info("MATRIX_ROOM_IDS is set; deprecated MATRIX_ROOM_ID environment variable will be ignored.")
    else:
        legacy_room_id = os.environ.get("MATRIX_ROOM_ID", "").strip()
        if not legacy_room_id:
            raise ValueError("Missing required environment variable: MATRIX_ROOM_IDS (or deprecated MATRIX_ROOM_ID).")
        MATRIX_ROOM_IDS = [legacy_room_id]

    MATRIX_ROOM_ID = MATRIX_ROOM_IDS[0]  # Kept for backward compatibility
    ADMIN_MATRIX_USER_ID = os.environ.get("ADMIN_MATRIX_USER_ID", "").strip() or None
    ALLOW_NON_ADMIN_STOP = get_env_bool("ALLOW_NON_ADMIN_STOP", False)

    MAX_MEDIA_SIZE_MB = int(os.environ.get("MAX_MEDIA_SIZE_MB", "50"))
    MAX_MEDIA_SIZE_BYTES = MAX_MEDIA_SIZE_MB * 1024 * 1024

    try:
        MIN_IMAGE_SIZE_KB = int(os.environ.get("MIN_IMAGE_SIZE_KB", "0"))
    except ValueError:
        MIN_IMAGE_SIZE_KB = 0
    if MIN_IMAGE_SIZE_KB == 0 and os.environ.get("MIN_IMAGE_SIZE_MB"):
        try:
            MIN_IMAGE_SIZE_KB = int(float(os.environ.get("MIN_IMAGE_SIZE_MB")) * 1024)
        except ValueError:
            MIN_IMAGE_SIZE_KB = 0
    MIN_IMAGE_SIZE_BYTES = MIN_IMAGE_SIZE_KB * 1024

    try:
        MIN_VIDEO_SIZE_KB = int(os.environ.get("MIN_VIDEO_SIZE_KB", "0"))
    except ValueError:
        MIN_VIDEO_SIZE_KB = 0
    if MIN_VIDEO_SIZE_KB == 0 and os.environ.get("MIN_VIDEO_SIZE_MB"):
        try:
            MIN_VIDEO_SIZE_KB = int(float(os.environ.get("MIN_VIDEO_SIZE_MB")) * 1024)
        except ValueError:
            MIN_VIDEO_SIZE_KB = 0
    MIN_VIDEO_SIZE_BYTES = MIN_VIDEO_SIZE_KB * 1024

    ENABLE_IMAGES = get_env_bool("ENABLE_IMAGES", True)
    ENABLE_VIDEOS = get_env_bool("ENABLE_VIDEOS", True)

    OVERSIZED_VIDEO_ACTION = os.environ.get("OVERSIZED_VIDEO_ACTION", "split").strip().lower()
    if OVERSIZED_VIDEO_ACTION not in ("split", "compress", "skip"):
        raise ValueError(f"Invalid OVERSIZED_VIDEO_ACTION '{OVERSIZED_VIDEO_ACTION}'. Must be one of: 'split', 'compress', 'skip'.")

    try:
        OVERSIZED_VIDEO_MAX_INPUT_MB = int(os.environ.get("OVERSIZED_VIDEO_MAX_INPUT_MB", "250"))
        OVERSIZED_VIDEO_MAX_INPUT_MB = max(OVERSIZED_VIDEO_MAX_INPUT_MB, MAX_MEDIA_SIZE_MB)
    except ValueError:
        OVERSIZED_VIDEO_MAX_INPUT_MB = 250
    OVERSIZED_VIDEO_MAX_INPUT_BYTES = OVERSIZED_VIDEO_MAX_INPUT_MB * 1024 * 1024

    OVERSIZED_VIDEO_FALLBACK_COMPRESS = get_env_bool("OVERSIZED_VIDEO_FALLBACK_COMPRESS", False)

    # Telegram keeps lower-resolution renditions of large videos (alt_documents). With this
    # on, the bridge picks one of them instead of skipping the video: the highest resolution
    # that fits the upload limit is sent as-is, otherwise the highest that fits the processing
    # ceiling is downloaded and handled by OVERSIZED_VIDEO_ACTION. The renditions are
    # Telegram transcodes, so this trades quality for a single message.
    PREFER_ALTERNATE_RENDITION = get_env_bool("PREFER_ALTERNATE_RENDITION", True)

    try:
        VIDEO_COMPRESSION_MAX_HEIGHT = int(os.environ.get("VIDEO_COMPRESSION_MAX_HEIGHT", "720"))
    except ValueError:
        VIDEO_COMPRESSION_MAX_HEIGHT = 720

    VIDEO_COMPRESSION_PRESET = os.environ.get("VIDEO_COMPRESSION_PRESET", "veryfast").strip().lower()
    if VIDEO_COMPRESSION_PRESET not in ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"):
        VIDEO_COMPRESSION_PRESET = "veryfast"

    try:
        VIDEO_COMPRESSION_CRF = int(os.environ.get("VIDEO_COMPRESSION_CRF", "28"))
    except ValueError:
        VIDEO_COMPRESSION_CRF = 28

    try:
        VIDEO_COMPRESSION_THREADS = int(os.environ.get("VIDEO_COMPRESSION_THREADS", "2"))
    except ValueError:
        VIDEO_COMPRESSION_THREADS = 2

    FFMPEG_PATH = shutil.which("ffmpeg")
    if not FFMPEG_PATH:
        try:
            import imageio_ffmpeg
            FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, OSError, RuntimeError):
            pass

    if OVERSIZED_VIDEO_ACTION in ("split", "compress") and not FFMPEG_PATH:
        logger.warning("FFmpeg executable was not found on PATH. Oversized video processing will be skipped until ffmpeg is installed.")

    DEDUPLICATION_ENABLED = get_env_bool("DEDUPLICATION_ENABLED", True)
    try:
        DEDUPLICATION_TTL_MINUTES = int(os.environ.get("DEDUPLICATION_TTL_MINUTES", "15"))
        if DEDUPLICATION_TTL_MINUTES < 1:
            DEDUPLICATION_TTL_MINUTES = 15
    except ValueError:
        DEDUPLICATION_TTL_MINUTES = 15

    try:
        DEDUPLICATION_CACHE_SIZE = int(os.environ.get("DEDUPLICATION_CACHE_SIZE", "2000"))
        if DEDUPLICATION_CACHE_SIZE < 1:
            DEDUPLICATION_CACHE_SIZE = 2000
    except ValueError:
        DEDUPLICATION_CACHE_SIZE = 2000

    LLAMAGUARD_API_URL = os.environ.get("LLAMAGUARD_API_URL", "").strip() or None
    LLAMAGUARD_MODEL_NAME = os.environ.get("LLAMAGUARD_MODEL_NAME", "meta-llama/llama-guard-4-12b").strip()
    LLAMAGUARD_API_KEY = os.environ.get("LLAMAGUARD_API_KEY", "").strip() or None

    try:
        LLAMAGUARD_VIDEO_FRAMES = int(os.environ.get("LLAMAGUARD_VIDEO_FRAMES", "5"))
        LLAMAGUARD_VIDEO_FRAMES = max(LLAMAGUARD_VIDEO_FRAMES, 1)
    except ValueError:
        LLAMAGUARD_VIDEO_FRAMES = 5

    LLAMAGUARD_RANDOM_FRAMES = get_env_bool("LLAMAGUARD_RANDOM_FRAMES", True)

    LLAMAGUARD_CHECKS = set()
    checks_raw = os.environ.get("LLAMAGUARD_CHECKS", "")
    if checks_raw:
        for check in checks_raw.split(','):
            check = check.strip()
            if check:
                LLAMAGUARD_CHECKS.add(check.upper())

    LLAMAGUARD_REQUIRE_CHECKS = set()
    req_checks_raw = os.environ.get("LLAMAGUARD_REQUIRE_CHECKS", "")
    if req_checks_raw:
        for check in req_checks_raw.split(','):
            check = check.strip()
            if check:
                LLAMAGUARD_REQUIRE_CHECKS.add(check.upper())

    # Extract channels, ignoring empty elements, supporting channel_id:topic_id formats
    TG_CHANNELS_RAW = get_env_or_raise("TG_CHANNELS")
    TG_CHANNELS = []
    TG_TOPIC_FILTERS = {}  # maps base_chat_id -> set of topic_ids (ints)
    TG_UNFILTERED_CHANNELS = set()  # tracks channels configured to allow all topics

    import re
    for item in TG_CHANNELS_RAW.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            parts = item.split(":", 1)
            chan_part = parts[0].strip()
            topic_part = parts[1].strip()

            if chan_part.replace('-', '').isdigit():
                chan_id = int(chan_part)
            else:
                chan_username = chan_part.lstrip('@')
                if not re.match(r'^[a-zA-Z0-9_]+$', chan_username):
                    raise ValueError(f"Invalid Telegram channel username '{chan_part}' in '{item}'. Must contain only letters, numbers, and underscores.")
                chan_id = chan_username.lower()

            if topic_part.isdigit():
                topic_id = int(topic_part)
            else:
                raise ValueError(f"Invalid topic ID in '{item}'")

            TG_CHANNELS.append(chan_id)
            if chan_id not in TG_TOPIC_FILTERS:
                TG_TOPIC_FILTERS[chan_id] = set()
            TG_TOPIC_FILTERS[chan_id].add(topic_id)
        else:
            if item.replace('-', '').isdigit():
                chan_id = int(item)
            else:
                chan_username = item.lstrip('@')
                if not re.match(r'^[a-zA-Z0-9_]+$', chan_username):
                    raise ValueError(f"Invalid Telegram channel username '{item}'. Must contain only letters, numbers, and underscores.")
                chan_id = chan_username.lower()
            TG_CHANNELS.append(chan_id)
            TG_UNFILTERED_CHANNELS.add(chan_id)

    # De-duplicate TG_CHANNELS list while preserving order
    unique_channels = []
    for c in TG_CHANNELS:
        if c not in unique_channels:
            unique_channels.append(c)
    TG_CHANNELS = unique_channels

    if not TG_CHANNELS:
        raise ValueError("TG_CHANNELS must contain at least one valid channel identifier.")

except Exception as init_err:
    logger.critical(f"Configuration initialization failed: {init_err}")
    raise

