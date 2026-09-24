import os
import io
import time
import math
import shutil
import tempfile
import subprocess
import asyncio
import logging
import hashlib
from collections import OrderedDict
from telethon import TelegramClient, events
from nio import AsyncClient, UploadResponse, RoomSendResponse, RoomSendError, RoomMessageText

# Set up professional logging format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

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
            logging.info("MATRIX_ROOM_IDS is set; deprecated MATRIX_ROOM_ID environment variable will be ignored.")
    else:
        legacy_room_id = os.environ.get("MATRIX_ROOM_ID", "").strip()
        if not legacy_room_id:
            raise ValueError("Missing required environment variable: MATRIX_ROOM_IDS (or deprecated MATRIX_ROOM_ID).")
        MATRIX_ROOM_IDS = [legacy_room_id]

    MATRIX_ROOM_ID = MATRIX_ROOM_IDS[0]  # Kept for backward compatibility
    ADMIN_MATRIX_USER_ID = os.environ.get("ADMIN_MATRIX_USER_ID", "").strip() or None
    ALLOW_NON_ADMIN_STOP = get_env_bool("ALLOW_NON_ADMIN_STOP", False)

    MAX_MEDIA_SIZE_MB = int(os.environ.get("MAX_MEDIA_SIZE_MB", 50))
    MAX_MEDIA_SIZE_BYTES = MAX_MEDIA_SIZE_MB * 1024 * 1024

    try:
        MIN_IMAGE_SIZE_KB = int(os.environ.get("MIN_IMAGE_SIZE_KB", 0))
    except ValueError:
        MIN_IMAGE_SIZE_KB = 0
    if MIN_IMAGE_SIZE_KB == 0 and os.environ.get("MIN_IMAGE_SIZE_MB"):
        try:
            MIN_IMAGE_SIZE_KB = int(float(os.environ.get("MIN_IMAGE_SIZE_MB")) * 1024)
        except ValueError:
            MIN_IMAGE_SIZE_KB = 0
    MIN_IMAGE_SIZE_BYTES = MIN_IMAGE_SIZE_KB * 1024

    try:
        MIN_VIDEO_SIZE_KB = int(os.environ.get("MIN_VIDEO_SIZE_KB", 0))
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
        if OVERSIZED_VIDEO_MAX_INPUT_MB < MAX_MEDIA_SIZE_MB:
            OVERSIZED_VIDEO_MAX_INPUT_MB = MAX_MEDIA_SIZE_MB
    except ValueError:
        OVERSIZED_VIDEO_MAX_INPUT_MB = 250
    OVERSIZED_VIDEO_MAX_INPUT_BYTES = OVERSIZED_VIDEO_MAX_INPUT_MB * 1024 * 1024

    OVERSIZED_VIDEO_FALLBACK_COMPRESS = get_env_bool("OVERSIZED_VIDEO_FALLBACK_COMPRESS", False)

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
        except Exception:
            pass

    if OVERSIZED_VIDEO_ACTION in ("split", "compress") and not FFMPEG_PATH:
        logging.warning("FFmpeg executable was not found on PATH. Oversized video processing will be skipped until ffmpeg is installed.")

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
        if LLAMAGUARD_VIDEO_FRAMES < 1:
            LLAMAGUARD_VIDEO_FRAMES = 1
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
    logging.critical(f"Configuration initialization failed: {init_err}")
    raise

# Cache for already processed album IDs
PROCESSED_ALBUMS = set()


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
            oldest_key, oldest_ts = next(iter(self._cache.items()))
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
    enabled=DEDUPLICATION_ENABLED,
    ttl_minutes=DEDUPLICATION_TTL_MINUTES,
    max_size=DEDUPLICATION_CACHE_SIZE
)


def get_telegram_media_id(message) -> str | None:
    """Extracts a unique Telegram internal identifier for the media if present."""
    if not message:
        return None
    photo = getattr(message, 'photo', None)
    if photo and hasattr(photo, 'id'):
        return f"tg_photo_{photo.id}"
    document = getattr(message, 'document', None)
    if document and hasattr(document, 'id'):
        return f"tg_doc_{document.id}"
    return None


def compute_sha256(data: bytes) -> str:
    """Compute SHA-256 hexadecimal digest for raw bytes."""
    return hashlib.sha256(data).hexdigest()



def is_channel_and_topic_allowed(chat_id, chat_entity, topic_id=None):
    """Check if a given channel ID and topic ID match the configured filtering rules."""
    chat_username = getattr(chat_entity, 'username', None) if chat_entity else None
    chat_username_lower = chat_username.lower() if chat_username else None

    # If the channel itself was configured without any topic filter, allow all topics.
    if chat_id in TG_UNFILTERED_CHANNELS or (chat_username_lower and chat_username_lower in TG_UNFILTERED_CHANNELS):
        return True

    allowed_topics = None
    if chat_id in TG_TOPIC_FILTERS:
        allowed_topics = TG_TOPIC_FILTERS[chat_id]
    elif chat_username_lower and chat_username_lower in TG_TOPIC_FILTERS:
        allowed_topics = TG_TOPIC_FILTERS[chat_username_lower]

    if allowed_topics is not None:
        return topic_id in allowed_topics

    return True

# Cache for resolved topic names
TOPIC_NAMES_CACHE = {}

async def get_topic_name(client, chat_entity, topic_id):
    if not topic_id:
        return None
    cache_key = (chat_entity.id, topic_id)
    if cache_key in TOPIC_NAMES_CACHE:
        return TOPIC_NAMES_CACHE[cache_key]
    
    try:
        from telethon.tl.functions.channels import GetForumTopicsByIDRequest
        result = await client(GetForumTopicsByIDRequest(
            channel=chat_entity,
            topics=[topic_id]
        ))
        if result and result.topics:
            name = result.topics[0].title
            TOPIC_NAMES_CACHE[cache_key] = name
            return name
    except Exception as e:
        logging.debug(f"Could not fetch topic name for topic {topic_id} in {chat_entity.id}: {e}")
        
    return f"Topic {topic_id}"


def extract_video_frames(video_bytes: bytes, max_frames: int = 5, use_random: bool = True) -> list:
    """Extracts up to max_frames frames from the video as JPEG bytes in-memory (either evenly spaced or randomly selected)"""
    try:
        import av
        from PIL import Image
        import io
        import random
    except ImportError:
        logging.error("PygAV (av) or Pillow (PIL) package is missing! Please install them to enable video frame safety moderation.")
        return []

    frames_bytes = []
    container = None
    try:
        container = av.open(io.BytesIO(video_bytes))
        video_stream = container.streams.video[0]
        
        total_frames = video_stream.frames
        if not total_frames:
            # Estimate from duration and frame rate if available
            duration = video_stream.duration
            time_base = video_stream.time_base
            average_rate = video_stream.average_rate
            if duration is not None and time_base is not None and average_rate is not None:
                try:
                    total_seconds = float(duration * time_base)
                    total_frames = int(total_seconds * float(average_rate))
                except Exception:
                    total_frames = 0
        
        # If total_frames is not available, try to decode up to some reasonable limit or read first frames
        if not total_frames or total_frames <= 1:
            for frame in container.decode(video=0):
                img = frame.to_image()
                img_bytes_io = io.BytesIO()
                img.save(img_bytes_io, format="JPEG")
                frames_bytes.append(img_bytes_io.getvalue())
                if len(frames_bytes) >= max_frames:
                    break
            return frames_bytes

        # If we have total_frames, select the target frame indices
        if total_frames <= max_frames:
            target_indices = list(range(total_frames))
        elif use_random:
            target_indices = sorted(random.sample(range(total_frames), max_frames))
        else:
            step = total_frames / max_frames
            target_indices = [int((i + 0.5) * step) for i in range(max_frames)]
            # Keep bounds
            target_indices = [max(0, min(total_frames - 1, idx)) for idx in target_indices]
            # Ensure unique, sorted indices
            target_indices = sorted(list(set(target_indices)))

        target_set = set(target_indices)
        frame_count = 0
        for frame in container.decode(video=0):
            if frame_count in target_set:
                img = frame.to_image()
                img_bytes_io = io.BytesIO()
                img.save(img_bytes_io, format="JPEG")
                frames_bytes.append(img_bytes_io.getvalue())
                if len(frames_bytes) >= len(target_set):
                    break
            frame_count += 1

        # Fallback if no frames were decoded but we expected them
        if not frames_bytes:
            container.seek(0)
            for frame in container.decode(video=0):
                img = frame.to_image()
                img_bytes_io = io.BytesIO()
                img.save(img_bytes_io, format="JPEG")
                frames_bytes.append(img_bytes_io.getvalue())
                break

    except Exception as e:
        logging.error(f"Failed to extract frames from video: {e}")
    finally:
        if container is not None:
            try:
                container.close()
            except Exception:
                pass
    return frames_bytes


def is_video_message(message) -> bool:
    """Checks whether a Telegram message represents a video document."""
    if not message:
        return False
    if getattr(message, 'video', None) is not None:
        return True
    if getattr(message, 'file', None) and getattr(message.file, 'mime_type', None):
        return message.file.mime_type.startswith("video/")
    return False


def get_temp_dir() -> str | None:
    """Returns /dev/shm (shared-memory ramdisk) if available with adequate space, otherwise None (system default /tmp)."""
    if os.path.exists("/dev/shm"):
        try:
            stat = os.statvfs("/dev/shm")
            free_bytes = stat.f_bavail * stat.f_frsize
            if free_bytes > 200 * 1024 * 1024:
                return "/dev/shm"
        except Exception:
            pass
    return None


def probe_video_duration(video_path: str, ffmpeg_bin: str) -> float | None:
    """Probes the video duration in seconds using ffprobe or av."""
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin and ffmpeg_bin:
        candidate = os.path.join(os.path.dirname(ffmpeg_bin), "ffprobe")
        if os.path.exists(candidate):
            ffprobe_bin = candidate
    if ffprobe_bin:
        try:
            res = subprocess.run(
                [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=5
            )
            if res.returncode == 0 and res.stdout.strip():
                val = float(res.stdout.strip())
                if val > 0:
                    return val
        except Exception:
            pass

    try:
        import av
        container = av.open(video_path)
        dur = None
        for stream in container.streams.video:
            if stream.duration and stream.time_base:
                dur = float(stream.duration * stream.time_base)
                break
        if dur is None and container.duration:
            dur = float(container.duration / 1000000.0)
        container.close()
        if dur and dur > 0:
            return dur
    except Exception:
        pass

    return None


def probe_video_dimensions(video_path: str, ffmpeg_bin: str) -> tuple[int, int] | None:
    """Probes video width and height using ffprobe or av."""
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin and ffmpeg_bin:
        candidate = os.path.join(os.path.dirname(ffmpeg_bin), "ffprobe")
        if os.path.exists(candidate):
            ffprobe_bin = candidate
    if ffprobe_bin:
        try:
            res = subprocess.run(
                [ffprobe_bin, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", video_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=5
            )
            if res.returncode == 0 and "x" in res.stdout:
                w_str, h_str = res.stdout.strip().split("x", 1)
                return int(w_str), int(h_str)
        except Exception:
            pass

    try:
        import av
        container = av.open(video_path)
        for stream in container.streams.video:
            if stream.width and stream.height:
                dims = (int(stream.width), int(stream.height))
                container.close()
                return dims
        container.close()
    except Exception:
        pass

    return None


def split_video_lossless(
    video_bytes: bytes,
    filename: str,
    max_bytes: int,
    source_chat: str,
    known_duration: float = None
) -> list[dict]:
    """
    Losslessly splits an oversized video into sequential parts smaller than max_bytes using FFmpeg stream copy (-c copy).
    Operates in-memory via /dev/shm (if available) with zero re-encoding, optimal for low-power CPUs like Raspberry Pi 4.
    """
    t0 = time.time()
    ffmpeg_bin = FFMPEG_PATH or shutil.which("ffmpeg")
    if not ffmpeg_bin:
        logging.error(f"[{source_chat}] FFmpeg binary not found. Cannot split video.")
        return []

    name_part, ext = os.path.splitext(filename)
    if not ext or ext.lower() not in ('.mp4', '.mkv', '.webm', '.mov', '.avi'):
        ext = '.mp4'

    total_size = len(video_bytes)
    target_part_size = max_bytes * 0.85
    parts_count = max(2, math.ceil(total_size / target_part_size))

    temp_dir_parent = get_temp_dir()
    with tempfile.TemporaryDirectory(dir=temp_dir_parent) as tmp_dir:
        input_path = os.path.join(tmp_dir, f"input{ext}")
        with open(input_path, "wb") as f:
            f.write(video_bytes)

        duration = known_duration
        if not duration or duration <= 0:
            duration = probe_video_duration(input_path, ffmpeg_bin)

        if duration and duration > 0:
            segment_time = max(1.0, round(duration / parts_count, 2))
        else:
            segment_time = 15.0

        output_pattern = os.path.join(tmp_dir, f"part_%03d{ext}")
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i", input_path,
            "-map", "0:v?",
            "-map", "0:a?",
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(segment_time),
            "-reset_timestamps", "1",
        ]
        if ext.lower() in ('.mp4', '.mov'):
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(output_pattern)

        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if res.returncode != 0:
            logging.error(f"[{source_chat}] FFmpeg split error: {res.stderr.decode('utf-8', errors='replace')[:400]}")
            return []

        part_files = sorted([
            os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)
            if f.startswith("part_") and f.endswith(ext)
        ])

        if len(part_files) <= 1 and duration and duration > 2.0:
            segment_time = max(1.0, round(segment_time / 2, 2))
            for f in part_files:
                try:
                    os.remove(f)
                except Exception:
                    pass
            cmd[-2] = str(segment_time)
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            part_files = sorted([
                os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)
                if f.startswith("part_") and f.endswith(ext)
            ])

        if len(part_files) <= 1:
            logging.warning(f"[{source_chat}] Video could not be split losslessly (no internal keyframes found).")
            return []

        oversized_parts = [f for f in part_files if os.path.getsize(f) > max_bytes]
        if oversized_parts:
            logging.warning(
                f"[{source_chat}] Splitting produced {len(part_files)} parts, but {len(oversized_parts)} part(s) "
                f"still exceed {max_bytes / (1024 * 1024):.1f} MB due to keyframe spacing."
            )

        results = []
        total_parts = len(part_files)
        for i, ppath in enumerate(part_files):
            with open(ppath, "rb") as pf:
                p_bytes = pf.read()

            part_dur = probe_video_duration(ppath, ffmpeg_bin)
            part_dims = probe_video_dimensions(ppath, ffmpeg_bin)

            results.append({
                "bytes": p_bytes,
                "filename": f"{name_part}_part{i+1}_of_{total_parts}{ext}",
                "part_index": i + 1,
                "total_parts": total_parts,
                "duration_ms": int(part_dur * 1000) if part_dur else None,
                "w": part_dims[0] if part_dims else None,
                "h": part_dims[1] if part_dims else None,
                "size": len(p_bytes)
            })

        logging.info(
            f"[{source_chat}] Video ({total_size / (1024 * 1024):.2f} MB) losslessly split into "
            f"{total_parts} parts in {time.time() - t0:.2f}s."
        )
        return results


def compress_video(
    video_bytes: bytes,
    filename: str,
    max_bytes: int,
    source_chat: str,
    max_height: int = 720,
    preset: str = "veryfast",
    crf: int = 28,
    threads: int = 2
) -> dict | None:
    """
    Compresses an oversized video by downscaling resolution and re-encoding with libx264.
    Uses fast preset and audio stream copy to minimize CPU utilization.
    """
    t0 = time.time()
    ffmpeg_bin = FFMPEG_PATH or shutil.which("ffmpeg")
    if not ffmpeg_bin:
        logging.error(f"[{source_chat}] FFmpeg binary not found. Cannot compress video.")
        return None

    name_part, ext = os.path.splitext(filename)
    out_ext = ".mp4"

    temp_dir_parent = get_temp_dir()
    with tempfile.TemporaryDirectory(dir=temp_dir_parent) as tmp_dir:
        input_path = os.path.join(tmp_dir, f"input{ext or '.mp4'}")
        output_path = os.path.join(tmp_dir, f"output{out_ext}")
        with open(input_path, "wb") as f:
            f.write(video_bytes)

        scale_filter = f"scale='if(gte(iw,ih),min(1280,iw),-2)':'if(gte(iw,ih),-2,min({max_height},ih))'"

        cmd = [
            ffmpeg_bin,
            "-y",
            "-i", input_path,
            "-vf", scale_filter,
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-threads", str(threads),
            "-c:a", "copy",
            "-movflags", "+faststart",
            output_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if res.returncode != 0:
            cmd[-3:-2] = ["-c:a", "aac", "-b:a", "128k"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if res.returncode != 0:
                logging.error(f"[{source_chat}] FFmpeg compression failed: {res.stderr.decode('utf-8', errors='replace')[:400]}")
                return None

        if os.path.exists(output_path):
            with open(output_path, "rb") as out_f:
                out_bytes = out_f.read()

            dur = probe_video_duration(output_path, ffmpeg_bin)
            dims = probe_video_dimensions(output_path, ffmpeg_bin)

            logging.info(
                f"[{source_chat}] Video compressed from {len(video_bytes)/(1024*1024):.2f} MB "
                f"to {len(out_bytes)/(1024*1024):.2f} MB ({(1 - len(out_bytes)/len(video_bytes))*100:.1f}% reduction) in {time.time() - t0:.2f}s."
            )
            return {
                "bytes": out_bytes,
                "filename": f"{name_part}{out_ext}",
                "duration_ms": int(dur * 1000) if dur else None,
                "w": dims[0] if dims else None,
                "h": dims[1] if dims else None,
                "size": len(out_bytes)
            }
    return None


# Formats a Telegram still image can legitimately be. Anything else is treated as
# undecodable: Pillow's TGA/DDS plugins accept near-arbitrary bytes, so without
# this whitelist a corrupt thumbnail would probe as a bogus multi-thousand-pixel image.
STILL_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}

# Component counts of the encoded blurhash. 4x3 is the common choice for landscape
# previews and what most Matrix clients are tuned for.
BLURHASH_COMPONENTS_X = 4
BLURHASH_COMPONENTS_Y = 3

# The blurhash only encodes the lowest spatial frequencies of the image, so a
# downscaled copy produces a visually identical hash. The encoder is pure Python
# and costs linear time in the pixel count, so this bound is what keeps a
# full-resolution photo at tens of milliseconds instead of tens of seconds.
BLURHASH_MAX_EDGE = 64


def probe_image(image_bytes: bytes) -> tuple[int, int, str] | None:
    """Reads width, height and mime type straight from the encoded bytes.
    Returns None if the bytes are not a decodable still image."""
    try:
        from PIL import Image
    except ImportError:
        logging.error("Pillow (PIL) package is missing! Please install it to enable thumbnail dimension probing.")
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.format not in STILL_IMAGE_FORMATS:
                logging.debug(f"Ignoring thumbnail of unexpected format {img.format}.")
                return None
            width, height = img.size
            Image.init()
            mime = Image.MIME.get(img.format)
            if not mime or width < 1 or height < 1:
                return None
            return int(width), int(height), mime
    except Exception as e:
        logging.debug(f"Could not probe thumbnail bytes: {e}")
        return None


def generate_image_thumbnail(image_bytes: bytes, max_size: int = 800) -> tuple[bytes, int, int, str] | None:
    """Generates a high-quality downscaled thumbnail from still image bytes.

    Returns (thumbnail_bytes, width, height, mime_type) or None if the bytes
    could not be decoded or processed."""
    try:
        from PIL import Image
    except ImportError:
        logging.error("Pillow (PIL) package is missing! Cannot generate image thumbnail.")
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.format not in STILL_IMAGE_FORMATS:
                logging.debug(f"Refusing to generate thumbnail for unexpected format {img.format}.")
                return None

            # Get MIME format type
            Image.init()
            mime = Image.MIME.get(img.format)
            if not mime:
                return None

            # Precaution: If image is already smaller than target thumbnail bounds,
            # return the original bytes and dimensions immediately to avoid processing.
            width, height = img.size
            if width <= max_size and height <= max_size:
                if width < 1 or height < 1:
                    return None
                return image_bytes, int(width), int(height), mime

            # Optimize JPEG decoding by using draft mode (scales during load)
            if img.format == "JPEG":
                img.draft(img.mode, (max_size * 2, max_size * 2))

            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

            # Read size post-resize
            width, height = img.size
            if width < 1 or height < 1:
                return None

            out = io.BytesIO()
            save_format = img.format if img.format in ("PNG", "WEBP") else "JPEG"
            img.save(out, format=save_format, quality=85, optimize=True)
            return out.getvalue(), width, height, mime
    except Exception as e:
        logging.debug(f"Could not generate image thumbnail: {e}")
        return None



def compute_blurhash(image_bytes: bytes) -> str | None:
    """Encodes a blurhash from still image bytes, downscaling them to a thumbnail first.

    Returns None whenever a hash cannot be produced. A blurhash is a rendering
    nicety rather than a safety property, so an absent package or an undecodable
    image degrades to an event without one, never to dropped media."""
    try:
        from PIL import Image
        import blurhash
    except ImportError:
        logging.debug("Pillow (PIL) or blurhash package is missing; sending media without a blurhash.")
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.format not in STILL_IMAGE_FORMATS:
                logging.debug(f"Refusing to blurhash unexpected format {img.format}.")
                return None
            # Decode into an RGB copy while the source is still open, so the copy
            # outlives the context manager that closes the source.
            thumbnail = img.convert("RGB")

        thumbnail.thumbnail((BLURHASH_MAX_EDGE, BLURHASH_MAX_EDGE))

        # blurhash.encode() wants rows of (r, g, b) rather than an Image.
        width, height = thumbnail.size
        raw = thumbnail.tobytes()
        stride = width * 3
        rows = [
            [tuple(raw[x:x + 3]) for x in range(y * stride, (y + 1) * stride, 3)]
            for y in range(height)
        ]
        return blurhash.encode(rows, BLURHASH_COMPONENTS_X, BLURHASH_COMPONENTS_Y)
    except Exception as e:
        logging.debug(f"Could not compute blurhash: {e}")
        return None


async def get_image_safety_classification(image_bytes: bytes, source_chat: str, filename: str, index_label: str = None) -> tuple[bool | None, list[str]]:
    """
    Calls Llama Guard API for a single image/frame.
    Returns (is_safe, violated_categories) where:
    - is_safe is True if 'safe', False if 'unsafe', or None if there was an error.
    - violated_categories is a list of violated Sxx categories.
    """
    label_prefix = f" [{index_label}]" if index_label else ""
    logging.info(f"[{source_chat}]{label_prefix} Calling Llama Guard API ({LLAMAGUARD_MODEL_NAME}) for safety classification of {filename}...")
    
    try:
        import base64
        import urllib.request
        import json

        b64_data = base64.b64encode(image_bytes).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{b64_data}"
        
        prompt_text = """Task: Check if there is unsafe content in 'User' messages in conversations according to our safety policy with the below categories.

<BEGIN UNSAFE CONTENT CATEGORIES>
S1: Violent Crimes.
S2: Non-Violent Crimes.
S3: Sex-Related Crimes.
S4: Child Sexual Exploitation.
S5: Defamation.
S6: Specialized Advice.
S7: Privacy.
S8: Intellectual Property.
S9: Indiscriminate Weapons.
S10: Hate.
S11: Suicide & Self-Harm.
S12: Sexual Content.
S13: Elections.
S14: Code Interpreter Abuse.
<END UNSAFE CONTENT CATEGORIES>

<BEGIN CONVERSATION>
User: <|image|>
<END CONVERSATION>

Provide your safety assessment for ONLY THE LAST User in the above conversation:
- First line must read 'safe' or 'unsafe'.
- If unsafe, a list of violated categories must be on the next line."""

        payload = {
            "model": LLAMAGUARD_MODEL_NAME,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt_text
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_url
                            }
                        }
                    ]
                }
            ],
            "temperature": 0.0
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        if LLAMAGUARD_API_KEY:
            headers["Authorization"] = f"Bearer {LLAMAGUARD_API_KEY}"
            
        api_url_base = LLAMAGUARD_API_URL.rstrip('/')
        if api_url_base.endswith('/chat/completions'):
            api_url_base = api_url_base[:-17].rstrip('/')

        req = urllib.request.Request(
            url=f"{api_url_base}/chat/completions",
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method="POST"
        )
        
        def do_request():
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read()
                
        response_bytes = await asyncio.to_thread(do_request)
        resp_json = json.loads(response_bytes.decode('utf-8'))
        
        if "choices" not in resp_json or not resp_json["choices"]:
            logging.error(f"[{source_chat}]{label_prefix} Unexpected Llama Guard API response structure: {resp_json}")
            return None, []
            
        response_text = resp_json["choices"][0]["message"]["content"].strip()
        logging.info(f"[{source_chat}]{label_prefix} Llama Guard classification result: {response_text}")
        
        lines = response_text.split()
        if not lines:
            logging.error(f"[{source_chat}]{label_prefix} Llama Guard returned empty response.")
            return None, []
            
        status = lines[0].lower()
        if status == "unsafe":
            # Extract violated categories
            violated = []
            for line in lines[1:]:
                for word in line.replace(',', ' ').split():
                    word_clean = word.strip().upper()
                    if word_clean.startswith('S') and word_clean[1:].isdigit():
                        violated.append(word_clean)
            if not violated:
                violated = ["UNSPECIFIED"]
            return False, violated
        elif status == "safe":
            return True, []
        else:
            logging.error(f"[{source_chat}]{label_prefix} Llama Guard returned unexpected status '{status}'.")
            return None, []
            
    except Exception as e:
        logging.error(f"[{source_chat}]{label_prefix} Error during Llama Guard safety check: {e}")
        return None, []


async def check_single_image_safety(image_bytes: bytes, source_chat: str, filename: str, index_label: str = None) -> bool:
    """
    Checks the safety of a single image/frame against the Llama Guard API.
    Returns True if safe/allowed to pass, False if unsafe/blocked.
    """
    is_safe, violated = await get_image_safety_classification(image_bytes, source_chat, filename, index_label)
    if is_safe is None:
        return False  # Block on API error / fail-closed
        
    label_prefix = f" [{index_label}]" if index_label else ""
    
    if not is_safe:
        # 1. Block-list check (LLAMAGUARD_CHECKS)
        if LLAMAGUARD_CHECKS:
            overlap = [c for c in violated if c in LLAMAGUARD_CHECKS]
            if overlap:
                logging.warning(f"[{source_chat}]{label_prefix} BLOCKING media {filename}! Violates configured Llama Guard categories: {overlap}")
                return False

        # 2. Required-list check (LLAMAGUARD_REQUIRE_CHECKS)
        if LLAMAGUARD_REQUIRE_CHECKS:
            required_overlap = [c for c in violated if c in LLAMAGUARD_REQUIRE_CHECKS]
            if not required_overlap:
                logging.warning(f"[{source_chat}]{label_prefix} BLOCKING media {filename}! Classified as unsafe ({violated}), but does not match any required categories: {LLAMAGUARD_REQUIRE_CHECKS}")
                return False
            else:
                logging.info(f"[{source_chat}]{label_prefix} Media matches required safety check: {required_overlap}. Passing.")
                return True

        # 3. Default behavior if no required-list and no block-list overlap
        if LLAMAGUARD_CHECKS:
            logging.info(f"[{source_chat}]{label_prefix} Media classified as unsafe ({violated}), but none are in configured important checks ({LLAMAGUARD_CHECKS}). Passing.")
            return True
        else:
            logging.warning(f"[{source_chat}]{label_prefix} BLOCKING media {filename}! Violates Llama Guard categories: {violated}")
            return False
            
    else:  # safe
        if LLAMAGUARD_REQUIRE_CHECKS:
            logging.warning(f"[{source_chat}]{label_prefix} BLOCKING media {filename}! Classified as safe, but does not match any required unsafe categories: {LLAMAGUARD_REQUIRE_CHECKS}")
            return False
        return True


async def check_media_safety(media_bytes: bytes, mime_type: str, source_chat: str, filename: str) -> bool:
    """
    Checks the safety of the media using Meta Llama Guard via an OpenAI-compatible Vision API.
    Returns True if the media is safe/allowed, False if it is unsafe/blocked.
    """
    if not LLAMAGUARD_API_URL:
        return True # Disabled, pass by default

    image_list = []
    if mime_type.startswith("image/"):
        image_list = [media_bytes]
    elif mime_type.startswith("video/"):
        logging.info(f"[{source_chat}] Extracting up to {LLAMAGUARD_VIDEO_FRAMES} frames from video {filename} for Llama Guard safety check...")
        image_list = await asyncio.to_thread(extract_video_frames, media_bytes, LLAMAGUARD_VIDEO_FRAMES, LLAMAGUARD_RANDOM_FRAMES)
        if not image_list:
            logging.error(f"[{source_chat}] Could not extract any video frames for safety check. Blocking media due to check failure.")
            return False

    if not image_list:
        logging.error(f"[{source_chat}] No image bytes available for safety check. Blocking media due to check failure.")
        return False

    if len(image_list) == 1:
        return await check_single_image_safety(image_list[0], source_chat, filename)

    # Spawn parallel safety checks for all extracted video frames
    tasks = []
    for i, img_bytes in enumerate(image_list):
        label = f"frame {i+1}/{len(image_list)}"
        tasks.append(get_image_safety_classification(img_bytes, source_chat, filename, label))

    results = await asyncio.gather(*tasks)
    
    # Analyze aggregated results for video frames
    # results is a list of tuple[bool | None, list[str]]
    
    # 1. Check for any API/processing errors (fail-closed)
    for is_safe, _ in results:
        if is_safe is None:
            logging.error(f"[{source_chat}] One or more video frames failed classification. Blocking video due to fail-closed safety policy.")
            return False

    # 2. Block-list check (LLAMAGUARD_CHECKS)
    # If ANY frame contains a category in LLAMAGUARD_CHECKS, we block the entire video.
    if LLAMAGUARD_CHECKS:
        for i, (is_safe, violated) in enumerate(results):
            if not is_safe:
                overlap = [c for c in violated if c in LLAMAGUARD_CHECKS]
                if overlap:
                    logging.warning(f"[{source_chat}] BLOCKING video {filename}! Frame {i+1} violates configured Llama Guard categories: {overlap}")
                    return False

    # 3. Required-list check (LLAMAGUARD_REQUIRE_CHECKS)
    # If whitelisting is active:
    # - At least ONE frame must match one of the required categories.
    # - Safe frames do not block the video in this context (since it's a video, and we only need the video to contain the required content somewhere).
    if LLAMAGUARD_REQUIRE_CHECKS:
        matched_required = False
        all_violated_matched = []
        for i, (is_safe, violated) in enumerate(results):
            if not is_safe:
                required_overlap = [c for c in violated if c in LLAMAGUARD_REQUIRE_CHECKS]
                if required_overlap:
                    matched_required = True
                    all_violated_matched.extend(required_overlap)
                    
        if not matched_required:
            logging.warning(f"[{source_chat}] BLOCKING video {filename}! No frames matched any required categories: {LLAMAGUARD_REQUIRE_CHECKS}")
            return False
        else:
            logging.info(f"[{source_chat}] Video matches required safety check (found required categories {list(set(all_violated_matched))}). Passing.")
            return True

    # 4. Default check if no required-list is configured
    # Under the default policy (or when only LLAMAGUARD_CHECKS is set), a video is allowed if no frames violate the block-list.
    # If LLAMAGUARD_CHECKS is empty and LLAMAGUARD_REQUIRE_CHECKS is empty:
    # - Any unsafe frame blocks the video by default.
    if not LLAMAGUARD_CHECKS:
        # LLAMAGUARD_CHECKS is empty, and LLAMAGUARD_REQUIRE_CHECKS is empty:
        # This is the "block all unsafe content" mode. Any unsafe frame blocks the video.
        for i, (is_safe, violated) in enumerate(results):
            if not is_safe:
                logging.warning(f"[{source_chat}] BLOCKING video {filename}! Frame {i+1} violates Llama Guard categories: {violated}")
                return False

    logging.info(f"[{source_chat}] Video {filename} successfully passed safety checks.")
    return True


tg_client = TelegramClient(
    'session/tgmabr', 
    TG_API_ID, 
    TG_API_HASH,
    device_model="Desktop",
    system_version="Windows 10",
    app_version="4.8.4"
)

# Initialize global Matrix client session
matrix_client = AsyncClient(MATRIX_HOMESERVER)
matrix_client.access_token = MATRIX_ACCESS_TOKEN


async def send_matrix_notice(room_id: str, plain_text: str, html_text: str):
    """Send a status/command response notice back to the Matrix room"""
    try:
        content = {
            "msgtype": "m.notice",
            "body": plain_text,
            "format": "org.matrix.custom.html",
            "formatted_body": html_text
        }
        await matrix_client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content
        )
    except Exception as e:
        logging.error(f"Failed to send Matrix command response notice: {e}")


async def on_matrix_message(room, event: RoomMessageText):
    """Matrix command handler for admin/room users to toggle image/video bridging dynamically."""
    global ENABLE_IMAGES, ENABLE_VIDEOS

    if not (ADMIN_MATRIX_USER_ID or ALLOW_NON_ADMIN_STOP):
        return

    # Only process commands sent from configured destination rooms
    if room.room_id not in MATRIX_ROOM_IDS:
        return

    # Ignore old messages sent before startup to prevent re-triggering historical commands
    if hasattr(event, 'server_timestamp') and event.server_timestamp and event.server_timestamp < STARTUP_TIMESTAMP_MS:
        return

    body = event.body.strip()
    if not body.lower().startswith("!tmmb"):
        return

    is_admin = bool(ADMIN_MATRIX_USER_ID and event.sender == ADMIN_MATRIX_USER_ID)
    is_stop_allowed = is_admin or ALLOW_NON_ADMIN_STOP

    # Check authorization to issue commands
    if not is_stop_allowed:
        logging.warning(f"[{room.room_id}] Unauthorized command attempt by invalid sender '{event.sender}': '{body}'")
        return

    logging.info(f"[{room.room_id}] Command received from user '{event.sender}': '{body}'")

    parts = body.split()
    if len(parts) == 1 or parts[1].lower() in ("help", "-h", "--help"):
        help_plain = (
            "[TgMediaToMatrix] Command Usage:\n"
            "- !tmmb start : Enable both image and video bridging (Admin only)\n"
            "- !tmmb stop : Disable both image and video bridging\n"
            "- !tmmb image enable/disable : Enable (Admin only) or disable image bridging\n"
            "- !tmmb video enable/disable : Enable (Admin only) or disable video bridging\n"
            "- !tmmb status : View current bridging status"
        )
        help_html = (
            "<strong>[TgMediaToMatrix] Command Usage:</strong><br/>"
            "• <code>!tmmb start</code> : Enable both image and video bridging <em>(Admin only)</em><br/>"
            "• <code>!tmmb stop</code> : Disable both image and video bridging<br/>"
            "• <code>!tmmb image enable/disable</code> : Enable <em>(Admin only)</em> or disable image bridging<br/>"
            "• <code>!tmmb video enable/disable</code> : Enable <em>(Admin only)</em> or disable video bridging<br/>"
            "• <code>!tmmb status</code> : View current bridging status"
        )
        await send_matrix_notice(room.room_id, help_plain, help_html)
        return

    subcmd = parts[1].lower()

    # START / ENABLE (Both) -> Requires Admin
    if subcmd in ("start", "enable") and len(parts) == 2:
        if not is_admin:
            logging.warning(f"[{room.room_id}] Unauthorized start attempt by non-admin '{event.sender}': '{body}'")
            notice_plain = "[TgMediaToMatrix] Only the configured admin user can start/enable media bridging."
            notice_html = "<strong>[TgMediaToMatrix]</strong> Only the configured admin user can start/enable media bridging."
            await send_matrix_notice(room.room_id, notice_plain, notice_html)
            return

        ENABLE_IMAGES = True
        ENABLE_VIDEOS = True
        logging.info(f"[{room.room_id}] User '{event.sender}' STARTED (ENABLED) both image and video bridging.")
        msg_plain = "[TgMediaToMatrix] Both image and video bridging have been STARTED (ENABLED)."
        msg_html = "<strong>[TgMediaToMatrix]</strong> Both image and video bridging have been <code>STARTED</code> (ENABLED)."
        await send_matrix_notice(room.room_id, msg_plain, msg_html)
        return

    # STOP / DISABLE (Both) -> Admin or ALLOW_NON_ADMIN_STOP
    if subcmd in ("stop", "disable") and len(parts) == 2:
        ENABLE_IMAGES = False
        ENABLE_VIDEOS = False
        logging.info(f"[{room.room_id}] User '{event.sender}' STOPPED (DISABLED) both image and video bridging.")
        msg_plain = "[TgMediaToMatrix] Both image and video bridging have been STOPPED (DISABLED)."
        msg_html = "<strong>[TgMediaToMatrix]</strong> Both image and video bridging have been <code>STOPPED</code> (DISABLED)."
        await send_matrix_notice(room.room_id, msg_plain, msg_html)
        return

    # STATUS -> Admin or ALLOW_NON_ADMIN_STOP
    if subcmd == "status":
        status_plain = (
            f"[TgMediaToMatrix] Current Status:\n"
            f"- Images: {'ENABLED' if ENABLE_IMAGES else 'DISABLED'}\n"
            f"- Videos: {'ENABLED' if ENABLE_VIDEOS else 'DISABLED'}\n"
            f"- Oversized Videos: {OVERSIZED_VIDEO_ACTION.upper()} (Limit: {MAX_MEDIA_SIZE_MB}MB, Ceiling: {OVERSIZED_VIDEO_MAX_INPUT_MB}MB)\n"
            f"- Deduplication: {'ENABLED' if DEDUPLICATION_ENABLED else 'DISABLED'} (TTL: {DEDUPLICATION_TTL_MINUTES}m, Cache: {len(media_deduplicator._cache)}/{DEDUPLICATION_CACHE_SIZE})"
        )
        status_html = (
            f"<strong>[TgMediaToMatrix] Current Status:</strong><br/>"
            f"• Images: <code>{'ENABLED' if ENABLE_IMAGES else 'DISABLED'}</code><br/>"
            f"• Videos: <code>{'ENABLED' if ENABLE_VIDEOS else 'DISABLED'}</code><br/>"
            f"• Oversized Videos: <code>{OVERSIZED_VIDEO_ACTION.upper()}</code> (Limit: {MAX_MEDIA_SIZE_MB}MB, Ceiling: {OVERSIZED_VIDEO_MAX_INPUT_MB}MB)<br/>"
            f"• Deduplication: <code>{'ENABLED' if DEDUPLICATION_ENABLED else 'DISABLED'}</code> (TTL: {DEDUPLICATION_TTL_MINUTES}m, Cache: {len(media_deduplicator._cache)}/{DEDUPLICATION_CACHE_SIZE})"
        )
        await send_matrix_notice(room.room_id, status_plain, status_html)
        return

    # IMAGE COMMANDS
    if len(parts) >= 3 and subcmd in ("image", "images"):
        action = parts[2].lower()
        if action in ("enable", "on", "true", "1"):
            if not is_admin:
                logging.warning(f"[{room.room_id}] Unauthorized image enable attempt by non-admin '{event.sender}': '{body}'")
                notice_plain = "[TgMediaToMatrix] Only the configured admin user can enable image bridging."
                notice_html = "<strong>[TgMediaToMatrix]</strong> Only the configured admin user can enable image bridging."
                await send_matrix_notice(room.room_id, notice_plain, notice_html)
                return

            ENABLE_IMAGES = True
            logging.info(f"[{room.room_id}] User '{event.sender}' ENABLED image bridging.")
            msg_plain = "[TgMediaToMatrix] Image bridging has been ENABLED."
            msg_html = "<strong>[TgMediaToMatrix]</strong> Image bridging has been <code>ENABLED</code>."
            await send_matrix_notice(room.room_id, msg_plain, msg_html)
        elif action in ("disable", "off", "false", "0"):
            ENABLE_IMAGES = False
            logging.info(f"[{room.room_id}] User '{event.sender}' DISABLED image bridging.")
            msg_plain = "[TgMediaToMatrix] Image bridging has been DISABLED."
            msg_html = "<strong>[TgMediaToMatrix]</strong> Image bridging has been <code>DISABLED</code>."
            await send_matrix_notice(room.room_id, msg_plain, msg_html)
        else:
            invalid_plain = f"[TgMediaToMatrix] Unknown action '{action}'. Use 'enable' or 'disable'."
            invalid_html = f"<strong>[TgMediaToMatrix]</strong> Unknown action <code>{action}</code>. Use <code>enable</code> or <code>disable</code>."
            await send_matrix_notice(room.room_id, invalid_plain, invalid_html)
        return

    # VIDEO COMMANDS
    if len(parts) >= 3 and subcmd in ("video", "videos"):
        action = parts[2].lower()
        if action in ("enable", "on", "true", "1"):
            if not is_admin:
                logging.warning(f"[{room.room_id}] Unauthorized video enable attempt by non-admin '{event.sender}': '{body}'")
                notice_plain = "[TgMediaToMatrix] Only the configured admin user can enable video bridging."
                notice_html = "<strong>[TgMediaToMatrix]</strong> Only the configured admin user can enable video bridging."
                await send_matrix_notice(room.room_id, notice_plain, notice_html)
                return

            ENABLE_VIDEOS = True
            logging.info(f"[{room.room_id}] User '{event.sender}' ENABLED video bridging.")
            msg_plain = "[TgMediaToMatrix] Video bridging has been ENABLED."
            msg_html = "<strong>[TgMediaToMatrix]</strong> Video bridging has been <code>ENABLED</code>."
            await send_matrix_notice(room.room_id, msg_plain, msg_html)
        elif action in ("disable", "off", "false", "0"):
            ENABLE_VIDEOS = False
            logging.info(f"[{room.room_id}] User '{event.sender}' DISABLED video bridging.")
            msg_plain = "[TgMediaToMatrix] Video bridging has been DISABLED."
            msg_html = "<strong>[TgMediaToMatrix]</strong> Video bridging has been <code>DISABLED</code>."
            await send_matrix_notice(room.room_id, msg_plain, msg_html)
        else:
            invalid_plain = f"[TgMediaToMatrix] Unknown action '{action}'. Use 'enable' or 'disable'."
            invalid_html = f"<strong>[TgMediaToMatrix]</strong> Unknown action <code>{action}</code>. Use <code>enable</code> or <code>disable</code>."
            await send_matrix_notice(room.room_id, invalid_plain, invalid_html)
        return

    unknown_plain = f"[TgMediaToMatrix] Unknown command '{body}'. Type '!tmmb help' for usage."
    unknown_html = f"<strong>[TgMediaToMatrix]</strong> Unknown command <code>{body}</code>. Type <code>!tmmb help</code> for usage."
    await send_matrix_notice(room.room_id, unknown_plain, unknown_html)


async def send_matrix_media_event(
    media_bytes: bytes,
    mime_type: str,
    filename: str,
    msg_type: str,
    info_dict: dict,
    source_chat: str,
    channel_name: str,
    caption_prefix: str = None,
    blurhash_str: str = None
) -> bool:
    """Uploads in-memory media bytes to Matrix homeserver and dispatches room messages across MATRIX_ROOM_IDS."""
    logging.info(f"[{source_chat}] Uploading to Matrix homeserver: {filename} ({len(media_bytes)} bytes)...")
    try:
        upload_resp, _ = await matrix_client.upload(
            io.BytesIO(media_bytes),
            content_type=mime_type,
            filename=filename,
            filesize=len(media_bytes)
        )
        if not isinstance(upload_resp, UploadResponse):
            logging.error(f"[{source_chat}] Matrix server rejected upload for {filename}! Response: {upload_resp}")
            return False

        content_uri = upload_resp.content_uri
        logging.info(f"[{source_chat}] Upload successful! MXC-URI: {content_uri}. Sending room message...")

        size_bytes = len(media_bytes)
        if size_bytes < 1024:
            size_str = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            size_str = f"{size_bytes / 1024:.2f} KB"
        else:
            size_str = f"{size_bytes / (1024 * 1024):.2f} MB"

        if info_dict and "w" in info_dict and "h" in info_dict:
            meta_line = f"{info_dict['w']}x{info_dict['h']} {size_str}"
        else:
            meta_line = f"{size_str}"

        if caption_prefix:
            meta_line = f"{caption_prefix} • {meta_line}"

        body_text = meta_line
        formatted_body_text = f'<font color="#888888"><small>{meta_line}</small></font>'

        matrix_content = {
            "msgtype": msg_type,
            "body": body_text,
            "url": content_uri,
            "filename": filename,
            "format": "org.matrix.custom.html",
            "formatted_body": formatted_body_text,
            "source": channel_name
        }
        if blurhash_str:
            info_dict["xyz.amorgan.blurhash"] = blurhash_str
        if info_dict:
            matrix_content["info"] = info_dict

        for target_room_id in MATRIX_ROOM_IDS:
            try:
                send_response = await matrix_client.room_send(
                    room_id=target_room_id,
                    message_type="m.room.message",
                    content=matrix_content
                )
                if isinstance(send_response, RoomSendResponse):
                    logging.info(f"[{source_chat}] Event successfully posted in Matrix room '{target_room_id}' (Event ID: {send_response.event_id})")
                elif isinstance(send_response, RoomSendError):
                    logging.error(f"[{source_chat}] Failed to post event to Matrix room '{target_room_id}': {send_response.message} (status code: {send_response.status_code})")
                else:
                    logging.error(f"[{source_chat}] Unknown response type when posting event to Matrix room '{target_room_id}': {send_response}")
            except Exception as room_err:
                logging.error(f"[{source_chat}] Error sending event to Matrix room '{target_room_id}': {room_err}")
        return True
    except Exception as e:
        logging.error(f"[{source_chat}] General error during Matrix transfer of {filename}: {e}")
        return False


async def process_and_upload_media(message, source_chat, channel_name):
    """Process a single Telegram message and stream the media to Matrix"""
    mime_type = message.file.mime_type if message.file else None
    filename = message.file.name if message.file else None
    
    if message.photo and not mime_type:
        mime_type = "image/jpeg"
        filename = f"telegram_photo_{message.id}.jpg"
    elif not mime_type:
        return

    is_image = mime_type.startswith("image/")
    is_video = mime_type.startswith("video/")

    if not (is_image or is_video):
        return

    if is_image and not ENABLE_IMAGES:
        logging.info(f"[{source_chat}] Skipping image media because ENABLE_IMAGES is false.")
        return

    if is_video and not ENABLE_VIDEOS:
        logging.info(f"[{source_chat}] Skipping video media because ENABLE_VIDEOS is false.")
        return

    if not filename:
        ext = "mp4" if mime_type.startswith("video/") else "jpg"
        filename = f"telegram_media_{message.id}.{ext}"

    file_size = message.file.size if message.file else 0
    if is_image and MIN_IMAGE_SIZE_BYTES > 0 and file_size < MIN_IMAGE_SIZE_BYTES:
        logging.info(f"[{source_chat}] Skipping image {filename}: File size ({round(file_size / 1024, 2)} KB) is below minimum required size of {MIN_IMAGE_SIZE_KB} KB.")
        return

    if is_video and MIN_VIDEO_SIZE_BYTES > 0 and file_size < MIN_VIDEO_SIZE_BYTES:
        logging.info(f"[{source_chat}] Skipping video {filename}: File size ({round(file_size / 1024, 2)} KB) is below minimum required size of {MIN_VIDEO_SIZE_KB} KB.")
        return

    # Pre-download deduplication check (Telegram Media ID)
    tg_media_id = get_telegram_media_id(message)
    if tg_media_id and media_deduplicator.is_duplicate(tg_media_id):
        logging.info(f"[{source_chat}] Skipping duplicate media {filename}: Telegram media ID ({tg_media_id}) was already forwarded within {DEDUPLICATION_TTL_MINUTES}m.")
        return

    if tg_media_id:
        media_deduplicator.mark_seen(tg_media_id)

    logging.info(f"[{source_chat}] Processing media: {filename} ({mime_type}). Starting Telegram download...")
    
    try:
        media_bytes = await message.download_media(file=bytes)
        if not media_bytes:
            logging.error(f"[{source_chat}] Telegram download failed for: {filename}")
            if tg_media_id:
                media_deduplicator.unmark(tg_media_id)
            return
    except Exception as e:
        logging.error(f"[{source_chat}] Critical error during Telegram download: {e}")
        if tg_media_id:
            media_deduplicator.unmark(tg_media_id)
        return

    logging.info(f"[{source_chat}] Download complete ({len(media_bytes)} bytes).")

    # Post-download deduplication check (Content SHA-256)
    content_hash = await asyncio.to_thread(compute_sha256, media_bytes)
    content_key = f"sha256_{content_hash}"
    if media_deduplicator.is_duplicate(content_key):
        logging.info(f"[{source_chat}] Skipping duplicate media {filename}: Content hash ({content_hash[:12]}...) was already forwarded within {DEDUPLICATION_TTL_MINUTES}m.")
        return
    media_deduplicator.mark_seen(content_key)

    # Llama Guard Safety Check
    if LLAMAGUARD_API_URL:
        is_safe = await check_media_safety(media_bytes, mime_type, source_chat, filename)
        if not is_safe:
            return

    # Thumbnail preparation for videos and images
    msg_type = "m.image" if mime_type.startswith("image/") else "m.video"
    thumb_url = None
    thumb_info = None
    blurhash_bytes = None

    if msg_type in ("m.video", "m.image"):
        try:
            thumb_bytes = None
            thumb_w = None
            thumb_h = None
            thumb_mime = None

            if msg_type == "m.image":
                # Generate thumbnail locally in-memory to save bandwidth and get optimal dimensions
                res = await asyncio.to_thread(generate_image_thumbnail, media_bytes)
                if res:
                    thumb_bytes, thumb_w, thumb_h, thumb_mime = res
            else:
                # For videos, download the high-resolution thumbnail from Telegram
                t_bytes = await message.download_media(thumb=-1, file=bytes)
                if t_bytes:
                    probed = await asyncio.to_thread(probe_image, t_bytes)
                    if probed:
                        thumb_bytes = t_bytes
                        thumb_w, thumb_h, thumb_mime = probed

            if thumb_bytes:
                blurhash_bytes = thumb_bytes
                thumb_ext = thumb_mime.split('/', 1)[1]
                thumb_resp, _ = await matrix_client.upload(
                    io.BytesIO(thumb_bytes),
                    content_type=thumb_mime,
                    filename=f"thumbnail.{thumb_ext}",
                    filesize=len(thumb_bytes)
                )
                if isinstance(thumb_resp, UploadResponse):
                    thumb_url = thumb_resp.content_uri
                    thumb_info = {
                        "mimetype": thumb_mime,
                        "size": len(thumb_bytes),
                        "w": thumb_w,
                        "h": thumb_h
                    }
        except Exception as thumb_err:
            logging.debug(f"[{source_chat}] Thumbnail skipped: {thumb_err}")

    if blurhash_bytes is None and is_image:
        blurhash_bytes = media_bytes

    blurhash_str = None
    if blurhash_bytes is not None:
        blurhash_str = await asyncio.to_thread(compute_blurhash, blurhash_bytes)

    # Extract additional metadata from Telegram attributes
    orig_duration = None
    orig_w = None
    orig_h = None
    if message.document and message.document.attributes:
        for attr in message.document.attributes:
            if hasattr(attr, 'duration') and attr.duration is not None:
                orig_duration = float(attr.duration)
            if hasattr(attr, 'w') and attr.w is not None and hasattr(attr, 'h') and attr.h is not None:
                orig_w = int(attr.w)
                orig_h = int(attr.h)
    elif message.photo and message.photo.sizes:
        largest = message.photo.sizes[-1]
        if hasattr(largest, 'w') and largest.w is not None and hasattr(largest, 'h') and largest.h is not None:
            orig_w = int(largest.w)
            orig_h = int(largest.h)

    # Handle oversized videos (if above configured limit)
    if is_video and len(media_bytes) > MAX_MEDIA_SIZE_BYTES:
        logging.info(
            f"[{source_chat}] Oversized video ({round(len(media_bytes) / (1024 * 1024), 2)} MB > "
            f"{MAX_MEDIA_SIZE_MB} MB). Action: {OVERSIZED_VIDEO_ACTION}..."
        )

        if OVERSIZED_VIDEO_ACTION == "split":
            parts = await asyncio.to_thread(
                split_video_lossless,
                media_bytes,
                filename,
                MAX_MEDIA_SIZE_BYTES,
                source_chat,
                known_duration=orig_duration
            )
            if parts:
                for p in parts:
                    part_info = {
                        "size": p["size"],
                        "mimetype": mime_type,
                        "filename": p["filename"]
                    }
                    if p.get("w") and p.get("h"):
                        part_info["w"] = p["w"]
                        part_info["h"] = p["h"]
                    elif orig_w and orig_h:
                        part_info["w"] = orig_w
                        part_info["h"] = orig_h

                    if p.get("duration_ms"):
                        part_info["duration"] = p["duration_ms"]
                    elif orig_duration:
                        part_info["duration"] = int((orig_duration / len(parts)) * 1000)

                    if thumb_url:
                        part_info["thumbnail_url"] = thumb_url
                        part_info["thumbnail_info"] = thumb_info

                    await send_matrix_media_event(
                        media_bytes=p["bytes"],
                        mime_type=mime_type,
                        filename=p["filename"],
                        msg_type="m.video",
                        info_dict=part_info,
                        source_chat=source_chat,
                        channel_name=channel_name,
                        caption_prefix=f"Part {p['part_index']}/{p['total_parts']}",
                        blurhash_str=blurhash_str
                    )
                return

            if OVERSIZED_VIDEO_FALLBACK_COMPRESS:
                logging.info(f"[{source_chat}] Lossless split produced no parts; falling back to compression...")
                compressed = await asyncio.to_thread(
                    compress_video,
                    media_bytes,
                    filename,
                    MAX_MEDIA_SIZE_BYTES,
                    source_chat,
                    max_height=VIDEO_COMPRESSION_MAX_HEIGHT,
                    preset=VIDEO_COMPRESSION_PRESET,
                    crf=VIDEO_COMPRESSION_CRF,
                    threads=VIDEO_COMPRESSION_THREADS
                )
                if compressed and compressed["size"] <= MAX_MEDIA_SIZE_BYTES:
                    media_bytes = compressed["bytes"]
                    filename = compressed["filename"]
                    mime_type = "video/mp4"
                    if compressed.get("duration_ms"):
                        orig_duration = compressed["duration_ms"] / 1000.0
                    if compressed.get("w") and compressed.get("h"):
                        orig_w = compressed["w"]
                        orig_h = compressed["h"]
                else:
                    logging.warning(f"[{source_chat}] Video compression fallback failed or result still exceeds {MAX_MEDIA_SIZE_MB} MB. Skipping.")
                    return
            else:
                logging.warning(f"[{source_chat}] Video could not be split below {MAX_MEDIA_SIZE_MB} MB. Skipping.")
                return

        elif OVERSIZED_VIDEO_ACTION == "compress":
            compressed = await asyncio.to_thread(
                compress_video,
                media_bytes,
                filename,
                MAX_MEDIA_SIZE_BYTES,
                source_chat,
                max_height=VIDEO_COMPRESSION_MAX_HEIGHT,
                preset=VIDEO_COMPRESSION_PRESET,
                crf=VIDEO_COMPRESSION_CRF,
                threads=VIDEO_COMPRESSION_THREADS
            )
            if compressed and compressed["size"] <= MAX_MEDIA_SIZE_BYTES:
                media_bytes = compressed["bytes"]
                filename = compressed["filename"]
                mime_type = "video/mp4"
                if compressed.get("duration_ms"):
                    orig_duration = compressed["duration_ms"] / 1000.0
                if compressed.get("w") and compressed.get("h"):
                    orig_w = compressed["w"]
                    orig_h = compressed["h"]
            else:
                logging.warning(f"[{source_chat}] Video compression failed or result still exceeds {MAX_MEDIA_SIZE_MB} MB. Skipping.")
                return

        elif OVERSIZED_VIDEO_ACTION == "skip":
            logging.warning(f"[{source_chat}] Media skipped: Video ({round(len(media_bytes) / (1024 * 1024), 2)} MB) exceeds limit of {MAX_MEDIA_SIZE_MB} MB.")
            return

    # Standard upload for single media item
    info_dict = {
        "size": len(media_bytes),
        "mimetype": mime_type,
        "filename": filename
    }
    if orig_duration is not None:
        info_dict["duration"] = int(orig_duration * 1000)
    if orig_w is not None and orig_h is not None:
        info_dict["w"] = orig_w
        info_dict["h"] = orig_h
    if thumb_url:
        info_dict["thumbnail_url"] = thumb_url
        info_dict["thumbnail_info"] = thumb_info

    await send_matrix_media_event(
        media_bytes=media_bytes,
        mime_type=mime_type,
        filename=filename,
        msg_type=msg_type,
        info_dict=info_dict,
        source_chat=source_chat,
        channel_name=channel_name,
        blurhash_str=blurhash_str
    )


# --- THE CENTRAL HANDLER FOR EVERYTHING ---
@tg_client.on(events.NewMessage(chats=TG_CHANNELS))
async def master_handler(event):
    if not event.message.media:
        return

    # Check for topic / thread details (subchannels in forums)
    topic_id = None
    if hasattr(event.message, 'reply_to') and event.message.reply_to:
        if hasattr(event.message.reply_to, 'forum_topic') and event.message.reply_to.forum_topic:
            topic_id = getattr(event.message.reply_to, 'reply_to_msg_id', None)

    # Validate channel / topic filter match
    if not is_channel_and_topic_allowed(event.chat_id, event.chat, topic_id):
        return

    # Extract clean display name for channel and topic
    channel_name = str(event.chat_id)
    if hasattr(event, 'chat') and event.chat:
        if hasattr(event.chat, 'title') and event.chat.title:
            channel_name = event.chat.title
        elif hasattr(event.chat, 'username') and event.chat.username:
            channel_name = event.chat.username

    topic_name = None
    if topic_id:
        topic_name = await get_topic_name(tg_client, event.chat, topic_id)

    channel_display = channel_name
    if topic_name:
        channel_display = f"{channel_name} - {topic_name}"

    chat_identifier = f"{channel_display} ({event.chat_id})"

    file_size = event.message.file.size if event.message.file else 0
    msg_is_video = is_video_message(event.message)
    if file_size > MAX_MEDIA_SIZE_BYTES:
        if msg_is_video and OVERSIZED_VIDEO_ACTION in ("split", "compress"):
            if file_size > OVERSIZED_VIDEO_MAX_INPUT_BYTES:
                logging.warning(
                    f"[{chat_identifier}] Video skipped: File with {round(file_size / (1024 * 1024), 2)} MB "
                    f"exceeds maximum allowed size for processing ({OVERSIZED_VIDEO_MAX_INPUT_MB} MB)."
                )
                return
            logging.info(
                f"[{chat_identifier}] Oversized video detected ({round(file_size / (1024 * 1024), 2)} MB > "
                f"{MAX_MEDIA_SIZE_MB} MB). Will {OVERSIZED_VIDEO_ACTION} after download."
            )
        else:
            logging.warning(f"[{chat_identifier}] Media skipped: File with {round(file_size / (1024 * 1024), 2)} MB exceeds limit of {MAX_MEDIA_SIZE_MB} MB.")
            return

    if event.message.grouped_id is not None:
        album_id = event.message.grouped_id
        
        if album_id in PROCESSED_ALBUMS:
            return
            
        PROCESSED_ALBUMS.add(album_id)
        logging.info(f"[{chat_identifier}] New album detected (Grouped ID: {album_id}). Waiting for complete reception...")
        
        try:
            await asyncio.sleep(2.5)
            
            try:
                album_messages = await tg_client.get_messages(
                    event.chat_id, 
                    min_id=event.message.id - 15, 
                    max_id=event.message.id + 15,
                    limit=30
                )
                filtered_messages = [m for m in album_messages if m.grouped_id == album_id]
                
                logging.info(f"[{chat_identifier}] Processing {len(filtered_messages)} items from album {album_id}...")
                for msg in reversed(filtered_messages):
                    if msg.media:
                        exact_size = msg.file.size if msg.file else 0
                        item_is_video = is_video_message(msg)
                        if exact_size > MAX_MEDIA_SIZE_BYTES:
                            if item_is_video and OVERSIZED_VIDEO_ACTION in ("split", "compress"):
                                if exact_size > OVERSIZED_VIDEO_MAX_INPUT_BYTES:
                                    logging.warning(
                                        f"[{chat_identifier}] Item in album skipped: Actual size ({round(exact_size / (1024 * 1024), 2)} MB) "
                                        f"exceeds maximum allowed size for processing ({OVERSIZED_VIDEO_MAX_INPUT_MB} MB)"
                                    )
                                    continue
                                logging.info(
                                    f"[{chat_identifier}] Oversized video in album ({round(exact_size / (1024 * 1024), 2)} MB > "
                                    f"{MAX_MEDIA_SIZE_MB} MB). Will {OVERSIZED_VIDEO_ACTION} after download."
                                )
                            else:
                                logging.warning(f"[{chat_identifier}] Item in album skipped: Actual size ({round(exact_size / (1024 * 1024), 2)} MB) exceeds limit ({MAX_MEDIA_SIZE_MB} MB)")
                                continue
                        
                    await process_and_upload_media(msg, chat_identifier, channel_display)
            except Exception as e:
                logging.error(f"[{chat_identifier}] Error loading album {album_id}: {e}")
                
            await asyncio.sleep(10)
        finally:
            PROCESSED_ALBUMS.discard(album_id)
    else:
        await process_and_upload_media(event.message, chat_identifier, channel_display)


async def main():
    logging.info("Starting Telegram client...")
    await tg_client.start()
    logging.info(f"Bridge successfully started and active for channels: {TG_CHANNELS}")
    logging.info(f"Target Matrix rooms: {MATRIX_ROOM_IDS}")
    logging.info(f"Configured max media limit: {MAX_MEDIA_SIZE_MB} MB")
    logging.info(
        f"Oversized video action: {OVERSIZED_VIDEO_ACTION.upper()} "
        f"(Max input: {OVERSIZED_VIDEO_MAX_INPUT_MB} MB, Fallback compress: {OVERSIZED_VIDEO_FALLBACK_COMPRESS})"
    )
    logging.info(f"Configured min image limit: {MIN_IMAGE_SIZE_KB} KB ({round(MIN_IMAGE_SIZE_KB / 1024, 2)} MB)" if MIN_IMAGE_SIZE_KB > 0 else "Configured min image limit: None")
    logging.info(f"Configured min video limit: {MIN_VIDEO_SIZE_KB} KB ({round(MIN_VIDEO_SIZE_KB / 1024, 2)} MB)" if MIN_VIDEO_SIZE_KB > 0 else "Configured min video limit: None")
    logging.info(f"Images enabled: {ENABLE_IMAGES}")
    logging.info(f"Videos enabled: {ENABLE_VIDEOS}")
    logging.info(f"Media deduplication: {'Enabled' if DEDUPLICATION_ENABLED else 'Disabled'} (TTL: {DEDUPLICATION_TTL_MINUTES} min, Max cache: {DEDUPLICATION_CACHE_SIZE})")

    matrix_sync_task = None
    if ADMIN_MATRIX_USER_ID or ALLOW_NON_ADMIN_STOP:
        logging.info(f"Matrix command listener active (Admin User: {ADMIN_MATRIX_USER_ID or 'None'}, Allow Non-Admin Stop: {ALLOW_NON_ADMIN_STOP})")
        matrix_client.add_event_callback(on_matrix_message, RoomMessageText)
        matrix_sync_task = asyncio.create_task(matrix_client.sync_forever(timeout=30000, full_state=False))
    else:
        logging.info("Admin Matrix user (ADMIN_MATRIX_USER_ID) and ALLOW_NON_ADMIN_STOP not configured. Dynamic chat commands disabled.")

    logging.info(f"Llama Guard Moderation: {'Enabled' if LLAMAGUARD_API_URL else 'Disabled'}")
    if LLAMAGUARD_API_URL:
        logging.info(f"Llama Guard Model: {LLAMAGUARD_MODEL_NAME}")
        logging.info(f"Llama Guard Video Frames: {LLAMAGUARD_VIDEO_FRAMES} (Random Selection: {LLAMAGUARD_RANDOM_FRAMES})")
        logging.info(f"Llama Guard Checks Filter (Block List): {list(LLAMAGUARD_CHECKS) if LLAMAGUARD_CHECKS else 'ALL categories'}")
        logging.info(f"Llama Guard Required Checks (Whitelist): {list(LLAMAGUARD_REQUIRE_CHECKS) if LLAMAGUARD_REQUIRE_CHECKS else 'None (safe content allowed)'}")
    try:
        await tg_client.run_until_disconnected()
    finally:
        if matrix_sync_task and not matrix_sync_task.done():
            matrix_sync_task.cancel()
        await matrix_client.close()

if __name__ == '__main__':
    asyncio.run(main())
