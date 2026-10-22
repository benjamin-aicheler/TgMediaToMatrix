"""Media handling: Telegram identifiers, ramdisk scratch space, ffprobe/ffmpeg
work, frame extraction, thumbnails and blurhashes."""

import asyncio
import hashlib
import io
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time

import config

logger = logging.getLogger("tgmabr")


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


def extract_video_frames(video_bytes: bytes, max_frames: int = 5, use_random: bool = True) -> list:
    """Extracts up to max_frames frames from the video as JPEG bytes in-memory (either evenly spaced or randomly selected)"""
    try:
        import importlib.util
        import io
        import random

        import av
        # Pillow is needed by av's frame.to_image(); check for it explicitly so the
        # error message below stays accurate instead of failing deeper inside PyAV.
        if importlib.util.find_spec("PIL") is None:
            raise ImportError("Pillow (PIL) is not installed")
    except ImportError:
        logger.error("PygAV (av) or Pillow (PIL) package is missing! Please install them to enable video frame safety moderation.")
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
                except (TypeError, ValueError, OverflowError):
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
            target_indices = sorted(set(target_indices))

        target_set = set(target_indices)
        for frame_count, frame in enumerate(container.decode(video=0)):
            if frame_count in target_set:
                img = frame.to_image()
                img_bytes_io = io.BytesIO()
                img.save(img_bytes_io, format="JPEG")
                frames_bytes.append(img_bytes_io.getvalue())
                if len(frames_bytes) >= len(target_set):
                    break

        # Fallback if no frames were decoded but we expected them
        if not frames_bytes:
            container.seek(0)
            for frame in container.decode(video=0):
                img = frame.to_image()
                img_bytes_io = io.BytesIO()
                img.save(img_bytes_io, format="JPEG")
                frames_bytes.append(img_bytes_io.getvalue())
                break

    except Exception as e:  # noqa: BLE001 - PyAV/Pillow raise library-specific errors; no frames just disables the check
        logger.error(f"Failed to extract frames from video: {e}")
    finally:
        if container is not None:
            try:
                container.close()
            except Exception as e:  # noqa: BLE001 - closing a broken container may raise anything; the frames are already collected
                logger.debug(f"Could not close the video container: {e}")
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


def _video_attribute(document):
    """Returns the document's DocumentAttributeVideo, or None when it carries none."""
    for attr in getattr(document, 'attributes', None) or []:
        if hasattr(attr, 'w') and hasattr(attr, 'h') and hasattr(attr, 'duration'):
            return attr
    return None


# Telegram ships tiny preview/scrub clips next to the real renditions: a few dozen KB,
# marked with a video_start_ts, usually 144x80 or smaller. They would be useless as a
# substitute video, so anything below these bounds is not treated as a rendition.
MIN_RENDITION_WIDTH = 240
MIN_RENDITION_HEIGHT = 120


def is_rendition_document(document) -> bool:
    """True for a real transcoded rendition, False for a preview/scrub clip or a non-video."""
    attr = _video_attribute(document)
    if attr is None:
        return False
    if getattr(attr, 'video_start_ts', None):
        return False
    if (attr.w or 0) < MIN_RENDITION_WIDTH or (attr.h or 0) < MIN_RENDITION_HEIGHT:
        return False
    return (getattr(attr, 'duration', 0) or 0) > 0


def describe_alt_documents(message, upload_limit_bytes: int, processing_limit_bytes: int) -> str:
    """Summarises the alternative video qualities Telegram carries for a document.

    Large videos are stored as one primary document plus transcoded renditions in
    ``MessageMediaDocument.alt_documents``. Telegram clients pick one of them depending on
    the data-saver setting, which is why the app can show a smaller size than the primary
    document the bridge reads. This only describes what is on offer; nothing is downloaded
    and nothing is selected here.

    Renditions are grouped by resolution and labelled against both limits, because they
    answer different questions: what could be uploaded as-is, and what is small enough to
    be processed at all.
    """
    media_obj = getattr(message, 'media', None)
    alternatives = list(getattr(media_obj, 'alt_documents', None) or [])
    if not alternatives:
        return "Telegram lists no alternative qualities for this video."

    renditions = [document for document in alternatives if is_rendition_document(document)]
    ignored = len(alternatives) - len(renditions)
    if not renditions:
        return (
            f"Telegram lists {len(alternatives)} alternative document(s), "
            f"none of them a usable rendition — all preview clips."
        )

    grouped: dict[tuple[int, int], list[int]] = {}
    for document in renditions:
        attr = _video_attribute(document)
        if attr is None:  # is_rendition_document() already guarantees one, kept for the type checker
            continue
        grouped.setdefault((attr.w, attr.h), []).append(getattr(document, 'size', 0) or 0)

    def as_mb(value: int) -> str:
        return f"{round(value / (1024 * 1024), 2)} MB"

    described = []
    for (width, height), sizes in sorted(grouped.items(), key=lambda item: item[0][0] * item[0][1], reverse=True):
        sizes.sort(reverse=True)
        upload_fits = [size for size in sizes if size <= upload_limit_bytes]
        processing_fits = [size for size in sizes if size <= processing_limit_bytes]
        if upload_fits:
            note = f"largest fitting the upload limit: {as_mb(upload_fits[0])}"
        elif processing_fits:
            note = "above the upload limit, small enough for processing"
        else:
            note = "above the processing limit as well"
        described.append(f"{width}x{height}: {', '.join(as_mb(size) for size in sizes)} ({note})")

    summary = f"Telegram lists {len(renditions)} usable rendition(s) in {len(grouped)} resolution(s)"
    if ignored:
        summary += f", {ignored} preview clip(s) ignored"
    return summary + ": " + " | ".join(described)


SHM_PATH = "/dev/shm"
# Scratch space is only ever taken from the ramdisk; if it does not fit, the media is skipped.
SHM_HEADROOM_BYTES = 64 * 1024 * 1024

try:
    SHM_WAIT_TIMEOUT_SECONDS = max(0, int(os.environ.get("SHM_WAIT_TIMEOUT_SECONDS", "30")))
except ValueError:
    SHM_WAIT_TIMEOUT_SECONDS = 30


def shm_space() -> tuple[int, int] | None:
    """Returns (total, free) bytes on the ramdisk, or None when it is not available."""
    if not os.path.isdir(SHM_PATH):
        return None
    try:
        stat = os.statvfs(SHM_PATH)
    except OSError:
        return None
    return stat.f_blocks * stat.f_frsize, stat.f_bavail * stat.f_frsize


def shm_required_bytes(media_size: int) -> int:
    """Scratch space a split or compression run needs: the input plus the produced parts."""
    return int(media_size * 2) + SHM_HEADROOM_BYTES


async def reserve_shm_space(required_bytes: int, source_chat: str, filename: str) -> bool:
    """Waits (bounded) for enough room on the ramdisk.

    Returns False when the media has to be skipped. There is deliberately no fallback to a
    disk directory: if the ramdisk cannot hold the scratch data, the media is dropped.
    """
    space = shm_space()
    if space is None:
        logger.error(f"[{source_chat}] /dev/shm is not available; skipping {filename} because scratch data must not be written to disk.")
        return False

    total, free = space
    if total < required_bytes:
        logger.error(
            f"[{source_chat}] {filename} needs {required_bytes / (1024 * 1024):.0f} MB of ramdisk, but /dev/shm "
            f"only provides {total / (1024 * 1024):.0f} MB in total. Skipping."
        )
        return False

    if free >= required_bytes:
        return True

    logger.warning(
        f"[{source_chat}] Waiting up to {SHM_WAIT_TIMEOUT_SECONDS}s for ramdisk space: {filename} needs "
        f"{required_bytes / (1024 * 1024):.0f} MB, {free / (1024 * 1024):.0f} MB free."
    )
    deadline = time.monotonic() + SHM_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        current = shm_space()
        if current is None:
            return False
        if current[1] >= required_bytes:
            logger.info(f"[{source_chat}] Ramdisk space available again for {filename}.")
            return True

    logger.warning(f"[{source_chat}] Still not enough ramdisk space for {filename} after {SHM_WAIT_TIMEOUT_SECONDS}s. Skipping.")
    return False


FFPROBE_TIMEOUT_SECONDS = 5


def find_ffprobe(ffmpeg_bin: str | None) -> str | None:
    """Locates ffprobe: first on PATH, then next to the resolved ffmpeg binary."""
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin and ffmpeg_bin:
        candidate = os.path.join(os.path.dirname(ffmpeg_bin), "ffprobe")
        if os.path.exists(candidate):
            ffprobe_bin = candidate
    return ffprobe_bin


def ffprobe_stdout(ffprobe_bin: str, args: list[str], timeout: float = FFPROBE_TIMEOUT_SECONDS) -> str | None:
    """Runs ffprobe and returns the stripped stdout, or None when it produced nothing usable."""
    try:
        res = subprocess.run(
            [ffprobe_bin, "-v", "error", *args],
            capture_output=True, text=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    output = res.stdout.strip()
    return output or None


def probe_video_duration(video_path: str, ffmpeg_bin: str) -> float | None:
    """Probes the video duration in seconds using ffprobe or av."""
    ffprobe_bin = find_ffprobe(ffmpeg_bin)
    if ffprobe_bin:
        output = ffprobe_stdout(ffprobe_bin, [
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ])
        if output:
            try:
                val = float(output)
                if val > 0:
                    return val
            except ValueError:
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
    except Exception as e:  # noqa: BLE001 - av fallback for duration: any library failure means the value is unknown
        logger.debug(f"Could not probe duration with av: {e}")

    return None


def probe_video_dimensions(video_path: str, ffmpeg_bin: str) -> tuple[int, int] | None:
    """Probes video width and height using ffprobe or av."""
    ffprobe_bin = find_ffprobe(ffmpeg_bin)
    if ffprobe_bin:
        output = ffprobe_stdout(ffprobe_bin, [
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            video_path,
        ])
        if output and "x" in output:
            try:
                width_str, height_str = output.split("x", 1)
                return int(width_str), int(height_str)
            except ValueError:
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
    except Exception as e:  # noqa: BLE001 - av fallback for dimensions: any library failure means the value is unknown
        logger.debug(f"Could not probe dimensions with av: {e}")

    return None


def split_video_lossless(
    video_bytes: bytes,
    filename: str,
    max_bytes: int,
    source_chat: str,
    known_duration: float | None = None
) -> list[dict]:
    """
    Losslessly splits an oversized video into sequential parts smaller than max_bytes using FFmpeg stream copy (-c copy).
    Operates in-memory via /dev/shm (if available) with zero re-encoding, optimal for low-power CPUs like Raspberry Pi 4.
    """
    t0 = time.time()
    ffmpeg_bin = config.FFMPEG_PATH or shutil.which("ffmpeg")
    if not ffmpeg_bin:
        logger.error(f"[{source_chat}] FFmpeg binary not found. Cannot split video.")
        return []

    name_part, ext = os.path.splitext(filename)
    if not ext or ext.lower() not in ('.mp4', '.mkv', '.webm', '.mov', '.avi'):
        ext = '.mp4'

    total_size = len(video_bytes)
    target_part_size = max_bytes * 0.85
    parts_count = max(2, math.ceil(total_size / target_part_size))

    if not os.path.isdir(SHM_PATH):
        logger.error(f"[{source_chat}] /dev/shm is not available. Splitting is skipped instead of writing to disk.")
        return []

    with tempfile.TemporaryDirectory(dir=SHM_PATH) as tmp_dir:
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

        res = subprocess.run(cmd, capture_output=True, check=False)
        if res.returncode != 0:
            logger.error(f"[{source_chat}] FFmpeg split error: {res.stderr.decode('utf-8', errors='replace')[:400]}")
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
                except OSError:
                    pass
            # Retry with a shorter segment time. Locate the argument by name: positional
            # indices are unreliable here, because the optional -movflags pair shifts them
            # and cmd[-2] addresses "+faststart" instead of the segment time.
            cmd[cmd.index("-segment_time") + 1] = str(segment_time)
            res = subprocess.run(cmd, capture_output=True, check=False)
            if res.returncode != 0:
                logger.error(f"[{source_chat}] FFmpeg split retry error: {res.stderr.decode('utf-8', errors='replace')[:400]}")
            part_files = sorted([
                os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir)
                if f.startswith("part_") and f.endswith(ext)
            ])

        if len(part_files) <= 1:
            logger.warning(f"[{source_chat}] Video could not be split losslessly (no internal keyframes found).")
            return []

        oversized_parts = [f for f in part_files if os.path.getsize(f) > max_bytes]
        if oversized_parts:
            logger.warning(
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

        logger.info(
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
    ffmpeg_bin = config.FFMPEG_PATH or shutil.which("ffmpeg")
    if not ffmpeg_bin:
        logger.error(f"[{source_chat}] FFmpeg binary not found. Cannot compress video.")
        return None

    name_part, ext = os.path.splitext(filename)
    out_ext = ".mp4"

    if not os.path.isdir(SHM_PATH):
        logger.error(f"[{source_chat}] /dev/shm is not available. Compression is skipped instead of writing to disk.")
        return None

    with tempfile.TemporaryDirectory(dir=SHM_PATH) as tmp_dir:
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
        res = subprocess.run(cmd, capture_output=True, check=False)
        if res.returncode != 0:
            # Audio stream copy is not supported for every codec/container pair, so fall back
            # to re-encoding the audio. Locate the argument by name: the previous positional
            # slice (cmd[-3:-2]) replaced -movflags instead of -c:a and produced an invalid
            # command that ffmpeg rejected outright.
            audio_arg = cmd.index("-c:a")
            cmd[audio_arg:audio_arg + 2] = ["-c:a", "aac", "-b:a", "128k"]
            res = subprocess.run(cmd, capture_output=True, check=False)
            if res.returncode != 0:
                logger.error(f"[{source_chat}] FFmpeg compression failed: {res.stderr.decode('utf-8', errors='replace')[:400]}")
                return None

        if os.path.exists(output_path):
            with open(output_path, "rb") as out_f:
                out_bytes = out_f.read()

            dur = probe_video_duration(output_path, ffmpeg_bin)
            dims = probe_video_dimensions(output_path, ffmpeg_bin)

            logger.info(
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
        logger.error("Pillow (PIL) package is missing! Please install it to enable thumbnail dimension probing.")
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.format not in STILL_IMAGE_FORMATS:
                logger.debug(f"Ignoring thumbnail of unexpected format {img.format}.")
                return None
            width, height = img.size
            Image.init()
            mime = Image.MIME.get(img.format)
            if not mime or width < 1 or height < 1:
                return None
            return int(width), int(height), mime
    except (OSError, ValueError) as e:
        logger.debug(f"Could not probe thumbnail bytes: {e}")
        return None


def generate_image_thumbnail(image_bytes: bytes, max_size: int = 800) -> tuple[bytes, int, int, str] | None:
    """Generates a high-quality downscaled thumbnail from still image bytes.

    Returns (thumbnail_bytes, width, height, mime_type) or None if the bytes
    could not be decoded or processed."""
    try:
        from PIL import Image
    except ImportError:
        logger.error("Pillow (PIL) package is missing! Cannot generate image thumbnail.")
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.format not in STILL_IMAGE_FORMATS:
                logger.debug(f"Refusing to generate thumbnail for unexpected format {img.format}.")
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
    except (OSError, ValueError) as e:
        logger.debug(f"Could not generate image thumbnail: {e}")
        return None


def compute_blurhash(image_bytes: bytes) -> str | None:
    """Encodes a blurhash from still image bytes, downscaling them to a thumbnail first.

    Returns None whenever a hash cannot be produced. A blurhash is a rendering
    nicety rather than a safety property, so an absent package or an undecodable
    image degrades to an event without one, never to dropped media."""
    try:
        import blurhash
        from PIL import Image
    except ImportError:
        logger.debug("Pillow (PIL) or blurhash package is missing; sending media without a blurhash.")
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.format not in STILL_IMAGE_FORMATS:
                logger.debug(f"Refusing to blurhash unexpected format {img.format}.")
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
    except Exception as e:  # noqa: BLE001 - blurhash encodes arbitrary bytes and the hash is cosmetic
        logger.debug(f"Could not compute blurhash: {e}")
        return None

